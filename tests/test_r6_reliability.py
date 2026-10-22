"""R6 security, HMAC integrity, Redis failover and time-bucket regression tests."""
from __future__ import annotations

import asyncio
import json

import pytest

from telegram_miniapp.approval_policy import classify_request, policy_allows
from telegram_miniapp.audit import ApprovalAudit
from telegram_miniapp.distributed_alerts import DistributedAlerts
from telegram_miniapp.health_history import HealthHistory
from telegram_miniapp.monitor_cluster import MonitorLease


def test_risk_policy_is_enforced_server_side(monkeypatch):
    assert classify_request("Remove-Item every file").level == "high"
    assert classify_request("โอนเงิน").level == "high"
    assert classify_request("write notes").level == "medium"
    assert classify_request("please describe the weather").level == "review"
    monkeypatch.setenv("HERMES_APPROVAL_MAX_RISK", "review")
    assert policy_allows("review")
    assert not policy_allows("medium")
    assert not policy_allows("high")
    monkeypatch.setenv("HERMES_APPROVAL_MAX_RISK", "bogus")
    assert not policy_allows("review")
    assert not policy_allows("unknown")


def test_hmac_chain_detects_record_tampering(tmp_path):
    path = tmp_path / "approved.jsonl"
    audit = ApprovalAudit(path, "secret-for-tests")
    audit.append("requested", "high", "delete SECRET_DATA")
    audit.append("approved", "high", "delete SECRET_DATA")
    assert audit.verify_integrity()
    data = path.read_text(encoding="utf-8")
    assert "SECRET_DATA" not in data
    assert "secret-for-tests" not in data
    records = [json.loads(line) for line in data.splitlines()]
    records[0]["decision"] = "denied"
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
    assert not audit.verify_integrity()
    with pytest.raises(RuntimeError):
        audit.append("approved", "high", "delete SECRET_DATA")
    with pytest.raises(RuntimeError):
        ApprovalAudit(path, "secret-for-tests")


def test_health_analytics_hour_day_and_gemini_timeline():
    history = HealthHistory(size=20)
    history.add(api=True, capabilities=True, telegram=True, at=1735689600)
    history.add(api=False, capabilities=True, telegram=False, at=1735689660)
    history.add(api=True, capabilities=True, telegram=True, at=1735776000)
    history.record_gemini(True, at=1735689601)
    history.record_gemini(False, at=1735689650)
    snap = history.snapshot()
    assert snap["total_samples"] == 3
    assert len(snap["hourly"]) == 2
    assert len(snap["daily"]) == 2
    assert snap["daily"][0]["ready_percent"] == 50.0
    assert snap["daily"][0]["telegram_percent"] == 50.0
    assert any(event["component"] == "gemini" for event in snap["timeline"])
    assert snap["gemini_connected"] is False


class FakeRedis:
    """Minimal Redis eval emulator for atomic pending-alert scripts, not a real Redis test."""

    def __init__(self):
        self.values: dict[str, str] = {}
        self.queues: dict[str, list[str]] = {}
        self.unavailable = False

    async def eval(self, script, keys, *args):
        if self.unavailable:
            raise ConnectionError("Simulated Redis failure")
        if keys == 3:  # atomic event transition + RPUSH
            leader, state, pending, token, event, item, max_count = args
            if self.values.get(leader) != token or self.values.get(state) == event:
                return 0
            queue = self.queues.setdefault(pending, [])
            if len(queue) >= int(max_count):
                return 0
            queue.append(item)
            self.values[state] = event
            return 1
        leader, pending, token = args[:3]
        if self.values.get(leader) != token:
            return None if "LINDEX" in script and len(args) == 3 else 0
        queue = self.queues.setdefault(pending, [])
        if len(args) == 3:
            return queue[0].encode("utf-8") if queue else None
        _leader, _pending, _token, old, new = args
        if not queue or queue[0] != old:
            return 0
        if new == "":
            queue.pop(0)
        else:
            queue[0] = new
        return 1


def test_redis_durable_retry_and_failover(monkeypatch):
    async def scenario():
        redis = FakeRedis()
        first = MonitorLease(redis)
        first.owned = True
        redis.values[first.key] = first.token
        q1 = DistributedAlerts(first)
        assert await q1.enqueue("down", now=100)
        assert not await q1.enqueue("down", now=101)
        assert len(redis.queues[q1.key]) == 1

        async def throttle(*_args):
            return False, 8

        import telegram_miniapp.distributed_alerts as module

        monkeypatch.setattr(module, "deliver_alert", throttle)
        cfg = type("Config", (), {"bot_token": "bot", "owner_id": 123})()
        assert not await q1.drain(None, cfg, now=100)
        assert json.loads(redis.queues[q1.key][0])["attempts"] == 1
        # The first monitor stops before acknowledging the message.
        second = MonitorLease(redis)
        second.owned = True
        redis.values[second.key] = second.token
        q2 = DistributedAlerts(second)
        async def delivered(*_args):
            return True, 0

        monkeypatch.setattr(module, "deliver_alert", delivered)
        assert not await q1.drain(None, cfg, now=110)  # old leader fenced out
        assert await q2.drain(None, cfg, now=110)
        assert redis.queues[q2.key] == []
        assert await q2.enqueue("recovered", now=111)
        redis.unavailable = True
        assert not await q2.drain(None, cfg, now=111)
        redis.unavailable = False
        assert await q2.drain(None, cfg, now=111)
        assert redis.queues[q2.key] == []

    asyncio.run(scenario())
