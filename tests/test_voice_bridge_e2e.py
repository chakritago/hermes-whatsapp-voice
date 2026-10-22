"""Offline end-to-end WebSocket integration with simulated Gemini and Hermes servers."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from telegram_miniapp import server

TOKEN = "111111:fake"
OWNER = 9981


def init_data() -> str:
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": OWNER, "first_name": "QA"})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


def test_end_to_end_owner_audio_and_transcript(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_APPROVAL_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    observed = []

    async def fake_gemini(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        setup = await ws.receive_json()
        observed.append(setup)
        await ws.send_json({"setupComplete": {}})
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            packet = json.loads(message.data)
            if "realtimeInput" in packet:
                observed.append(packet)
                await ws.send_json({
                    "serverContent": {
                        "modelTurn": {"parts": [{
                            "inlineData": {"data": base64.b64encode(b"\x00\x20" * 20).decode()}
                        }]},
                        "outputTranscription": {"text": "Hello from the simulated Gemini voice"},
                    }
                })
                break
        await ws.close()
        return ws

    async def scenario():
        fake = web.Application()
        fake.router.add_get("/gemini", fake_gemini)
        async with TestServer(fake) as gemini:
            monkeypatch.setattr(server, "GEMINI_ENDPOINT", str(gemini.make_url("/gemini")).replace("http:", "ws:"))
            config = server.Config(TOKEN, OWNER, "fake-gemini-key", "fake-hermes-key",
                                   public_url="https://voice.example.com", alerts_enabled=False)
            async with TestServer(server.create_app(config)) as proxy, TestClient(proxy) as client:
                ws = await client.ws_connect("/ws", headers={"Origin": "https://voice.example.com"})
                await ws.send_json({"type": "auth", "init_data": init_data()})
                ready = await ws.receive_json(timeout=4)
                assert ready["type"] == "ready"
                assert ready["resume_ticket"]
                await ws.send_bytes(b"\x00\x00" * 160)
                audio = await ws.receive(timeout=4)
                assert audio.type == WSMsgType.BINARY and audio.data == b"\x00\x20" * 20
                transcript = await ws.receive_json(timeout=4)
                assert transcript["type"] == "transcript"
                assert transcript["role"] == "assistant"
                await ws.close()

    asyncio.run(scenario())
    assert observed[0]["setup"]["tools"][0]["functionDeclarations"][0]["name"] == "ask_hermes"
    assert observed[1]["realtimeInput"]["audio"]["mimeType"] == "audio/pcm;rate=16000"
    assert base64.b64decode(observed[1]["realtimeInput"]["audio"]["data"]) == b"\x00\x00" * 160


def test_end_to_end_socket_reconnect_reuses_ticket(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_APPROVAL_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    async def fake_gemini(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.receive_json()
        await ws.send_json({"setupComplete": {}})
        async for _ in ws:
            pass
        return ws

    async def scenario():
        fake = web.Application()
        fake.router.add_get("/gemini", fake_gemini)
        async with TestServer(fake) as gemini:
            monkeypatch.setattr(server, "GEMINI_ENDPOINT", str(gemini.make_url("/gemini")).replace("http:", "ws:"))
            config = server.Config(TOKEN, OWNER, "fake-gemini-key", "fake-hermes-key",
                                   public_url="https://voice.example.com", alerts_enabled=False)
            async with TestServer(server.create_app(config)) as proxy, TestClient(proxy) as client:
                first = await client.ws_connect("/ws", headers={"Origin": "https://voice.example.com"})
                await first.send_json({"type": "auth", "init_data": init_data()})
                initial = await first.receive_json(timeout=4)
                assert initial["type"] == "ready"
                await first.close()
                for attempt in range(12):
                    second = await client.ws_connect("/ws", headers={"Origin": "https://voice.example.com"})
                    await second.send_json({
                        "type": "auth", "init_data": init_data(), "resume_ticket": initial["resume_ticket"],
                    })
                    again = await second.receive_json(timeout=4)
                    if again["type"] == "ready":
                        assert again["restored"] is True
                        assert again["resume_ticket"] == initial["resume_ticket"]
                        await second.send_json({"type": "stop"})
                        await second.close()
                        return
                    await second.close()
                    await asyncio.sleep(.05)
                raise AssertionError("Previous WebSocket was not released in time")

    asyncio.run(scenario())
