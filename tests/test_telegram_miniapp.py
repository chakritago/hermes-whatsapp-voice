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
