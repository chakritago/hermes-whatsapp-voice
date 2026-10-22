"""Risk policies, privacy-preserving audit, health statistics and monitor failover."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from telegram_miniapp.approval_policy import classify_request
from telegram_miniapp.audit import ApprovalAudit
from telegram_miniapp.health_history import HealthHistory
from telegram_miniapp.monitor_cluster import MonitorLease
from telegram_miniapp.external_monitor import MonitorConfig, monitor_once
from telegram_miniapp.alerts import AlertQueue, AlertState


def test_approval_policy_never_removes_human_approval():
    for task, expected in (
        ("ลบไฟล์ทั้งหมด", "high"), ("shutdown server", "high"),
        ("edit config", "medium"), ("แก้ไขไฟล์", "medium"),
        ("what time is it", "review"), ("show process list", "review"),
    ):
        risk = classify_request(task)
        assert risk.level == expected
        assert risk.requires_approval is True
        assert risk.reason


def test_safe_audit_no_plaintext_and_persistence(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = ApprovalAudit(path, "only-this-test-key")
    log.append("requested", "high", "delete SECRET-CUSTOMER-PASSWORD")
    log.append("denied", "high", "delete SECRET-CUSTOMER-PASSWORD")
    raw = path.read_text(encoding="utf-8")
    assert "SECRET" not in raw
    assert "only-this-test-key" not in raw
    assert len(ApprovalAudit(path, "only-this-test-key").history()) == 2
    first = json.loads(raw.splitlines()[0])
    assert set(first) == {"at", "decision", "risk", "request_tag"}


def test_audit_corruption_fails_closed(tmp_path):
    path = tmp_path / "audit.jsonl"
    path.write_text("invalid-json", encoding="utf-8")
    import pytest

    with pytest.raises(RuntimeError):
        ApprovalAudit(path, "key")


def test_health_history_bounded_and_stats():
    h = HealthHistory(size=3)
    for tick, status in enumerate([True, True, False, True]):
        h.add(api=status, capabilities=True, telegram=True, at=tick)
    result = h.snapshot()
    assert result["total_samples"] == 3
    assert result["ready_samples"] == 2
    assert result["transitions"] == 2
    assert result["ready_percent"] == 66.7


class FakeRedis:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, *, nx=False, ex=None):
        if nx and key in self.data:
            return False
        self.data[key] = value
        return True

    async def get(self, key):
        value = self.data.get(key)
        return value.encode() if isinstance(value, str) else value

    async def eval(self, script, count, *args):
        if count == 1:
            key, token = args[0], args[1]
            if self.data.get(key) != token:
                return 0
            if "DEL" in script:
                del self.data[key]
            return 1
        leader_key, state_key, token, event = args
        if self.data.get(leader_key) != token or self.data.get(state_key) == event:
            return 0
        self.data[state_key] = event
        return 1


def test_only_one_redis_monitor_leader_and_dedup():
    async def scenario():
        redis = FakeRedis()
        a = MonitorLease(redis)
        b = MonitorLease(redis)
        assert await a.claim()
        assert not await b.claim()
        assert await a.accept_event("down")
        assert not await a.accept_event("down")
        assert await a.current_down()
        assert not await b.accept_event("recovered")
        await a.release()
        assert await b.claim() and b.just_claimed
        assert await b.current_down()
        assert await b.accept_event("recovered")
        assert not await b.accept_event("recovered")
    asyncio.run(scenario())


def test_failover_urls_any_healthy(monkeypatch, tmp_path):
    async def scenario():
        import telegram_miniapp.external_monitor as module

        async def check(_session, url):
            return url.endswith("backup.example.org")

        async def suppress(_q, _s, _c):
            return False

        monkeypatch.setattr(module, "public_probe", check)
        monkeypatch.setattr(module, "drain_queue", suppress)
        config = MonitorConfig("https://primary.example.org", "testbot", 1,
                               urls=("https://primary.example.org", "https://backup.example.org"))
        state = AlertState()
        queue = AlertQueue(tmp_path / "queue.json")
        assert await monitor_once(SimpleNamespace(), config, state, queue)
        assert state.failures == 0 and not queue.items
    asyncio.run(scenario())
