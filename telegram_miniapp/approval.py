"""Fail-closed per-call Hermes tool approval in the owner-authenticated Mini App.

Every ask_hermes invocation requires explicit tap approval (conservative default:
no semantic classifier can reliably distinguish a read from a dangerous command).
Approvals are per-call, bound to the one validated Telegram WebSocket, expire
quickly and are not reusable across reconnects. A spoken 'yes' is NOT approval.
"""

from __future__ import annotations

import asyncio
import secrets
from dataclasses import dataclass

APPROVAL_TIMEOUT_SECONDS = 45
MAX_PENDING = 1


@dataclass
class PendingApproval:
    future: asyncio.Future[bool]
    request: str


class ApprovalGate:
    def __init__(self) -> None:
        self.pending: dict[str, PendingApproval] = {}

    def new(self, request: str) -> tuple[str, asyncio.Future[bool]]:
        if len(self.pending) >= MAX_PENDING:
            raise RuntimeError("Too many pending approvals")
        token = secrets.token_urlsafe(24)
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self.pending[token] = PendingApproval(future, request)
        return token, future

    def resolve(self, token: str, allow: bool) -> bool:
        entry = self.pending.pop(token, None)
        if entry is None or entry.future.done():
            return False
        entry.future.set_result(allow)
        return True

    async def wait(self, token: str, future: asyncio.Future[bool]) -> bool:
        try:
            return await asyncio.wait_for(future, timeout=APPROVAL_TIMEOUT_SECONDS)
        except TimeoutError:
            return False
        finally:
            self.pending.pop(token, None)

    def reject_all(self) -> None:
        for token, item in list(self.pending.items()):
            self.resolve(token, False)
