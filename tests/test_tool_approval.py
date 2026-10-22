"""Owner-confirmation security tests, including mocked full WebSocket -> Hermes path."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from telegram_miniapp import approval, server


def init_data(user_id: int = 321) -> str:
    data = {"auth_date": str(int(time.time())), "user": json.dumps({"id": user_id})}
    secret = hmac.new(b"WebAppData", b"111:fake", hashlib.sha256).digest()
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    data["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(data)


def test_approval_one_shot_and_timeout(monkeypatch):
    async def scenario():
        gate = approval.ApprovalGate()
        token, future = gate.new("delete a folder")
        assert not gate.resolve("wrong-token", True)
        assert gate.resolve(token, False)
        assert not gate.resolve(token, True)
        assert await gate.wait(token, future) is False

        token, future = gate.new("read a file")
        assert gate.resolve(token, True)
        assert await gate.wait(token, future) is True

        monkeypatch.setattr(approval, "APPROVAL_TIMEOUT_SECONDS", 0.01)
        token, future = gate.new("shutdown host")
        assert await gate.wait(token, future) is False
        assert not gate.resolve(token, True)

        token, _ = gate.new("next pending")
        gate.reject_all()
        assert token not in gate.pending

    asyncio.run(scenario())


def test_mocked_gemini_tool_request_cannot_bypass_owner_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_APPROVAL_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    submitted: list[dict] = []
    responses: list[dict] = []

    async def fake_gemini(request):
        sock = web.WebSocketResponse()
        await sock.prepare(request)
        await sock.receive_json()  # setup
        await sock.send_json({"setupComplete": {}})
        await sock.send_json({"toolCall": {"functionCalls": [
            {"id": "tool-1", "name": "ask_hermes", "args": {"request": "delete every backup"}}
        ]}})
        async for item in sock:
            if item.type.name == "TEXT":
                packet = json.loads(item.data)
                if "toolResponse" in packet:
                    responses.append(packet)
                    break
        await sock.close()
        return sock

    async def create_run(request):
        submitted.append(await request.json())
        return web.json_response({"run_id": "run-test"})

    async def get_run(request):
        return web.json_response({"status": "completed", "output": "confirmed"})

    async def scenario():
        fake = web.Application()
        fake.router.add_get("/gemini", fake_gemini)
        fake.router.add_post("/v1/runs", create_run)
        fake.router.add_get("/v1/runs/run-test", get_run)
        async with TestServer(fake) as upstream:
            monkeypatch.setattr(
                server, "GEMINI_ENDPOINT", str(upstream.make_url("/gemini")).replace("http:", "ws:")
            )
            conf = server.Config(
                "111:fake", 321, "gemini", "hermes", hermes_url=str(upstream.make_url("/")).rstrip("/"),
                public_url="https://voice.example.com", alerts_enabled=False,
            )
            async with TestServer(server.create_app(conf)) as local, TestClient(local) as client:
                for allow in (False, True):
                    ws = await client.ws_connect("/ws", headers={"Origin": "https://voice.example.com"})
                    await ws.send_json({"type": "auth", "init_data": init_data()})
                    assert (await ws.receive_json(timeout=3))["type"] == "ready"
                    for _ in range(8):
                        message = await ws.receive_json(timeout=3)
                        if message.get("type") == "approval_request":
                            break
                    assert message["type"] == "approval_request"
                    assert "delete every backup" in message["request"]
                    await ws.send_json({"type": "approval", "id": "wrong", "allow": True})
                    await asyncio.sleep(0.05)
                    assert not submitted
                    await ws.send_json({"type": "approval", "id": message["id"], "allow": allow})
                    for _ in range(30):
                        if len(responses) >= (1 if not allow else 2):
                            break
                        await asyncio.sleep(0.1)
                    await ws.close()
                assert len(submitted) == 1
                assert submitted[0]["input"] == "delete every backup"
                assert len(responses) == 2
                assert "denied" in responses[0]["toolResponse"]["functionResponses"][0]["response"]["result"]
                assert "confirmed" in responses[1]["toolResponse"]["functionResponses"][0]["response"]["result"]

    asyncio.run(scenario())


def test_approval_no_unauthorized_telegram_id(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_APPROVAL_AUDIT_FILE", str(tmp_path / "audit.jsonl"))
    async def fake_gemini(request):
        pytest.fail("unauthorized user must not reach Gemini")
    async def scenario():
        config = server.Config("111:fake", 321, "gemini", "hermes",
                               public_url="https://voice.example.com", alerts_enabled=False)
        async with TestServer(server.create_app(config)) as local, TestClient(local) as client:
            ws = await client.ws_connect("/ws", headers={"Origin": "https://voice.example.com"})
            await ws.send_json({"type": "auth", "init_data": init_data(999)})
            assert (await ws.receive_json(timeout=2))["type"] == "error"
            await ws.close()
    asyncio.run(scenario())
