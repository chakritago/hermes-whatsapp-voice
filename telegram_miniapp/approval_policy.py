"""Deterministic *advisory* tool-risk labelling. All Hermes tools still require approval."""
from __future__ import annotations

import re
from dataclasses import dataclass

HIGH = re.compile(
    r"\b(delete|remove|wipe|format|shutdown|reboot|restart|disable|drop|truncate|"
    r"transfer|pay|purchase|deploy|push|publish|install|uninstall|kill|erase|"
    r"rm\s+-rf|del\s+/[qfs])\b|"
    r"ลบ|ปิดเครื่อง|รีสตาร์ต|โอนเงิน|ชำระเงิน|ติดตั้ง|ถอนการติดตั้ง|"
    r"เผยแพร่|ล้างข้อมูล|ปิดระบบ",
    re.IGNORECASE,
)
MEDIUM = re.compile(
    r"\b(write|edit|update|create|send|email|message|modify|save|upload|"
    r"rename|move|execute|run|change)\b|"
    r"แก้ไข|เปลี่ยน|เขียน|สร้าง|ส่งข้อความ|อัปโหลด|ย้าย|บันทึก|สั่งงาน",
    re.IGNORECASE,
)

@dataclass(frozen=True)
class Risk:
    level: str
    reason: str
    requires_approval: bool = True


def classify_request(text: str) -> Risk:
    """Advisory label only; ambiguity never lowers the approval requirement."""
    if HIGH.search(text):
        return Risk("high", "คำขออาจเปลี่ยนแปลงระบบ ข้อมูล หรือมีผลทางการเงิน")
    if MEDIUM.search(text):
        return Risk("medium", "คำขออาจแก้ไขข้อมูลหรือเรียกใช้งานเครื่องมือ")
    return Risk("review", "ตรวจสอบคำสั่งให้ครบก่อนอนุมัติ; ระบบไม่ยืนยันว่าเป็นการอ่านอย่างเดียว")
