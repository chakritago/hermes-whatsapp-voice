"""Bounded, non-sensitive readiness history and sampled uptime analytics."""
from __future__ import annotations

import time
from collections import deque
from datetime import UTC, datetime


class HealthHistory:
    def __init__(self, size: int = 2880):
        self.samples: deque[dict] = deque(maxlen=size)
        self.gemini_events: deque[dict] = deque(maxlen=200)

    def add(self, *, api: bool, capabilities: bool, telegram: bool, at: int | None = None) -> None:
        self.samples.append({
            "at": int(time.time() if at is None else at),
            "api": bool(api), "capabilities": bool(capabilities),
            "telegram": bool(telegram),
            "ready": bool(api and capabilities and telegram),
        })

    def record_gemini(self, connected: bool, *, at: int | None = None) -> None:
        timestamp = int(time.time() if at is None else at)
        if not self.gemini_events or self.gemini_events[-1]["up"] != bool(connected):
            self.gemini_events.append({"at": timestamp, "up": bool(connected)})

    def buckets(self, period: str) -> list[dict]:
        if period not in {"hour", "day"}:
            raise ValueError("period must be hour/day")
        buckets: dict[str, dict] = {}
        for sample in self.samples:
            day = datetime.fromtimestamp(sample["at"], tz=UTC)
            key = day.strftime("%Y-%m-%dT%H:00Z" if period == "hour" else "%Y-%m-%d")
            slot = buckets.setdefault(key, {
                "period": key, "count": 0, "ready": 0, "api": 0,
                "capabilities": 0, "telegram": 0,
            })
            slot["count"] += 1
            for field in ("ready", "api", "capabilities", "telegram"):
                slot[field] += int(sample[field])
        return [
            {
                "period": item["period"], "samples": item["count"],
                **{
                    f"{field}_percent": round(100 * item[field] / item["count"], 1)
                    for field in ("ready", "api", "capabilities", "telegram")
                },
            }
            for item in list(buckets.values())[-72:]
        ]

    def timeline(self) -> list[dict]:
        events: list[dict] = []
        previous: dict | None = None
        for current in self.samples:
            for component, field in (("gateway", "api"), ("telegram", "telegram")):
                if previous is None or previous[field] != current[field]:
                    events.append({"at": current["at"], "component": component, "up": current[field]})
            previous = current
        events.extend({"at": e["at"], "component": "gemini", "up": e["up"]} for e in self.gemini_events)
        return sorted(events, key=lambda e: e["at"])[-25:]

    def snapshot(self) -> dict:
        samples = list(self.samples)
        ready = sum(1 for item in samples if item["ready"])
        changes = sum(samples[i]["ready"] != samples[i - 1]["ready"]
                      for i in range(1, len(samples)))
        return {
            "samples": samples[-288:], "ready_samples": ready,
            "total_samples": len(samples), "transitions": changes,
            "ready_percent": round(ready / len(samples) * 100, 1) if samples else None,
            "hourly": self.buckets("hour"), "daily": self.buckets("day"),
            "timeline": self.timeline(),
            "gemini_connected": self.gemini_events[-1]["up"] if self.gemini_events else None,
        }
