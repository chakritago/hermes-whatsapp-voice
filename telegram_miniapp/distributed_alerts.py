"""Redis-backed durable alert queue for external monitor replicas.

Writes state and event to the same Redis transaction (Lua). The leader holds
a lease; queue entries survive monitor process crashes and are retried.
Telegram delivery is at-least-once *best effort*, not exactly once: an ACK
lost after send can produce a duplicate notification.
"""
from __future__ import annotations

import json
import logging
import time

from .alerts import EXTERNAL_MESSAGES, deliver_alert

LOG = logging.getLogger("hermes.distributed_alerts")
MAX_PENDING = 1000


class DistributedAlerts:
    def __init__(self, lease):
        self.lease = lease
        self.redis = lease.redis
        self.key = lease.key + ":pending"

    async def enqueue(self, event: str, *, now: float | None = None) -> bool:
        if event not in EXTERNAL_MESSAGES or not self.lease.owned:
            return False
        item = json.dumps({
            "event": event, "attempts": 0,
            "next_attempt_at": now if now is not None else time.time(),
        })
        script = (
            'if redis.call("GET",KEYS[1]) ~= ARGV[1] then return 0 end; '
            'if redis.call("GET",KEYS[2]) == ARGV[2] then return 0 end; '
            'if redis.call("LLEN",KEYS[3]) >= tonumber(ARGV[4]) then return 0 end; '
            'redis.call("RPUSH",KEYS[3],ARGV[3]); '
            'redis.call("SET",KEYS[2],ARGV[2]); return 1'
        )
        try:
            return bool(await self.redis.eval(
                script, 3, self.lease.key, self.lease.state_key, self.key,
                self.lease.token, event, item, MAX_PENDING,
            ))
        except Exception as exc:  # noqa: BLE001 - optional redis client failures fail closed
            LOG.warning("Redis alert enqueue failed: %s", type(exc).__name__)
            return False

    async def peek(self) -> tuple[str, dict] | None:
        try:
            raw = await self.redis.eval(
                'if redis.call("GET",KEYS[1]) ~= ARGV[1] then return nil end; '
                'return redis.call("LINDEX",KEYS[2],0)',
                2, self.lease.key, self.key, self.lease.token,
            )
            if not raw:
                return None
            packet = json.loads(raw)
            if packet.get("event") not in EXTERNAL_MESSAGES:
                LOG.error("Unexpected queued Redis event")
                return None
            return raw.decode("utf-8") if isinstance(raw, bytes) else raw, packet
        except (ValueError, TypeError) as exc:
            LOG.error("Invalid persisted Redis event: %s", type(exc).__name__)
            return None
        except Exception as exc:  # noqa: BLE001 - optional redis failure
            LOG.warning("Redis alert peek failed: %s", type(exc).__name__)
            return None

    async def replace_head(self, old: str, new: str | None) -> bool:
        # Token fencing prevents another leader from acknowledging our event.
        script = (
            'if redis.call("GET",KEYS[1]) ~= ARGV[1] then return 0 end; '
            'if redis.call("LINDEX",KEYS[2],0) ~= ARGV[2] then return 0 end; '
            'if ARGV[3] == "" then redis.call("LPOP",KEYS[2]) '
            'else redis.call("LSET",KEYS[2],0,ARGV[3]) end; return 1'
        )
        try:
            return bool(await self.redis.eval(
                script, 2, self.lease.key, self.key,
                self.lease.token, old, new or "",
            ))
        except Exception as exc:  # noqa: BLE001 - optional redis failure
            LOG.warning("Redis alert acknowledgement failed: %s", type(exc).__name__)
            return False

    async def drain(self, session, config, *, now: float | None = None) -> bool:
        pending = await self.peek()
        if not pending:
            return False
        raw, item = pending
        clock = time.time() if now is None else now
        if float(item.get("next_attempt_at", 0)) > clock:
            return False
        successful, retry_after = await deliver_alert(
            session, config.bot_token, config.owner_id, EXTERNAL_MESSAGES[item["event"]],
        )
        if successful:
            return await self.replace_head(raw, None)
        attempt = min(12, max(0, int(item.get("attempts", 0))) + 1)
        item["attempts"] = attempt
        item["next_attempt_at"] = clock + max(1, retry_after, min(300, 2**min(attempt, 8)))
        await self.replace_head(raw, json.dumps(item))
        return False
