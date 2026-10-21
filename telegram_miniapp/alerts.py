"""Debounced Hermes gateway health alerts using Telegram sendMessage.

This monitors the Hermes API server's /health endpoint, not OS process health.
No Bot API getUpdates/webhook mutation is performed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession, ClientTimeout

LOG = logging.getLogger("hermes.telegram_alerts")


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


async def probe_gateway(session: ClientSession, url: str) -> bool:
    """Hermes API server GET /health returns {"status": "ok"}."""
    try:
        async with session.get(url.rstrip("/") + "/health", timeout=ClientTimeout(total=6)) as response:
            if response.status != 200:
                return False
            body = await response.json(content_type=None)
            return isinstance(body, dict) and body.get("status") == "ok"
    except (ClientError, TimeoutError, ValueError):
        return False


async def send_alert(session: ClientSession, token: str, owner_id: int, text: str) -> bool:
    try:
        async with session.post(
            "https://api.telegram.org/bot" + token + "/sendMessage",
            json={"chat_id": owner_id, "text": text, "disable_web_page_preview": True},
            timeout=ClientTimeout(total=10),
        ) as response:
            body = await response.json(content_type=None)
            return response.status == 200 and body.get("ok") is True
    except (ClientError, TimeoutError, ValueError):
        LOG.warning("Telegram alert delivery failed; check connectivity/token/chat permissions")
        return False


async def gateway_watch(config, *, interval: float | None = None) -> None:
    """One alert after consecutive failures, then one recovery notification.

    The monitor is in-process. It cannot notify if the entire Windows PC or
    monitor process is powered off; use an external uptime monitor for that case.
    """
    if interval is None:
        try:
            interval = float(os.getenv("GATEWAY_ALERT_INTERVAL", "60"))
        except ValueError:
            interval = 60
    interval = max(15, min(interval, 3600))
    state = AlertState()
    async with ClientSession() as session:
        while True:
            healthy = await probe_gateway(session, config.hermes_url)
            event = state.observe(healthy, now=time.monotonic())
            if event == "down":
                await send_alert(
                    session, config.bot_token, config.owner_id,
                    "🔴 Hermes Gateway: API ไม่ตอบสนองต่อเนื่อง 3 ครั้ง กรุณาตรวจสอบเครื่องและ Gateway",
                )
            elif event == "recovered":
                await send_alert(
                    session, config.bot_token, config.owner_id,
                    "🟢 Hermes Gateway: API กลับมาทำงานและตอบ /health แล้ว",
                )
            await asyncio.sleep(interval)
