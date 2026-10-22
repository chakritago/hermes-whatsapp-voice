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
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from aiohttp import ClientError, ClientSession, ClientTimeout, WSMsgType, web

from .alerts import gateway_watch
from .approval import ApprovalGate
from .readiness import HealthSnapshot
from .recovery import Recovery, RecoveryStore, resume_setup, update_handle

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
    alerts_enabled: bool = True

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
            alerts_enabled=os.getenv("GATEWAY_ALERTS_ENABLED", "true").lower() in ("true", "1", "yes"),
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
            except (ClientError, TimeoutError):
                continue
            if status.get("status") in TERMINAL:
                finished = True
                if status["status"] != "completed":
                    return "Hermes task " + status["status"] + ": " + str(status.get("error", ""))
                return str(status.get("output", ""))[:8000] or "Hermes completed with no text output."
        return "Hermes timed out. The task was not confirmed complete."
    except (ClientError, KeyError, TimeoutError, ValueError) as exc:
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
            except (ClientError, TimeoutError):
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
            "default-src 'none'; script-src 'self' https://telegram.org; "
            "style-src 'unsafe-inline'; connect-src 'self' wss:; worker-src 'self'; "
            "img-src 'self' data:; base-uri 'none'; frame-ancestors https://web.telegram.org"
        ),
    })


async def script(request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        Path(__file__).parent / "web" / "app.js",
        headers={"Cache-Control": "no-store", "Content-Type": "application/javascript; charset=utf-8"},
    )


async def adaptive_buffer_script(request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        Path(__file__).parent / "web" / "audio-buffer.js",
        headers={"Cache-Control": "no-store", "Content-Type": "application/javascript; charset=utf-8"},
    )


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "telegram-miniapp-voice"})


async def ready(request: web.Request) -> web.Response:
    status, body = request.app["readiness_snapshot"].public()
    return web.json_response(body, status=status)


async def audio_worklet(request: web.Request) -> web.FileResponse:
    return web.FileResponse(
        Path(__file__).parent / "web" / "audio-worklet.js",
        headers={"Cache-Control": "no-store", "Content-Type": "application/javascript; charset=utf-8"},
    )


async def socket_handler(request: web.Request) -> web.StreamResponse:
    config: Config = request.app["config"]
    if not origin_allowed(request.headers.get("Origin"), config.public_url):
        raise web.HTTPForbidden(text="Origin not allowed")
    ws = web.WebSocketResponse(max_msg_size=MAX_AUDIO_FRAME + 1024, heartbeat=20)
    await ws.prepare(request)
    try:
        first = await ws.receive(timeout=10)
        if first.type != WSMsgType.TEXT:
            raise ValueError("Telegram authorization required")
        auth = json.loads(first.data)
        if not isinstance(auth, dict) or auth.get("type") != "auth":
            raise ValueError("invalid authentication message")
        user = validate_init_data(auth.get("init_data", ""), config.bot_token, config.owner_id)
    except (ValueError, KeyError, TypeError, TimeoutError, json.JSONDecodeError):
        await ws.send_json({"type": "error", "message": "Telegram login invalid or expired"})
        await ws.close(code=1008)
        return ws

    lock: asyncio.Lock = request.app["call_lock"]
    if lock.locked():
        await ws.send_json({"type": "error", "message": "Another voice session is active"})
        await ws.close(code=1008)
        return ws

    # No await between the lock check and acquisition: only one authenticated
    # session can own Gemini/Hermes, including during reconnection handoffs.
    await lock.acquire()
    tickets: RecoveryStore = request.app["recoveries"]
    ticket, recovery, restored = tickets.acquire(user["id"], auth.get("resume_ticket"))
    stopped = False
    try:
        stopped = await voice_session(ws, config, user, ticket, recovery, restored)
    finally:
        tickets.release(ticket, stopped=stopped)
        lock.release()
        await ws.close()
    return ws


