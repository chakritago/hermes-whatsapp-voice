"""Short-lived, owner-bound reconnect tickets for WebSocket and Gemini continuity.

In-memory only: process restarts deliberately invalidate tickets. Do not persist Gemini
resumption handles or Hermes session identifiers in Telegram/browser storage.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

TICKET_TTL_SECONDS = 120
MAX_TICKETS = 8


@dataclass
class Recovery:
    owner_id: int
    hermes_session: str = field(default_factory=lambda: "telegram-voice-" + secrets.token_hex(12))
    gemini_handle: str | None = None
    expires_at: float = 0
    in_use: bool = False


class RecoveryStore:
    def __init__(self):
        self._tickets: dict[str, Recovery] = {}

    def acquire(self, owner_id: int, ticket: str | None, *, now: float | None = None) -> tuple[str, Recovery, bool]:
        now = time.monotonic() if now is None else now
        self.prune(now=now)
        if isinstance(ticket, str) and len(ticket) <= 128:
            recovery = self._tickets.get(ticket)
            if recovery and recovery.owner_id == owner_id and not recovery.in_use:
                recovery.in_use = True
                recovery.expires_at = now + TICKET_TTL_SECONDS
                return ticket, recovery, True
        token = secrets.token_urlsafe(32)
        recovery = Recovery(owner_id=owner_id, expires_at=now + TICKET_TTL_SECONDS, in_use=True)
        self._tickets[token] = recovery
        self.prune(now=now)
        return token, recovery, False

    def release(self, ticket: str, *, stopped: bool = False, now: float | None = None) -> None:
        recovery = self._tickets.get(ticket)
        if stopped:
            self._tickets.pop(ticket, None)
        elif recovery is not None:
            recovery.in_use = False
            recovery.expires_at = (time.monotonic() if now is None else now) + TICKET_TTL_SECONDS

    def prune(self, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        for ticket, recovery in list(self._tickets.items()):
            if not recovery.in_use and recovery.expires_at <= now:
                self._tickets.pop(ticket, None)
        if len(self._tickets) > MAX_TICKETS:
            for ticket, _ in sorted(
                ((t, r) for t, r in self._tickets.items() if not r.in_use),
                key=lambda item: item[1].expires_at,
            )[: len(self._tickets) - MAX_TICKETS]:
                self._tickets.pop(ticket, None)


def resume_setup(setup: dict, handle: str | None) -> dict:
    """Enable Gemini session resumption on every connection, even the first one."""
    result = {"setup": dict(setup["setup"])}
    result["setup"]["sessionResumption"] = {"handle": handle} if handle else {}
    return result


def update_handle(recovery: Recovery, message: dict) -> bool:
    """Keep the latest only when Gemini explicitly marks the handle as resumable."""
    update = message.get("sessionResumptionUpdate") or {}
    handle = update.get("newHandle")
    if update.get("resumable") is True and isinstance(handle, str) and handle:
        recovery.gemini_handle = handle
        return True
    return False
