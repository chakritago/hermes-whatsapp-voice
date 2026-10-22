"""Supervise only subprocesses owned by the optional Windows Service.

Hung processes are restarted after consecutive failed local readiness probes,
with a grace period and bounded backoff. Never terminate a pre-existing gateway.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse


def gateway_command(raw: str | None) -> list[str]:
    if not raw:
        return []
    if "\n" in raw or "\r" in raw:
        raise ValueError("Gateway commands may not contain control characters")
    if raw.lstrip().startswith("["):
        args = json.loads(raw)
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise ValueError("JSON gateway command must be an array of strings")
    else:
        args = shlex.split(raw, posix=os.name != "nt")
        if os.name == "nt":
            args = [arg[1:-1] if arg.startswith('"') and arg.endswith('"') else arg for arg in args]
    if not args or not args[0] or any("\n" in arg or "\r" in arg for arg in args):
        raise ValueError("HERMES_GATEWAY_COMMAND must be an executable and arguments, not a shell script")
    return args


def _log_file():
    root = Path(os.getenv("HERMES_SERVICE_LOG_DIR") or Path(os.getenv("PROGRAMDATA") or Path.home()) / "HermesTelegramVoice")
    root.mkdir(parents=True, exist_ok=True)
    return open(root / "service-children.log", "ab", buffering=0)


def _probe(url: str, *, key: str | None = None, require_capabilities: bool = False) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        return False  # Only our local child processes may be restarted on probe failure.
    headers = {"Authorization": "Bearer " + key} if key else {}
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=4) as reply:
            body = json.load(reply)
            if require_capabilities:
                features = body.get("features", {})
                return reply.status == 200 and features.get("run_submission") is True
            return reply.status == 200 and body.get("status") == "ok" if url.endswith("/health") else reply.status == 200
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError):
        return False


@dataclass
class SupervisedProcess:
    argv: list[str]
    process: subprocess.Popen | None = None
    restarts: int = 0
    next_start_at: float = 0
    started_at: float = 0
    consecutive_failures: int = 0
    _log: object | None = field(default=None, repr=False)

    def tick(self, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if self.process is not None and self.process.poll() is None:
            if now - self.started_at > 300:
                self.restarts = 0
            return True
        if now < self.next_start_at:
            return False
        self.restarts += 1
        self.next_start_at = now + min(300, 2 ** min(self.restarts - 1, 8))
        try:
            if self._log is None:
                self._log = _log_file()
            self.process = subprocess.Popen(
                self.argv, shell=False, stdin=subprocess.DEVNULL, stdout=self._log,
                stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                close_fds=True,
            )
            self.started_at = now
            self.consecutive_failures = 0
        except (OSError, ValueError):
            self.process = None
            return False
        return True

    def stop(self) -> None:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None

    def close(self) -> None:
        self.stop()
        if self._log:
            self._log.close()
            self._log = None


def worker_commands() -> tuple[list[str], list[str]]:
    executable = os.getenv("HERMES_MINIAPP_PYTHON", "")
    if not executable or not Path(executable).is_absolute() or not Path(executable).is_file():
        raise ValueError("Set HERMES_MINIAPP_PYTHON to an absolute path to the venv python.exe")
    return [executable, "-m", "telegram_miniapp.server"], gateway_command(
        os.getenv("HERMES_GATEWAY_COMMAND")
    )


class GatewaySupervisor:
    """Owns Mini App child and optionally a Hermes gateway child."""

    def __init__(self, miniapp: list[str], gateway: list[str] | None = None):
        self.miniapp = SupervisedProcess(miniapp)
        self.gateway = SupervisedProcess(gateway) if gateway else None
        self.last_probe = 0.0

    def tick(self, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        mini_ok = self.miniapp.tick(now=now)
        gateway_ok = self.gateway.tick(now=now) if self.gateway else True
        if now - self.last_probe < 20:
            return mini_ok and gateway_ok
        self.last_probe = now
        checks = [
            (self.miniapp, "http://127.0.0.1:" + os.getenv("MINIAPP_PORT", "8788") + "/healthz", None, False),
        ]
        if self.gateway:
            base = os.getenv("HERMES_API_URL", "http://127.0.0.1:8642").rstrip("/")
            checks.append((self.gateway, base + "/v1/capabilities", os.getenv("API_SERVER_KEY"), True))
        for child, url, key, caps in checks:
            if child.process is None or child.process.poll() is not None:
                continue
            if now - child.started_at < 45:
                continue
            if _probe(url, key=key, require_capabilities=caps):
                child.consecutive_failures = 0
            else:
                child.consecutive_failures += 1
                if child.consecutive_failures >= 3:
                    child.stop()  # Only the Popen handle we created, never other processes.
                    child.consecutive_failures = 0
                    child.next_start_at = now + min(300, 2 ** min(child.restarts, 8))
        return mini_ok and gateway_ok

    def stop(self) -> None:
        self.miniapp.close()
        if self.gateway:
            self.gateway.close()
