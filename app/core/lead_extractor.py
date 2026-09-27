"""会员信息提取与线索卡片渲染。

手机号获取策略（已与用户确认）：能拿就拿、拿不到用 @用户名。
Telegram 只在对方是「联系人」时才返回手机号，所以顺序是
发送者自带的号码 → 消息文本里的号码 / 微信号 → @用户名 / 用户 ID。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 中国大陆手机号；前后不允许还有数字，避免把长数字串截出来
CN_PHONE = re.compile(r"(?<!\d)(?:\+?86[\s-]?)?(1[3-9]\d{9})(?!\d)")
# 带国际区号的通用号码，如 +639123456789
INTL_PHONE = re.compile(r"(?<!\d)(\+\d{8,15})(?!\d)")
WECHAT = re.compile(
    r"(?:微信|weixin|wechat|vx|v信|威信|加我|加v)[号:：\s]*([A-Za-z][A-Za-z0-9_-]{4,19})",
    re.IGNORECASE,
)
USERNAME = re.compile(r"@([A-Za-z][A-Za-z0-9_]{4,31})")

DEFAULT_CARD_TEMPLATE = (
    "🎯 命中线索：{关键词}\n"
    "👤 {昵称} {用户名}\n"
    "🆔 用户 ID：{用户ID}\n"
    "📞 联系方式：{联系方式}\n"
    "💬 发言：{原文}\n"
    "📍 来源：{来源群}｜{时间}"
)


@dataclass(frozen=True)
class SenderInfo:
    """消息发送者的可见信息。"""

    tg_user_id: int | None = None
    username: str | None = None
    display_name: str | None = None
    phone: str | None = None
    is_bot: bool = False
    # 普通用户才允许进入冷私聊候选；频道 / 匿名身份不能当个人用户处理
    is_user: bool = True
    # Telethon 原始 User 对象带 access_hash 时，当前监听账号持有可用于后续联系的 peer
    has_peer_reference: bool = False


@dataclass(frozen=True)
class ContactInfo:
    """从一条消息里扒到的联系方式。"""

    phones: list[str] = field(default_factory=list)
    wechats: list[str] = field(default_factory=list)
    usernames: list[str] = field(default_factory=list)

    @property
    def primary_phone(self) -> str | None:
        return self.phones[0] if self.phones else None

    @property
    def primary_wechat(self) -> str | None:
        return self.wechats[0] if self.wechats else None

    def dump(self) -> str:
        return json.dumps(
            {
                "phones": self.phones,
                "wechats": self.wechats,
                "usernames": self.usernames,
            },
            ensure_ascii=False,
        )

    def is_empty(self) -> bool:
        return not (self.phones or self.wechats or self.usernames)


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def extract_contacts(
    text: str | None,
    *,
    capture_phone: bool = True,
    capture_contact: bool = True,
) -> ContactInfo:
    """从消息文本里提取手机号、微信号与 @用户名。"""
    body = text or ""
    phones: list[str] = []
    wechats: list[str] = []
    usernames: list[str] = []
    if capture_phone:
        phones = _unique(CN_PHONE.findall(body) + INTL_PHONE.findall(body))
    if capture_contact:
        wechats = _unique(WECHAT.findall(body))
    usernames = _unique(USERNAME.findall(body))
    return ContactInfo(phones=phones, wechats=wechats, usernames=usernames)


def sender_info(sender: Any) -> SenderInfo:
    """把 Telethon 的发送者对象适配成纯数据。"""
    if sender is None:
        return SenderInfo()
    username = getattr(sender, "username", None)
    first = getattr(sender, "first_name", None) or ""
    last = getattr(sender, "last_name", None) or ""
    title = getattr(sender, "title", None)
    display = title or f"{first}{last}".strip() or username or None
    tg_user_id = getattr(sender, "id", None)
    is_bot = bool(getattr(sender, "bot", False))
    has_title = bool(getattr(sender, "title", None))
    return SenderInfo(
        tg_user_id=int(tg_user_id) if tg_user_id else None,
        username=username,
        display_name=display,
        phone=getattr(sender, "phone", None),
        is_bot=is_bot,
        is_user=not is_bot and not has_title,
        has_peer_reference=getattr(sender, "access_hash", None) is not None,
    )


def contact_summary(
    *,
    sender: SenderInfo,
    contacts: ContactInfo,
    capture_phone: bool = True,
    capture_contact: bool = True,
) -> str:
    """拼一行「能怎么联系他」：手机号优先，其次微信，再次 @用户名。"""
    parts: list[str] = []
    if capture_phone:
        if sender.phone:
            parts.append(f"手机号 {sender.phone}（通讯录）")
        for phone in contacts.phones:
            parts.append(f"手机号 {phone}")
    if capture_contact:
        for wechat in contacts.wechats:
            parts.append(f"微信 {wechat}")
    if sender.username:
        parts.append(f"@{sender.username}")
    for name in contacts.usernames:
        if name != sender.username:
            parts.append(f"@{name}")
    return "、".join(parts) if parts else "未提供"


def render_lead_card(
    template: str | None,
    *,
    sender: SenderInfo,
    contacts: ContactInfo,
    keyword: str,
    text: str,
    source_title: str,
    message_at: datetime | None = None,
    capture_phone: bool = True,
    capture_contact: bool = True,
    max_text: int = 220,
) -> str:
    """按模板渲染线索卡片。"""
    body = (text or "").strip()
    if len(body) > max_text:
        body = body[: max_text - 1] + "…"
    values = {
        "{关键词}": keyword or "",
        "{昵称}": sender.display_name or "（无昵称）",
        "{用户名}": f"@{sender.username}" if sender.username else "（无用户名）",
        "{用户ID}": str(sender.tg_user_id or ""),
        "{联系方式}": contact_summary(
            sender=sender,
            contacts=contacts,
            capture_phone=capture_phone,
            capture_contact=capture_contact,
        ),
        "{手机号}": sender.phone or contacts.primary_phone or "",
        "{微信号}": contacts.primary_wechat or "",
        "{原文}": body,
        "{来源群}": source_title or "",
        "{时间}": message_at.strftime("%Y-%m-%d %H:%M") if message_at else "",
    }
    card = template or DEFAULT_CARD_TEMPLATE
    for key, value in values.items():
        card = card.replace(key, value)
    return card.strip()
