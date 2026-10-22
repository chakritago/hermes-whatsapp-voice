"""Optional Redis-backed leader lease and alert transition deduplication.

A single monitor needs no Redis. With multiple replicas, one leader probes,
and a shared state key prevents repeating an unchanged down/up event.
If Redis is unavailable, multi-replica monitors fail closed for alerts.
As with any external message API, exactly-once delivery cannot be guaranteed.
"""
from __future__ import annotations

import os
import secrets


class MonitorLease:
    def __init__(self, redis_client, *, key: str = "hermes-voice:external-monitor", ttl: int = 90):
        self.redis = redis_client
        self.key = key
        self.state_key = key + ":state"
        self.ttl = ttl
        self.token = secrets.token_hex(24)
        self.owned = False
        self.just_claimed = False

    async def claim(self) -> bool:
        try:
            self.just_claimed = False
            if self.owned:
                renewed = await self.redis.eval(
                    'if redis.call("GET", KEYS[1]) == ARGV[1] then '
                    'return redis.call("EXPIRE", KEYS[1], ARGV[2]) else return 0 end',
                    1, self.key, self.token, self.ttl,
                )
                self.owned = bool(renewed)
                return self.owned
            self.owned = bool(await self.redis.set(self.key, self.token, ex=self.ttl, nx=True))
            self.just_claimed = self.owned
            return self.owned
        except Exception:  # RedisError subclasses vary across redis-py versions.
            self.owned = False
            return False

    async def current_down(self) -> bool:
        try:
            return await self.redis.get(self.state_key) == b"down"
        except Exception:
            # Ignore unavailable history; event dedup still fails closed below.
            return False

    async def accept_event(self, event: str) -> bool:
        if event not in {"down", "recovered"} or not self.owned:
            return False
        try:
            return bool(await self.redis.eval(
                'if redis.call("GET", KEYS[1]) ~= ARGV[1] then return 0 end; '
                'if redis.call("GET", KEYS[2]) == ARGV[2] then return 0 end; '
                'redis.call("SET", KEYS[2], ARGV[2]); return 1',
                2, self.key, self.state_key, self.token, event,
            ))
        except Exception:
            return False

    async def release(self) -> None:
        try:
            await self.redis.eval(
                'if redis.call("GET", KEYS[1]) == ARGV[1] then '
                'return redis.call("DEL", KEYS[1]) else return 0 end',
                1, self.key, self.token,
            )
        except Exception:
            pass
        self.owned = False


def replicas_configured() -> bool:
    return int(os.getenv("HERMES_MONITOR_REPLICAS", "1")) > 1
