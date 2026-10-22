"""Offline resilience and Windows supervisor tests, with no real network calls."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from telegram_miniapp.alerts import AlertQueue, AlertState, drain_queue
from telegram_miniapp.readiness import HealthSnapshot, Readiness, check_readiness
from telegram_miniapp.supervisor import GatewaySupervisor, SupervisedProcess, gateway_command


class FakeReply:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, **_kwargs):
        return self.data


class FakeSession:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[url]

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses[url]


def test_persistent_alert_queue_and_retry_after(tmp_path):
    queue = AlertQueue(tmp_path / "alerts.json")
    queue.enqueue("down", now=100)
    url = "https://api.telegram.org/bot1:test/sendMessage"
    config = SimpleNamespace(bot_token="1:test", owner_id=123)
    throttle = FakeSession({url: FakeReply({"ok": False, "parameters": {"retry_after": 42}}, 429)})
    assert not asyncio.run(drain_queue(queue, throttle, config, now=100))
    assert queue.items[0].next_attempt_at == 142
    assert not asyncio.run(drain_queue(queue, throttle, config, now=120))
    assert len(throttle.calls) == 1
    assert AlertQueue(queue.path).items[0].attempts == 1
    queue.enqueue("recovered", now=120)
    delivered = FakeSession({url: FakeReply({"ok": True})})
    assert asyncio.run(drain_queue(queue, delivered, config, now=142))
    assert asyncio.run(drain_queue(queue, delivered, config, now=143))
    assert AlertQueue(queue.path).items == []
    assert delivered.calls[0][1]["json"]["chat_id"] == 123


def test_alert_queue_corruption_and_debounce(tmp_path):
    file = tmp_path / "queue.json"
    file.write_text("not json")
    queue = AlertQueue(file)
    assert not queue.items
    with pytest.raises(ValueError):
        queue.enqueue("unknown")
    state = AlertState()
    assert state.observe(False, now=1) is None
    assert state.observe(False, now=2) is None
    assert state.observe(False, now=3) == "down"
    assert state.observe(False, now=4) is None
    assert state.observe(True, now=5) == "recovered"


def test_readiness_api_capabilities_bot():
    config = SimpleNamespace(hermes_url="http://127.0.0.1:8642", hermes_key="secret", bot_token="1:test")
    urls = [
        "http://127.0.0.1:8642/health",
        "http://127.0.0.1:8642/v1/capabilities",
        "https://api.telegram.org/bot1:test/getMe",
    ]
    client = FakeSession(dict(zip(urls, [
        FakeReply({"status": "ok"}),
        FakeReply({"features": {"run_submission": True}}),
        FakeReply({"ok": True, "result": {"is_bot": True}}),
    ])))
    assert asyncio.run(check_readiness(client, config)).ready
    assert client.calls[1][1]["headers"]["Authorization"] == "Bearer secret"
    assert not asyncio.run(check_readiness(FakeSession(dict(zip(urls, [
        FakeReply({"status": "ok"}),
        FakeReply({"features": {"run_submission": False}}),
        FakeReply({"ok": True, "result": {"is_bot": True}}),
    ]))), config)).gateway_ready
    snap = HealthSnapshot()
    assert snap.public(now=100)[0] == 503
    snap.last = Readiness(True, True, True, checked_at=100)
    assert snap.public(now=101)[0] == 200
    assert snap.public(now=300)[0] == 503


def test_service_supervises_only_owned_children(monkeypatch, tmp_path):
    import telegram_miniapp.supervisor as mod

    monkeypatch.setenv("HERMES_SERVICE_LOG_DIR", str(tmp_path))
    children = []

    class Proc:
        def __init__(self, *_args, **_kwargs):
            self.running = True
            children.append(self)

        def poll(self):
            return None if self.running else 1

        def terminate(self):
            self.running = False

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(mod.subprocess, "Popen", Proc)
    monkeypatch.setattr(mod, "_probe", lambda *_args, **_kwargs: False)
    supervisor = GatewaySupervisor(["miniapp"], ["hermes", "gateway"])
    assert supervisor.tick(now=0)
    for clock in (66, 88, 110):
        supervisor.tick(now=clock)
    assert len(children) == 2
    assert all(not child.running for child in children)
    supervisor.stop()
    assert gateway_command("hermes gateway") == ["hermes", "gateway"]
    assert gateway_command(None) == []
    with pytest.raises(ValueError):
        gateway_command("hermes\ngateway")


def test_supervisor_child_crash_restart(monkeypatch, tmp_path):
    import telegram_miniapp.supervisor as mod

    monkeypatch.setenv("HERMES_SERVICE_LOG_DIR", str(tmp_path))
    class Proc:
        def __init__(self, *_args, **_kwargs):
            self.running = True

        def poll(self):
            return None if self.running else 1

        def terminate(self):
            self.running = False

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(mod.subprocess, "Popen", Proc)
    proc = SupervisedProcess(["miniapp"])
    assert proc.tick(now=0)
    old = proc.process
    old.running = False
    assert proc.tick(now=10)
    assert proc.process is not old
    proc.close()
