"""Authenticated Telegram Mini App -> Gemini Live -> Hermes bridge.

Serve behind HTTPS reverse proxy in production; the API keys never reach the browser.
Run: python -m telegram_miniapp.server
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web

LOG = logging.getLogger("hermes.telegram_voice")
GEMINI_ENDPOINT = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
MAX_AUDIO_FRAME = 65536
MAX_SESSION_SECONDS = 900
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    gemini_key: str
    hermes_key: str
    hermes_url: str = "http://127.0.0.1:8642"
    model: str = "gemini-3.8-live"
    voice: str = "Kore"
    public_url: str = ""

    @classmethod
    def from_env(cls) -> Config:
        values = {
            "bot_token": os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            "owner_id": os.getenv("TELEGRAM_OWNER_ID", "").strip(),
            "gemini_key": os.getenv("GOOGLE_API_KEY", "").strip()
            or os.getenv("GEMINI_API_KEY", "").strip(),
            "hermes_key": os.getenv("API_SERVER_KEY", "").strip(),
        }
        if not all(values.values()):
            raise ValueError(
                "TELEGRAM_BOT_TOKEN, TELEGRAM_OWNER_ID, GOOGLE_API_KEY/GEMINI_API_KEY "
                "and API_SERVER_KEY are required"
            )
        try:
            owner = int(values["owner_id"])
        except ValueError as exc:
            raise ValueError("TELEGRAM_OWNER_ID must be an integer") from exc
        if owner <= 0:
            raise ValueError("TELEGRAM_OWNER_ID must be positive")
        return cls(
            bot_token=values["bot_token"],
            owner_id=owner,
            gemini_key=values["gemini_key"],
            hermes_key=values["hermes_key"],
            hermes_url=os.getenv("HERMES_API_URL", "http://127.0.0.1:8642").rstrip("/"),
            model=os.getenv("GEMINI_LIVE_MODEL", "gemini-3.8-live"),
            voice=os.getenv("GEMINI_VOICE", "Kore"),
            public_url=os.getenv("MINIAPP_PUBLIC_URL", "").rstrip("/"),
        )


def validate_init_data(raw: str, token: str, owner_id: int, *, now: int | None = None) -> dict:
    """Check Telegram's bot-token HMAC, freshness, uniqueness and the owner's ID."""
    if not raw or len(raw) > 16384:
        raise ValueError("missing or oversized Telegram initData")
    pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("duplicate Telegram initData fields")
    received = fields.pop("hash", "")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", received):
        raise ValueError("invalid Telegram initData hash")
    check_string = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received.lower()):
        raise ValueError("Telegram initData signature mismatch")
    try:
        auth_date = int(fields["auth_date"])
        user = json.loads(fields["user"])
        user_id = int(user["id"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Telegram user/auth_date") from exc
    current = int(time.time()) if now is None else now
    if auth_date > current + 60 or current - auth_date > 86400:
        raise ValueError("expired or future-dated Telegram initData")
    if user_id != owner_id:
        raise ValueError("unauthorized Telegram user")
    return user


def setup_message(config: Config) -> dict:
    return {
        "setup": {
            "model": "models/" + config.model.removeprefix("models/"),
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {
                    "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": config.voice}}
                },
            },
            "systemInstruction": {
                "parts": [{
                    "text": (
                        "You are Hermes's real-time voice interface in a Telegram Mini App. "
                        "Speak naturally and concisely in the user's language, including Thai. "
                        "Use ask_hermes for tasks requiring tools, memory, files or real-world actions. "
                        "Never claim a Hermes task was completed unless the tool confirms it."
                    )
                }]
            },
            "inputAudioTranscription": {},
            "outputAudioTranscription": {},
            "realtimeInputConfig": {
                "automaticActivityDetection": {"silenceDurationMs": 600},
                "activityHandling": "START_OF_ACTIVITY_INTERRUPTS",
            },
            "tools": [{
                "functionDeclarations": [{
                    "name": "ask_hermes",
                    "description": "Ask Hermes Agent to perform a task or answer using its own tools.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "request": {"type": "string", "description": "User's task or question"}
                        },
                        "required": ["request"],
                    },
                }]
            }],
        }
    }


async def run_hermes(
    session: ClientSession, config: Config, request: str, session_id: str
) -> str:
    """Runs a Hermes turn and stops it if this coroutine is cancelled/times out."""
    run_id = None
    finished = False
    headers = {"Authorization": "Bearer " + config.hermes_key}
    try:
        async with session.post(
            config.hermes_url + "/v1/runs",
            headers=headers,
            json={"input": request[:4000], "session_id": session_id},
            timeout=ClientTimeout(total=20),
        ) as response:
            response.raise_for_status()
            payload = await response.json()
        run_id = payload["run_id"]
        deadline = asyncio.get_running_loop().time() + 60
        while asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(1)
            try:
                async with session.get(
                    config.hermes_url + "/v1/runs/" + run_id,
                    headers=headers,
                    timeout=ClientTimeout(total=10),
                ) as response:
                    response.raise_for_status()
                    status = await response.json()
            except (ClientError, asyncio.TimeoutError):
                continue
            if status.get("status") in TERMINAL:
                finished = True
                if status["status"] != "completed":
                    return "Hermes task " + status["status"] + ": " + str(status.get("error", ""))
                return str(status.get("output", ""))[:8000] or "Hermes completed with no text output."
        return "Hermes timed out. The task was not confirmed complete."
    except (ClientError, KeyError, asyncio.TimeoutError, ValueError) as exc:
        LOG.warning("Hermes run failed: %s", type(exc).__name__)
        return "Hermes API unavailable or returned an invalid response."
    finally:
        if run_id and not finished:
            try:
                async with session.post(
                    config.hermes_url + "/v1/runs/" + run_id + "/stop",
                    headers=headers,
                    timeout=ClientTimeout(total=5),
                ) as response:
                    await response.read()
            except (ClientError, asyncio.TimeoutError):
                pass


def origin_allowed(origin: str | None, public_url: str) -> bool:
    if not origin or not public_url:
        return False
    source = urlparse(origin)
    target = urlparse(public_url)
    return source.scheme == target.scheme == "https" and source.netloc == target.netloc


async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(Path(__file__).parent / "web" / "index.html", headers={
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'none'; script-src 'unsafe-inline' https://telegram.org; "
            "style-src 'unsafe-inline'; connect-src 'self' wss:; "
            "img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"
        ),
    })


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "telegram-miniapp-voice"})


async def socket_handler(request: web.Request) -> web.StreamResponse:
    config: Config = request.app["config"]
    if not origin_allowed(request.headers.get("Origin"), config.public_url):
        raise web.HTTPForbidden(text="Origin not allowed")
    ws = web.WebSocketResponse(max_msg_size=MAX_AUDIO_FRAME + 1024, heartbeat=20)
    await ws.prepare(request)
    lock: asyncio.Lock = request.app["call_lock"]
    if lock.locked():
        await ws.send_json({"type": "error", "message": "Another voice session is active"})
        await ws.close()
        return ws
    try:
        first = await ws.receive(timeout=10)
        if first.type != WSMsgType.TEXT:
            raise ValueError("Telegram authorization is required")
        auth = json.loads(first.data)
        if auth.get("type") != "auth":
            raise ValueError("invalid auth message")
        user = validate_init_data(auth.get("init_data", ""), config.bot_token, config.owner_id)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError, asyncio.TimeoutError):
        await ws.send_json({"type": "error", "message": "Telegram login invalid or expired"})
        await ws.close(code=1008)
        return ws

    await lock.acquire()
    try:
        await voice_session(ws, config, user)
    finally:
        lock.release()
        await ws.close()
    return ws


async def voice_session(ws: web.WebSocketResponse, config: Config, user: dict) -> None:
    tasks: set[asyncio.Task] = set()
    send_lock = asyncio.Lock()
    session_id = "telegram-voice-" + secrets.token_hex(12)
    async with ClientSession(timeout=ClientTimeout(total=None)) as session:
        try:
            upstream = await session.ws_connect(
                GEMINI_ENDPOINT,
                headers={"x-goog-api-key": config.gemini_key},
                heartbeat=20,
                timeout=15,
                max_msg_size=2**22,
            )
            async with upstream:
                await upstream.send_json(setup_message(config))
                ready = await upstream.receive(timeout=20)
                if ready.type != WSMsgType.TEXT or "setupComplete" not in json.loads(ready.data):
                    raise RuntimeError("Gemini Live did not confirm session setup")
                await ws.send_json({"type": "ready", "user": user.get("first_name", "")})

                async def send_upstream(message: dict) -> None:
                    async with send_lock:
                        if not upstream.closed:
                            await upstream.send_json(message)

                async def relay_microphone() -> None:
                    async for item in ws:
                        if item.type == WSMsgType.BINARY:
                            if len(item.data) > MAX_AUDIO_FRAME or len(item.data) % 2:
                                break
                            await send_upstream({
                                "realtimeInput": {
                                    "audio": {
                                        "data": base64.b64encode(item.data).decode("ascii"),
                                        "mimeType": "audio/pcm;rate=16000",
                                    }
                                }
                            })
                        elif item.type == WSMsgType.TEXT:
                            data = json.loads(item.data)
                            if data.get("type") == "stop":
                                break
                        else:
                            break

                async def handle_call(call: dict) -> None:
                    call_id = call.get("id", "")
                    name = call.get("name", "")
                    args = call.get("args") or {}
                    if name == "ask_hermes" and isinstance(args, dict):
                        question = str(args.get("request", "")).strip()
                        result = (
                            await run_hermes(session, config, question, session_id)
                            if question else "Empty Hermes request."
                        )
                    else:
                        result = "Unknown tool."
                    await send_upstream({
                        "toolResponse": {
                            "functionResponses": [{
                                "id": call_id,
                                "name": name,
                                "response": {"result": result},
                            }]
                        }
                    })

                async def relay_gemini() -> None:
                    async for item in upstream:
                        if item.type != WSMsgType.TEXT:
                            break
                        packet = json.loads(item.data)
                        content = packet.get("serverContent") or {}
                        if content.get("interrupted"):
                            await ws.send_json({"type": "interrupted"})
                        for part in (content.get("modelTurn") or {}).get("parts", []):
                            encoded = (part.get("inlineData") or {}).get("data")
                            if encoded:
                                await ws.send_bytes(base64.b64decode(encoded))
                        for field, kind in (
                            ("inputTranscription", "user"),
                            ("outputTranscription", "assistant"),
                        ):
                            transcript = (content.get(field) or {}).get("text")
                            if transcript:
                                await ws.send_json({
                                    "type": "transcript",
                                    "role": kind,
                                    "text": transcript[:2000],
                                })
                        if packet.get("goAway"):
                            await ws.send_json({
                                "type": "notice",
                                "message": "Gemini session ending; reconnect to continue.",
                            })
                        for call in (packet.get("toolCall") or {}).get("functionCalls", []):
                            if len(tasks) >= 2:
                                continue
                            task = asyncio.create_task(handle_call(call))
                            tasks.add(task)
                            task.add_done_callback(tasks.discard)

                transports = [
                    asyncio.create_task(relay_microphone()),
                    asyncio.create_task(relay_gemini()),
                ]
                try:
                    done, pending = await asyncio.wait(
                        transports, timeout=MAX_SESSION_SECONDS,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for completed in done:
                        completed.result()
                    if not done:
                        await ws.send_json({
                            "type": "notice",
                            "message": "15-minute session limit reached. Start a new conversation.",
                        })
                finally:
                    for task in transports + list(tasks):
                        task.cancel()
                    await asyncio.gather(*transports, *tasks, return_exceptions=True)
        except (ClientError, asyncio.TimeoutError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            LOG.warning("Voice session ended: %s", type(exc).__name__)
            if not ws.closed:
                await ws.send_json({"type": "error", "message": "Voice service unavailable. Try again."})


def create_app(config: Config) -> web.Application:
    app = web.Application(client_max_size=MAX_AUDIO_FRAME + 1024)
    app["config"] = config
    app["call_lock"] = asyncio.Lock()
    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/ws", socket_handler)
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    config = Config.from_env()
    port = int(os.getenv("MINIAPP_PORT", "8788"))
    if not config.public_url.startswith("https://"):
        raise SystemExit("Set MINIAPP_PUBLIC_URL to the public HTTPS address of your reverse proxy")
    # Deliberately binds to loopback; reverse proxy/tunnel publishes HTTPS.
    web.run_app(create_app(config), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
