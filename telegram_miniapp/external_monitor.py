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
from .monitor_cluster import MonitorLease, replicas_configured

LOG = logging.getLogger("hermes.external_monitor")


def valid_public_origin(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https" and bool(parsed.hostname)
        and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
        and not parsed.username and not parsed.password
        and not parsed.query and not parsed.fragment and parsed.path in {"", "/"}
    )


@dataclass(frozen=True)
class MonitorConfig:
    url: str
    token: str
    owner_id: int
    interval: int = 60
    external: bool = True
    urls: tuple[str, ...] = ()

    @property
    def endpoints(self) -> tuple[str, ...]:
        return self.urls or (self.url,)

    @classmethod
    def from_env(cls) -> MonitorConfig:
        url = os.getenv("HERMES_MONITOR_URL", "").rstrip("/")
        supplied = os.getenv("HERMES_MONITOR_URLS", "")
        urls = tuple(dict.fromkeys(item.strip().rstrip("/") for item in supplied.split(";") if item.strip()))
        if urls:
            url = urls[0]
        bot = os.getenv("TELEGRAM_BOT_TOKEN", "")
        owner = os.getenv("TELEGRAM_OWNER_ID", "")
        if (
            not url
            or not all(valid_public_origin(target) for target in (urls or (url,)))
            or not bot
            or not owner.isdecimal()
            or int(owner) <= 0
        ):
            raise ValueError("Set a public HTTPS HERMES_MONITOR_URL, TELEGRAM_BOT_TOKEN and TELEGRAM_OWNER_ID")
        return cls(url=url, token=bot, owner_id=int(owner),
                   interval=max(15, min(3600, int(os.getenv("HERMES_MONITOR_INTERVAL", "60")))),
                   urls=urls)

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
                       queue: AlertQueue, cluster: MonitorLease | None = None) -> bool:
    # Endpoints are alternatives: a live backup HTTPS endpoint avoids false
    # outages when the primary tunnel or DNS record becomes unavailable.
    statuses = await asyncio.gather(*(public_probe(session, u) for u in config.endpoints))
    healthy = any(statuses)
    event = state.observe(healthy, now=asyncio.get_running_loop().time())
    if event and (cluster is None or await cluster.accept_event(event)):
        queue.enqueue(event)
    # Ordered retry queue: never claims Telegram message delivered before Bot API ack.
    await drain_queue(queue, session, config)
    return healthy


async def monitor(config: MonitorConfig) -> None:
    state = AlertState()
    path = Path(os.environ.get("EXTERNAL_MONITOR_QUEUE_FILE", ".hermes-uptime-alerts.json"))
    queue = AlertQueue(path)
    lease = None
    redis = None
    url = os.getenv("HERMES_MONITOR_REDIS_URL", "")
    if replicas_configured() and not url:
        raise ValueError("HERMES_MONITOR_REDIS_URL is required when HERMES_MONITOR_REPLICAS > 1")
    if url:
        import redis.asyncio as redis_async  # optional dependency: requirements-monitor.txt

        redis = redis_async.from_url(url, socket_timeout=3, socket_connect_timeout=3)
        lease = MonitorLease(redis, key=os.getenv(
            "HERMES_MONITOR_CLUSTER_KEY", "hermes-voice:external-monitor"
        ), ttl=max(90, config.interval * 3))
    try:
        async with ClientSession() as session:
            while True:
                try:
                    leader = await lease.claim() if lease else True
                    if leader:
                        if lease and lease.just_claimed:
                            state.offline = await lease.current_down()
                        await monitor_once(session, config, state, queue, lease)
                except (ClientError, OSError, ValueError) as exc:
                    LOG.warning("External monitor cycle failed: %s", type(exc).__name__)
                await asyncio.sleep(config.interval)
    finally:
        if lease is not None:
            await lease.release()
        if redis is not None:
            await redis.aclose()

def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(monitor(MonitorConfig.from_env()))


if __name__ == "__main__":
    main()
