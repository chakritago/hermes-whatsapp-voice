"""Minimal, privacy-conscious audit trail for Hermes tool approvals.

Stores decision, risk and a keyed HMAC of the request. Never stores request body,
voice transcripts, Telegram initData, API keys or recovery handles. Audit is
best-effort: if storage fails, tool execution fails closed.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from collections import deque
from pathlib import Path

VALID = frozenset({"requested", "approved", "denied", "expired", "blocked"})


class ApprovalAudit:
    def __init__(self, path: Path, secret: str, max_recent: int = 40):
        if not secret:
            raise ValueError("Audit requires a non-empty server-side signing secret")
        self.path = path
        self.secret = secret.encode("utf-8")
        self.recent: deque[dict] = deque(maxlen=max_recent)
        self._load()

    def _load(self):
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    if row.get("decision") in VALID and "request_tag" in row:
                        self.recent.append(row)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError):
            # Do not execute tools if audit history is corrupt/unavailable.
            raise RuntimeError("Approval audit log could not be validated") from None

    def append(self, decision: str, risk: str, request: str) -> dict:
        if decision not in VALID or risk not in {"high", "medium", "review"}:
            raise ValueError("Invalid decision/risk")
        tag = hmac.new(self.secret, request.encode("utf-8"), hashlib.sha256).hexdigest()[:20]
        row = {"at": int(time.time()), "decision": decision,
               "risk": risk, "request_tag": tag}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Restrict newly-created files on POSIX. On Windows configure NTFS ACL.
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        fd = os.open(self.path, flags, 0o600)
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise RuntimeError("Unable to durably audit approval") from exc
        self.recent.append(row)
        return row

    def history(self) -> list[dict]:
        return list(self.recent)


def audit_path() -> Path:
    explicit = os.getenv("HERMES_APPROVAL_AUDIT_FILE")
    if explicit:
        return Path(explicit).expanduser()
    base = Path(os.getenv("LOCALAPPDATA") or Path.home() / ".hermes")
    return base / "HermesTelegramVoice" / "approvals.jsonl"
