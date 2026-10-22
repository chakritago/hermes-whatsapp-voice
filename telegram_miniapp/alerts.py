"""Reliable Telegram owner alerts and non-intrusive Hermes API readiness monitoring.

A bounded disk-backed FIFO survives process crashes. Only generic health events are
persisted, never bot tokens, session handles, transcripts or user data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout

from .readiness import HealthSnapshot, check_readiness

LOG = logging.getLogger("hermes.telegram_alerts")
MAX_QUEUED = 50


@dataclass
class AlertState:
    failures: int = 0
    offline: bool = False
    last_alert_at: float = 0

    def observe(self, healthy: bool, *, now: float, threshold: int = 3) -> str | None:
        if healthy:
            self.failures = 0
            if self.offline:
                self.offline = False
                return "recovered"
            return None
        self.failures += 1
        if not self.offline and self.failures >= threshold:
            self.offline = True
            self.last_alert_at = now
            return "down"
        return None


@dataclass
class PendingAlert:
    event: str
    created_at: float
    attempts: int = 0
    next_attempt_at: float = 0

    def as_dict(self) -> dict:
        return vars(self).copy()


class AlertQueue:
    def __init__(self, path: Path):
        self.path = path
        self.items: list[PendingAlert] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise TypeError("invalid alert queue")
            for item in raw[-MAX_QUEUED:]:
                if item["event"] in {"down", "recovered"}:
                    self.items.append(PendingAlert(
                        item["event"], float(item["created_at"]),
                        max(0, int(item.get("attempts", 0))),
                        float(item.get("next_attempt_at", 0)),
                    ))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            LOG.warning("Unable to load alert retry queue; starting with an empty queue")

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        staging = self.path.with_name(self.path.name + ".tmp")
        staging.write_text(json.dumps([item.as_dict() for item in self.items]), encoding="utf-8")
        os.replace(staging, self.path)

    def enqueue(self, event: str, *, now: float | None = None) -> None:
        if event not in {"down", "recovered"}:
            raise ValueError("unsupported gateway event")
        now = time.time() if now is None else now
        if self.items and self.items[-1].event == event:
            return
        self.items.append(PendingAlert(event, now, next_attempt_at=now))
        self.items = self.items[-MAX_QUEUED:]
        self._save()

    def acknowledge(self) -> None:
        if self.items:
            self.items.pop(0)
            self._save()

    def postpone(self, *, now: float | None = None, retry_after: float = 0) -> None:
        if not self.items:
            return
        now = time.time() if now is None else now
        alert = self.items[0]
        alert.attempts += 1
        alert.next_attempt_at = now + max(
            max(1, retry_after), min(300, 2 ** min(alert.attempts, 8)),
        )
        self._save()


MESSAGES = {
    "down": "🔴 Hermes Gateway: API readiness ไม่ผ่านต่อเนื่อง 3 ครั้ง กรุณาตรวจสอบ",
    "recovered": "🟢 Hermes Gateway: API readiness กลับมาทำงานแล้ว",
}
EXTERNAL_MESSAGES = {
    "down": "🔴 External Monitor: ติดต่อ Hermes Windows Endpoint ไม่ได้ต่อเนื่อง (เครื่อง/เครือข่าย/HTTPS อาจมีปัญหา)",
    "recovered": "🟢 External Monitor: Hermes Windows Endpoint กลับมาตอบสนองแล้ว",
}


async def probe_gateway(session: ClientSession, url: str) -> bool:
    """Compatibility helper for the old health-only tests."""
    try:
        async with session.get(url.rstrip("/") + "/health", timeout=ClientTimeout(total=6)) as response:
            if response.status != 200:
                return False
            body = await response.json(content_type=None)
            return isinstance(body, dict) and body.get("status") == "ok"
    except (ClientError, TimeoutError, ValueError):
        return False


async def deliver_alert(
    session: ClientSession, token: str, owner_id: int, text: str
) -> tuple[bool, float]:
    """Return success and a server-suggested retry delay (429 retry_after)."""
    try:
        async with session.post(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            json={"chat_id": owner_id, "text": text, "disable_web_page_preview": True},
            timeout=ClientTimeout(total=10),
        ) as response:
            body = await response.json(content_type=None)
            if response.status == 200 and body.get("ok") is True:
                return True, 0
            retry = (body.get("parameters") or {}).get("retry_after", 0)
            return False, max(0, min(3600, float(retry or 0)))
    except (ClientError, TimeoutError, ValueError, TypeError):
        LOG.warning("Telegram delivery unsuccessful; alert remains queued")
        return False, 0


async def send_alert(session: ClientSession, token: str, owner_id: int, text: str) -> bool:
    """Legacy bool interface; queue uses deliver_alert with retry hints."""
    success, _retry = await deliver_alert(session, token, owner_id, text)
    return success


async def drain_queue(queue: AlertQueue, session: ClientSession, config, *, now: float | None = None) -> bool:
    if not queue.items:
        return False
    now = time.time() if now is None else now
    pending = queue.items[0]
    if pending.next_attempt_at > now:
        return False
    successful, retry = await deliver_alert(
        session, config.bot_token, config.owner_id,
        (EXTERNAL_MESSAGES if getattr(config, "external", False) else MESSAGES)[pending.event],
    )
    if successful:
        queue.acknowledge()
    else:
        queue.postpone(now=now, retry_after=retry)
    return successful


def alert_queue_path() -> Path:
    explicit = os.getenv("GATEWAY_ALERT_QUEUE_FILE")
    if explicit:
        return Path(explicit).expanduser()
    base = Path(os.getenv("LOCALAPPDATA") or Path.home() / ".hermes")
    return base / "HermesTelegramVoice" / "gateway-alerts.json"


async def gateway_watch(config, *, interval: float | None = None, snapshot: HealthSnapshot | None = None) -> None:
    """Probe readiness, retry alerts without spamming or replacing Telegram webhook.

    Runs in the Mini App backend process; cannot alert if the whole host is offline.
    """
    if interval is None:
        try:
            interval = float(os.getenv("GATEWAY_ALERT_INTERVAL", "60"))
        except ValueError:
            interval = 60
    interval = max(15, min(interval, 3600))
    state = AlertState()
    queue = AlertQueue(alert_queue_path())
    snapshot = snapshot or HealthSnapshot()
    async with ClientSession() as session:
        while True:
            try:
                readiness = await check_readiness(session, config)
                snapshot.last = readiness
                event = state.observe(readiness.gateway_ready, now=time.monotonic())
                if event:
                    queue.enqueue(event)
                await drain_queue(queue, session, config)
            except (OSError, ValueError, TypeError):
                LOG.exception("Gateway monitor cycle failed; will retry")
            await asyncio.sleep(interval)
