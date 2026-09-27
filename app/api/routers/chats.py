"""群组池与源/接收组共用的序列化与辅助函数。"""

from __future__ import annotations

from typing import Any

from app.db.models import TenantChat
from app.services import chat_service

CHAT_TYPE_LABEL = {
    "channel": "频道",
    "supergroup": "超级群",
    "group": "群组",
}

TARGET_ROLE_LABEL = {
    "content": "内容接收",
    "lead": "线索接收",
}


def serialize_chat(chat: TenantChat) -> dict[str, Any]:
    """聊天对象的统一对外结构。"""
    return {
        "id": chat.id,
        "tg_id": chat.tg_id,
        "chat_type": chat.chat_type,
        "chat_type_label": CHAT_TYPE_LABEL.get(chat.chat_type, chat.chat_type),
        "title": chat.title,
        "display_name": chat.display_name,
        # 展示用名称：备注名 > 原标题 > 用户名
        "name": chat.display_name or chat.title or chat.username or f"#{chat.tg_id}",
        "username": chat.username,
        "is_private": chat.is_private,
        "joined": chat.joined,
        "can_post": chat.can_post,
        "member_count": chat.member_count,
        "tags": chat_service.load_tags(chat),
        "source_kind": chat.source_kind,
        "is_source": chat.is_source,
        "source_enabled": chat.source_enabled,
        "is_target": chat.is_target,
        "target_enabled": chat.target_enabled,
        "target_role": chat.target_role,
        "target_role_label": TARGET_ROLE_LABEL.get(chat.target_role, chat.target_role),
        "note": chat.note,
    }


def page(items: list[TenantChat], total: int, limit: int, offset: int) -> dict[str, Any]:
    """分页响应。"""
    return {
        "items": [serialize_chat(item) for item in items],
        "total": total,
        "limit": limit,
        "offset": offset,
    }
