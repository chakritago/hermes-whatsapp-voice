"""Independent, external HTTPS uptime monitor.

Install on a *different powered-on host* with its own secret environment.
Checks public /healthz and /readyz (never accesses /ws or agent tools).
This detects unreachable public endpoints, NOT the root cause of an outage.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout

from .alerts import AlertQueue, AlertState, drain_queue

LOG = logging.getLogger("hermes.external_monitor")


@dataclass(frozen=True)
class MonitorConfig:
    url: str
    token: str
    owner_id: int
    interval: int = 60
    external: bool = True

    @classmethod
    def from_env(cls) -> MonitorConfig:
        url = os.getenv("HERMES_MONITOR_URL", "").rstrip("/")
        bot = os.getenv("TELEGRAM_BOT_TOKEN", "")
        owner = os.getenv("TELEGRAM_OWNER_ID", "")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not bot
            or not owner.isdecimal()
            or int(owner) <= 0
        ):
            raise ValueError("Set a public HTTPS HERMES_MONITOR_URL, TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID")
        return cls(url=url, token=bot, owner_id=int(owner),
                   interval=max(15, min(3600, int(os.getenv("HERMES_MONITOR_INTERVAL", "60")))))

    @property
    def bot_token(self) -> str:
        return self.token


async def public_probe(session: ClientSession, url: str) -> bool:
    """HTTPS only, no redirect to unexpected hosts; no authentication secrets sent."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("HTTPS endpoint required")
    for path, field in (("/healthz", "ok"), ("/readyz", "ready")):
        try:
            async with session.get(
                url.rstrip("/") + path,
                allow_redirects=False,
                timeout=ClientTimeout(total=8),
            ) as response:
                if response.status != 200:
                    return False
                body = await response.json(content_type=None)
                if not isinstance(body, dict) or body.get(field) is not True:
                    return False
        except (ClientError, TimeoutError, ValueError):
            return False
    return True


async def monitor_once(session: ClientSession, config: MonitorConfig, state: AlertState,
                       queue: AlertQueue) -> bool:
    healthy = await public_probe(session, config.url)
    event = state.observe(healthy, now=asyncio.get_running_loop().time())
    if event:
        queue.enqueue(event)
    # Ordered retry queue: never claims Telegram message delivered before Bot API ack.
    await drain_queue(queue, session, config)
    return healthy


async def monitor(config: MonitorConfig) -> None:
    state = AlertState()
    path = Path(os.environ.get("EXTERNAL_MONITOR_QUEUE_FILE", ".hermes-uptime-alerts.json"))
    queue = AlertQueue(path)
    async with ClientSession() as session:
        while True:
            try:
                await monitor_once(session, config, state, queue)
            except (ClientError, OSError, ValueError) as exc:
                LOG.warning("External monitor cycle failed: %s", type(exc).__name__)
            await asyncio.sleep(config.interval)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(monitor(MonitorConfig.from_env()))


if __name__ == "__main__":
    main()