async def voice_session(
    ws: web.WebSocketResponse,
    config: Config,
    user: dict,
    ticket: str,
    recovery: Recovery,
    restored: bool,
) -> bool:
    """Bridge one browser socket; a short-lived recovery ticket survives disconnects.

    Hermes session ID is reused across browser reconnects. Gemini's latest
    explicitly resumable handle is used for connection rotations.
    """
    stopped = False
    tasks: dict[str, asyncio.Task] = {}
    approvals = ApprovalGate()
    send_lock = asyncio.Lock()
    current: dict = {"upstream": None}
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MAX_SESSION_SECONDS

    async with ClientSession(timeout=ClientTimeout(total=None)) as session:

        async def connect_live() -> None:
            upstream = await session.ws_connect(
                GEMINI_ENDPOINT,
                headers={"x-goog-api-key": config.gemini_key},
                heartbeat=20,
                timeout=15,
                max_msg_size=2**22,
            )
            try:
                await upstream.send_json(resume_setup(setup_message(config), recovery.gemini_handle))
                ready = await upstream.receive(timeout=20)
                if ready.type != WSMsgType.TEXT or "setupComplete" not in json.loads(ready.data):
                    raise RuntimeError("Gemini Live did not confirm session setup")
            except BaseException:
                await upstream.close()
                raise
            current["upstream"] = upstream

        async def send_upstream(message: dict) -> None:
            async with send_lock:
                upstream = current["upstream"]
                if upstream is not None and not upstream.closed:
                    try:
                        await upstream.send_json(message)
                    except ClientError:
                        pass  # Relay will reconnect; stale microphone frames are not replayed.

        async def relay_microphone() -> None:
            nonlocal stopped
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
                    try:
                        data = json.loads(item.data)
                    except json.JSONDecodeError:
                        break
                    if not isinstance(data, dict):
                        continue
                    if data.get("type") == "stop":
                        stopped = True
                        break
                    if data.get("type") == "approval":
                        # Only the current validated owner socket can confirm.
                        token, allow = data.get("id"), data.get("allow")
                        if isinstance(token, str) and isinstance(allow, bool):
                            approvals.resolve(token, allow)
                    elif data.get("type") == "ping":
                        marker = data.get("id")
                        if isinstance(marker, int) and 0 <= marker <= 2147483647:
                            await ws.send_json({"type": "pong", "id": marker})
                else:
                    break

        async def handle_call(call: dict) -> None:
            call_id = str(call.get("id", ""))
            name = call.get("name", "")
            args = call.get("args") or {}
            if name == "ask_hermes" and isinstance(args, dict):
                question = str(args.get("request", "")).strip()
                if not question:
                    result = "Empty Hermes request."
                else:
                    try:
                        approval_id, future = approvals.new(question)
                        await ws.send_json({
                            "type": "approval_request", "id": approval_id,
                            "request": question[:4000], "expires_seconds": 45,
                        })
                        allowed = await approvals.wait(approval_id, future)
                    except RuntimeError:
                        allowed = False
                    result = (
                        await run_hermes(session, config, question, recovery.hermes_session)
                        if allowed else "Hermes tool request denied or timed out. No action executed."
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
            failures = 0
            while not ws.closed and loop.time() < deadline:
                if current["upstream"] is None or current["upstream"].closed:
                    try:
                        await connect_live()
                    except (ClientError, TimeoutError, RuntimeError, ValueError, json.JSONDecodeError):
                        failures += 1
                        if failures > 4:
                            raise RuntimeError("Gemini could not reconnect") from None
                        await ws.send_json({
                            "type": "status", "phase": "reconnecting",
                            "attempt": failures, "message": "กำลังเชื่อมต่อ Gemini ใหม่",
                        })
                        await asyncio.sleep(min(8, 2 ** (failures - 1)))
                        continue

                try:
                    upstream = current["upstream"]
                    async for item in upstream:
                        if item.type != WSMsgType.TEXT:
                            break
                        packet = json.loads(item.data)
                        update_handle(recovery, packet)
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
                                    "type": "transcript", "role": kind,
                                    "text": transcript[:2000],
                                })
                        for cancel_id in (packet.get("toolCallCancellation") or {}).get("ids", []):
                            task = tasks.get(cancel_id)
                            if task:
                                task.cancel()
                        for call in (packet.get("toolCall") or {}).get("functionCalls", []):
                            call_id = str(call.get("id", ""))
                            if call_id in tasks or len(tasks) >= 2:
                                continue
                            task = asyncio.create_task(handle_call(call))
                            tasks[call_id] = task
                            task.add_done_callback(lambda _, key=call_id: tasks.pop(key, None))
                        # Reconnect proactively on goAway only with a valid handle.
                        if packet.get("goAway") and recovery.gemini_handle:
                            break
                    failures += 1
                except (ClientError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
                    LOG.warning("Gemini connection interrupted: %s", type(exc).__name__)
                    failures += 1
                finally:
                    old = current["upstream"]
                    current["upstream"] = None
                    if old is not None:
                        await old.close()
                if failures > 4:
                    raise RuntimeError("Gemini reconnect limit reached")
                await ws.send_json({
                    "type": "status", "phase": "reconnecting",
                    "attempt": failures,
                    "message": "กำลังกู้คืน Gemini Live Session"
                    if recovery.gemini_handle else "กำลังเริ่ม Gemini Session ใหม่",
                })
                await asyncio.sleep(min(8, 2 ** (failures - 1)))
                # A successful setup below resets the retry budget.
                if current["upstream"] is None:
                    try:
                        await connect_live()
                    except (ClientError, TimeoutError, RuntimeError, ValueError, json.JSONDecodeError):
                        continue
                    failures = 0
                    await ws.send_json({
                        "type": "status",
                        "phase": "restored" if recovery.gemini_handle else "new_session",
                        "message": "เชื่อมต่อ Gemini สำเร็จ",
                    })

        try:
            await connect_live()
            await ws.send_json({
                "type": "ready", "user": user.get("first_name", ""),
                "resume_ticket": ticket, "restored": restored,
                "gemini_resumable": bool(recovery.gemini_handle),
            })
            transports = [
                asyncio.create_task(relay_microphone()),
                asyncio.create_task(relay_gemini()),
            ]
            try:
                done, _pending = await asyncio.wait(
                    transports, timeout=MAX_SESSION_SECONDS, return_when=asyncio.FIRST_COMPLETED,
                )
                for completed in done:
                    completed.result()
                if not done:
                    await ws.send_json({
                        "type": "notice",
                        "message": "ครบ 15 นาทีแล้ว กรุณาเริ่มสนทนาใหม่",
                    })
            finally:
                approvals.reject_all()
                for task in transports + list(tasks.values()):
                    task.cancel()
                await asyncio.gather(*transports, *tasks.values(), return_exceptions=True)
        except (ClientError, TimeoutError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
            LOG.warning("Voice session ended: %s", type(exc).__name__)
            if not ws.closed:
                await ws.send_json({"type": "error", "message": "Voice service unavailable. Try again."})
        finally:
            if current["upstream"] is not None:
                await current["upstream"].close()
    return stopped


async def on_startup(app: web.Application) -> None:
    config: Config = app["config"]
    if config.alerts_enabled:
        app["alert_task"] = asyncio.create_task(gateway_watch(config, snapshot=app["readiness_snapshot"]))


async def on_cleanup(app: web.Application) -> None:
    task = app.get("alert_task")
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def create_app(config: Config) -> web.Application:
    app = web.Application(client_max_size=MAX_AUDIO_FRAME + 1024)
    app["config"] = config
    app["call_lock"] = asyncio.Lock()
    app["recoveries"] = RecoveryStore()
    app["readiness_snapshot"] = HealthSnapshot()
    app.router.add_get("/", index)
    app.router.add_get("/app.js", script)
    app.router.add_get("/audio-worklet.js", audio_worklet)
    app.router.add_get("/audio-buffer.js", adaptive_buffer_script)
    app.router.add_get("/readyz", ready)
    app.router.add_get("/healthz", health)
    app.router.add_get("/ws", socket_handler)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
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
