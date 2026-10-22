"""Explicit Hermes permissions: risk labels never grant tool access."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

HIGH = re.compile(
    r"\b(delete|remove|wipe|format|shutdown|reboot|restart|disable|drop|truncate|"
    r"transfer|pay|purchase|deploy|push|publish|install|uninstall|kill|erase|"
    r"rm\s+-rf|del\s+/[qfs]|Remove-Item|Stop-Service|Set-ExecutionPolicy|"
    r"reg\s+delete|chmod|chown|sudo|curl.{0,20}\|.{0,20}(sh|bash))\b|"
    r"ลบ|ปิดเครื่อง|รีสตาร์ต|โอนเงิน|ชำระเงิน|ติดตั้ง|ถอนการติดตั้ง|"
    r"เผยแพร่|ล้างข้อมูล|ปิดระบบ|เปลี่ยนสิทธิ์",
    re.IGNORECASE,
)
MEDIUM = re.compile(
    r"\b(write|edit|update|create|send|email|message|modify|save|upload|"
    r"rename|move|execute|run|change|download|configure)\b|"
    r"แก้ไข|เปลี่ยน|เขียน|สร้าง|ส่งข้อความ|อัปโหลด|ย้าย|บันทึก|สั่งงาน",
    re.IGNORECASE,
)
LEVELS = {"review": 0, "medium": 1, "high": 2}


@dataclass(frozen=True)
class Risk:
    level: str
    reason: str
    requires_approval: bool = True


def classify_request(text: str) -> Risk:
    """Advisory severity; unclear instructions still require approval."""
    if HIGH.search(text):
        return Risk("high", "คำขออาจเปลี่ยนระบบ ลบข้อมูล เปลี่ยนสิทธิ์ หรือมีผลทางการเงิน")
    if MEDIUM.search(text):
        return Risk("medium", "คำขออาจเปลี่ยนข้อมูลหรือเรียกใช้งานเครื่องมือ")
    return Risk("review", "โปรดตรวจสอบคำขอทั้งหมดก่อนอนุมัติ")


def policy_allows(level: str, *, maximum: str | None = None) -> bool:
    """Hard server-side ceiling, independent of a WebSocket button or Gemini."""
    maximum = maximum or os.getenv("HERMES_APPROVAL_MAX_RISK", "high").lower()
    if level not in LEVELS or maximum not in LEVELS:
        return False
    return LEVELS[level] <= LEVELS[maximum]
