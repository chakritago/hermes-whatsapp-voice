"""Offline tests for the Telegram Mini App addition."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

from telegram_miniapp.server import Config, origin_allowed, setup_message, validate_init_data

TOKEN = "123456:fake-test-token"
OWNER = 321123


def signed_data(user_id=OWNER, auth_date=None, **extra):
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "user": json.dumps({"id": user_id, "first_name": "User"}),
        **extra,
    }
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_owner_authorized():
    assert validate_init_data(signed_data(), TOKEN, OWNER)["id"] == OWNER


@pytest.mark.parametrize(
    "value",
    [
        "",
        "auth_date=1&hash=" + "a" * 64,
        signed_data(user_id=44),
        signed_data(auth_date=1),
        signed_data(auth_date=int(time.time()) + 3600),
        signed_data() + "&auth_date=2",
    ],
)
def test_auth_invalid(value):
    with pytest.raises(ValueError):
        validate_init_data(value, TOKEN, OWNER)


def test_tampering_rejected():
    signed = signed_data()
    with pytest.raises(ValueError, match="signature"):
        validate_init_data(signed.replace("User", "Admin"), TOKEN, OWNER)


def test_origin_enforces_https_and_host():
    url = "https://voice.example.com/app"
    assert origin_allowed("https://voice.example.com", url)
    assert not origin_allowed("http://voice.example.com", url)
    assert not origin_allowed("https://attacker.example", url)
    assert not origin_allowed(None, url)


def test_live_setup_and_hermes_tool():
    config = Config(TOKEN, OWNER, "secret-gemini-key-123", "secret-hermes-key-456")
    setup = setup_message(config)["setup"]
    payload = json.dumps(setup)
    assert setup["model"] == "models/gemini-3.8-live"
    assert setup["tools"][0]["functionDeclarations"][0]["name"] == "ask_hermes"
    assert config.gemini_key not in payload
    assert config.hermes_key not in payload


def test_missing_env_rejected(monkeypatch):
    for name in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "GOOGLE_API_KEY", "GEMINI_API_KEY", "API_SERVER_KEY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="required"):
        Config.from_env()

def test_reconnect_owner_bound_and_hermes_session_continuity():
    from telegram_miniapp.recovery import RecoveryStore

    store = RecoveryStore()
    ticket, recovery, restored = store.acquire(OWNER, None, now=100)
    assert not restored
    session_id = recovery.hermes_session
    store.release(ticket, now=101)
    resumed_ticket, resumed, restored = store.acquire(OWNER, ticket, now=110)
    assert restored and resumed_ticket == ticket
    assert resumed.hermes_session == session_id
    assert resumed.in_use
    another, _, restored = store.acquire(OWNER, ticket, now=111)
    assert another != ticket and not restored
    store.release(ticket, stopped=True)
    _, new_session, restored = store.acquire(OWNER, ticket, now=113)
    assert not restored and new_session.hermes_session != session_id


def test_reconnect_expiry_and_wrong_owner():
    from telegram_miniapp.recovery import RecoveryStore

    store = RecoveryStore()
    ticket, _, _ = store.acquire(OWNER, None, now=100)
    store.release(ticket, now=101)
    _, other, restored = store.acquire(OWNER + 1, ticket, now=102)
    assert not restored and other.owner_id == OWNER + 1
    _, expired, restored = store.acquire(OWNER, ticket, now=300)
    assert not restored and expired.owner_id == OWNER


def test_gemini_session_resumption_only_from_explicit_handle():
    from telegram_miniapp.recovery import Recovery, resume_setup, update_handle

    recovery = Recovery(OWNER)
    config = Config(TOKEN, OWNER, "secret-gemini", "secret-hermes")
    base = setup_message(config)
    assert resume_setup(base, None)["setup"]["sessionResumption"] == {}
    assert not update_handle(recovery, {"sessionResumptionUpdate": {"resumable": False, "newHandle": "bad"}})
    assert recovery.gemini_handle is None
    assert update_handle(recovery, {"sessionResumptionUpdate": {"resumable": True, "newHandle": "resume-key"}})
    resumed = resume_setup(base, recovery.gemini_handle)
    assert resumed["setup"]["sessionResumption"]["handle"] == "resume-key"
    assert "sessionResumption" not in base["setup"]


def test_gateway_alert_threshold_recovery_no_spam():
    from telegram_miniapp.alerts import AlertState

    state = AlertState()
    assert state.observe(False, now=1) is None
    assert state.observe(False, now=2) is None
    assert state.observe(False, now=3) == "down"
    for n in range(4, 50):
        assert state.observe(False, now=n) is None
    assert state.observe(True, now=50) == "recovered"
    assert state.observe(True, now=51) is None
    for n in range(52, 54):
        assert state.observe(False, now=n) is None
    assert state.observe(False, now=54) == "down"


def test_gateway_probe_checks_json_health():
    import asyncio

    from telegram_miniapp.alerts import probe_gateway

    class Reply:
        def __init__(self, status, value):
            self.status = status
            self.value = value

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def json(self, **_kwargs):
            return self.value

    class Client:
        def __init__(self, reply):
            self.reply = reply
            self.request_url = None

        def get(self, url, **_kwargs):
            self.request_url = url
            return self.reply

    client = Client(Reply(200, {"status": "ok"}))
    assert asyncio.run(probe_gateway(client, "http://127.0.0.1:8642/"))
    assert client.request_url == "http://127.0.0.1:8642/health"
    assert not asyncio.run(probe_gateway(Client(Reply(200, {"status": "error"})), "http://host"))
    assert not asyncio.run(probe_gateway(Client(Reply(500, {"status": "ok"})), "http://host"))


def test_miniapp_web_assets_present_and_private_credentials_not_embedded():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "telegram_miniapp" / "web"
    html = (root / "index.html").read_text(encoding="utf-8")
    code = (root / "app.js").read_text(encoding="utf-8")
    for name in ("mute", "outputSelect", "waveform", "visualizer", "speakerAudio"):
        assert f'id="{name}"' in html
    assert "resume_ticket" in code
    assert "TELEGRAM_BOT_TOKEN" not in html + code
    assert "API_SERVER_KEY" not in html + code
    assert "GEMINI_API_KEY" not in html + code


def test_server_owner_only_websocket_rejects_bad_initdata():
    import asyncio

    from aiohttp import WSMsgType
    from aiohttp.test_utils import TestClient, TestServer

    from telegram_miniapp.server import create_app

    async def check():
        config = Config(TOKEN, OWNER, "gemini-test", "hermes-test", public_url="https://voice.example.com", alerts_enabled=False)
        async with TestServer(create_app(config)) as server, TestClient(server) as client:
            ws = await client.ws_connect("/ws", headers={"Origin": "https://voice.example.com"})
            await ws.send_json({"type": "auth", "init_data": signed_data(user_id=OWNER + 2)})
            message = await ws.receive(timeout=2)
            assert message.type == WSMsgType.TEXT
            assert json.loads(message.data)["type"] == "error"
            await ws.close()

    asyncio.run(check())
