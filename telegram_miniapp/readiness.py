"""Side-effect-free checks: Hermes HTTP health, authenticated capabilities, Telegram bot API.

A successful probe is evidence for API readiness, not proof that Hermes's separate
Telegram long-poll/webhook worker is online. Never invoke an agent tool in a probe.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from aiohttp import ClientError, ClientSession, ClientTimeout

TIMEOUT = ClientTimeout(total=6)


@dataclass(frozen=True)
class Readiness:
    api: bool
    capabilities: bool
    telegram: bool
    checked_at: float

    @property
    def ready(self) -> bool:
        return self.api and self.capabilities and self.telegram

    @property
    def gateway_ready(self) -> bool:
        return self.api and self.capabilities


async def _get_ok(session: ClientSession, url: str, *, headers: dict | None = None) -> dict | None:
    try:
        async with session.get(url, headers=headers, timeout=TIMEOUT) as response:
            if response.status != 200:
                return None
            payload = await response.json(content_type=None)
            return payload if isinstance(payload, dict) else None
    except (ClientError, ValueError, TimeoutError):
        return None


async def check_readiness(session: ClientSession, config) -> Readiness:
    """Query three independent, read-only paths without leaking returned secrets."""
    base = config.hermes_url.rstrip("/")
    api, capabilities, bot = await asyncio.gather(
        _get_ok(session, base + "/health"),
        _get_ok(session, base + "/v1/capabilities", headers={
            "Authorization": "Bearer " + config.hermes_key,
        }),
        _get_ok(session, "https://api.telegram.org/bot" + config.bot_token + "/getMe"),
    )
    return Readiness(
        api=api is not None and api.get("status") == "ok",
        capabilities=capabilities is not None and (capabilities.get("features") or {}).get("run_submission") is True,
        telegram=bot is not None and bot.get("ok") is True and (bot.get("result") or {}).get("is_bot") is True,
        checked_at=time.time(),
    )


class HealthSnapshot:
    """One process-wide state updated by the monitor; /readyz never probes on request."""

    def __init__(self):
        self.last: Readiness | None = None

    def public(self, *, now: float | None = None, stale_after: float = 180) -> tuple[int, dict]:
        now = time.time() if now is None else now
        if self.last is None or now - self.last.checked_at > stale_after:
            return 503, {"ready": False, "state": "unknown"}
        state = "ready" if self.last.ready else "degraded"
        return (200 if self.last.ready else 503), {"ready": self.last.ready, "state": state}
