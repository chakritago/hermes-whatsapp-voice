"""Independent monitor uses HTTPS-only read-only probes, including down/up transitions."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from telegram_miniapp.alerts import AlertQueue, AlertState
from telegram_miniapp.external_monitor import MonitorConfig, monitor_once, public_probe


class MockReply:
    def __init__(self, payload, status=200):
        self.status, self.payload = status, payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def json(self, **_kwargs):
        return self.payload


class FakeSession:
    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.replies[url]


def test_https_probe_and_fails_on_redirect():
    uri = "https://voice.example.org"
    client = FakeSession({
        uri + "/healthz": MockReply({"ok": True}),
        uri + "/readyz": MockReply({"ready": True}),
    })
    assert asyncio.run(public_probe(client, uri))
    assert all(call[1].get("allow_redirects") is False for call in client.calls)
    bad = FakeSession({
        uri + "/healthz": MockReply({"ok": True}, status=302),
        uri + "/readyz": MockReply({"ready": True}),
    })
    assert not asyncio.run(public_probe(bad, uri))
    with pytest.raises(ValueError):
        asyncio.run(public_probe(client, "http://voice.example.org"))


def test_monitor_config_requires_explicit_independent_host(monkeypatch):
    monkeypatch.setenv("HERMES_MONITOR_URL", "https://voice.example.org")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot")
    monkeypatch.setenv("TELEGRAM_OWNER_ID", "123")
    conf = MonitorConfig.from_env()
    assert conf.url == "https://voice.example.org"
    monkeypatch.setenv("HERMES_MONITOR_URL", "http://voice.example.org")
    with pytest.raises(ValueError):
        MonitorConfig.from_env()
    monkeypatch.setenv("HERMES_MONITOR_URL", "https://user:secret@voice.example.org")
    with pytest.raises(ValueError):
        MonitorConfig.from_env()


def test_external_monitor_down_after_three_and_recovers(monkeypatch, tmp_path):
    config = MonitorConfig("https://voice.example.org", "token", 321)
    state = AlertState()
    queue = AlertQueue(tmp_path / "external.json")
    seen = []

    async def fake_probe(_session, _url):
        return seen[-1]

    async def no_send(_queue, _session, _config):
        return False

    import telegram_miniapp.external_monitor as m

    monkeypatch.setattr(m, "public_probe", fake_probe)
    monkeypatch.setattr(m, "drain_queue", no_send)

    async def scenario():
        for online in (False, False, False, False, True):
            seen.append(online)
            await monitor_once(SimpleNamespace(), config, state, queue)

    asyncio.run(scenario())
    assert [x.event for x in queue.items] == ["down", "recovered"]
