"""Bounded non-sensitive health samples for the owner-only dashboard."""
from __future__ import annotations

import time
from collections import deque


class HealthHistory:
    def __init__(self, size: int = 288):
        self.samples: deque[dict] = deque(maxlen=size)

    def add(self, *, api: bool, capabilities: bool, telegram: bool, at: int | None = None) -> None:
        self.samples.append({
            "at": int(time.time() if at is None else at),
            "api": bool(api), "capabilities": bool(capabilities), "telegram": bool(telegram),
            "ready": bool(api and capabilities and telegram),
        })

    def snapshot(self) -> dict:
        samples = list(self.samples)
        ready = sum(1 for item in samples if item["ready"])
        changes = sum(samples[i]["ready"] != samples[i - 1]["ready"]
                      for i in range(1, len(samples)))
        return {"samples": samples, "ready_samples": ready,
                "total_samples": len(samples), "transitions": changes,
                "ready_percent": round(ready / len(samples) * 100, 1) if samples else None}
