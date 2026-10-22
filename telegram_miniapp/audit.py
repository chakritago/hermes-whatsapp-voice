"""Privacy-preserving, HMAC-chained owner tool audit.

No raw task text or credential is written. Entries are chained to detect
modification or reordering; an independent protected checkpoint is needed
to detect complete log truncation, so this is not an immutable ledger.
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
            raise ValueError("Audit requires a non-empty signing secret")
        self.path = path
        self.secret = secret.encode("utf-8")
        self.recent: deque[dict] = deque(maxlen=max_recent)
        self.head = "0" * 64
        self.verified_count = 0
        self._load()

    def _seal(self, row: dict) -> str:
        serialized = json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hmac.new(self.secret, serialized.encode("utf-8"), hashlib.sha256).hexdigest()

    def _load(self) -> None:
        rows: list[dict] = []
        try:
            with self.path.open(encoding="utf-8") as handle:
                for line in handle:
                    row = json.loads(line)
                    if not isinstance(row, dict) or row.get("decision") not in VALID:
                        raise ValueError("Invalid audit record")
                    if row.get("risk") not in {"review", "medium", "high"}:
                        raise ValueError("Invalid audit risk")
                    if not isinstance(row.get("request_tag"), str):
                        raise ValueError("Invalid audit request tag")
                    seal = row.get("seal")
                    if seal is None:
                        # Legacy unsealed records remain readable. The first sealed
                        # record anchors all earlier canonical records in the chain.
                        if any("seal" in previous for previous in rows):
                            raise ValueError("Unsealed records after sealed records")
                        self.head = self._seal({"previous": self.head, "legacy": row})
                    else:
                        unsigned = {k: v for k, v in row.items() if k != "seal"}
                        if (
                            not isinstance(seal, str)
                            or row.get("previous") != self.head
                            or not hmac.compare_digest(self._seal(unsigned), seal)
                        ):
                            raise ValueError("Audit chain integrity failure")
                        self.head = seal
                        self.verified_count += 1
                    rows.append(row)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise RuntimeError("Approval audit integrity validation failed") from exc
        self.recent.extend(rows[-self.recent.maxlen:])

    def verify_integrity(self) -> bool:
        try:
            ApprovalAudit(self.path, self.secret.decode("utf-8"))
            return True
        except RuntimeError:
            return False

    def append(self, decision: str, risk: str, request: str) -> dict:
        if decision not in VALID or risk not in {"high", "medium", "review"}:
            raise ValueError("Invalid decision/risk")
        if not self.verify_integrity():
            raise RuntimeError("Audit was modified externally; tool execution is blocked")
        tag = hmac.new(self.secret, request.encode("utf-8"), hashlib.sha256).hexdigest()[:20]
        row = {
            "at": int(time.time()), "decision": decision, "risk": risk,
            "request_tag": tag, "previous": self.head,
        }
        row["seal"] = self._seal(row)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise RuntimeError("Unable to durably audit approval") from exc
        self.head = row["seal"]
        self.verified_count += 1
        self.recent.append(row)
        return row

    def history(self) -> list[dict]:
        return [{k: v for k, v in row.items() if k not in {"previous", "seal"}} for row in self.recent]


def audit_path() -> Path:
    explicit = os.getenv("HERMES_APPROVAL_AUDIT_FILE")
    if explicit:
        return Path(explicit).expanduser()
    base = Path(os.getenv("LOCALAPPDATA") or Path.home() / ".hermes")
    return base / "HermesTelegramVoice" / "approvals.jsonl"
