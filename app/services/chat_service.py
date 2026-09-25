"""聊天对象服务：源与接收目标共用一张表。"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.core.telegram_client import ChatProfile
from app.db.models import SOURCE_KIND_LOCAL, Chat


def load_tags(chat: Chat) -> list[str]:
    """读取标签列表（容错）。"""
    try:
        value = json.loads(chat.tags or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(item) for item in value] if isinstance(value, list) else []


def dump_tags(tags: list[str] | None) -> str:
    """序列化标签列表（去空、去重、保持顺序）。"""
    result: list[str] = []
    for tag in tags or []:
        text = str(tag).strip()
        if text and text not in result:
            result.append(text)
    return json.dumps(result, ensure_ascii=False)


async def get_chat(session: AsyncSession, chat_id: int) -> Chat | None:
    """按主键查询。"""
    return await session.get(Chat, chat_id)


async def get_chat_by_tg_id(session: AsyncSession, tg_id: int) -> Chat | None:
    """按 Telegram ID 查询。"""
    return await session.scalar(select(Chat).where(Chat.tg_id == tg_id))


async def list_chats(
    session: AsyncSession,
    *,
    chat_type: str | None = None,
    joined: bool | None = None,
    source_kind: str | None = None,
    tag: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Chat], int]:
    """分页查询聊天对象。"""
    statements = _chat_filters(
        chat_type=chat_type,
        joined=joined,
        source_kind=source_kind,
        tag=tag,
        keyword=keyword,
    )
    statement = select(Chat).order_by(Chat.id)
    count_statement = select(func.count()).select_from(Chat)
    for condition in statements:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)
    return rows, total


def _chat_filters(
    *,
    chat_type: str | None,
    joined: bool | None,
    source_kind: str | None,
    tag: str | None,
    keyword: str | None,
) -> list:
    conditions = []
    if chat_type:
        conditions.append(Chat.chat_type == chat_type)
    if joined is not None:
        conditions.append(Chat.joined.is_(joined))
    if source_kind:
        conditions.append(Chat.source_kind == source_kind)
    if tag:
        conditions.append(Chat.tags.like(f'%"{tag}"%'))
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(Chat.title.like(pattern) | Chat.username.like(pattern))
    return conditions


async def upsert_chat_from_profile(
    session: AsyncSession,
    profile: ChatProfile,
    *,
    joined: bool = True,
    can_post: bool | None = None,
    tags: list[str] | None = None,
    note: str | None = None,
    source_kind: str = SOURCE_KIND_LOCAL,
) -> Chat:
    """按 Telegram ID 新增或更新聊天对象。"""
    chat = await get_chat_by_tg_id(session, profile.tg_id) if profile.tg_id else None
    if chat is None:
        chat = Chat(
            tg_id=profile.tg_id,
            chat_type=profile.chat_type,
            title=profile.title,
            username=profile.username,
            is_private=profile.is_private,
            joined=joined,
            can_post=can_post,
            member_count=profile.member_count,
            tags=dump_tags(tags),
            source_kind=source_kind,
            note=(note or None),
        )
        session.add(chat)
    else:
        chat.chat_type = profile.chat_type
        chat.title = profile.title or chat.title
        chat.username = profile.username or chat.username
        chat.is_private = profile.is_private
        chat.joined = joined
        if can_post is not None:
            chat.can_post = can_post
        if profile.member_count is not None:
            chat.member_count = profile.member_count
        if tags:
            chat.tags = dump_tags(tags)
        if note:
            chat.note = note

    await session.commit()
    await session.refresh(chat)
    return chat


async def update_chat(
    session: AsyncSession,
    chat_id: int,
    *,
    tags: list[str] | None = None,
    note: str | None = None,
    joined: bool | None = None,
    can_post: bool | None = None,
) -> Chat:
    """更新标签、备注与权限状态。"""
    chat = await get_chat(session, chat_id)
    if chat is None:
        raise NotFoundError("聊天对象不存在")
    if tags is not None:
        chat.tags = dump_tags(tags)
    if note is not None:
        chat.note = note.strip() or None
    if joined is not None:
        chat.joined = joined
    if can_post is not None:
        chat.can_post = can_post
    await session.commit()
    await session.refresh(chat)
    return chat


async def delete_chat(session: AsyncSession, chat_id: int) -> Chat:
    """删除聊天对象。"""
    chat = await get_chat(session, chat_id)
    if chat is None:
        raise NotFoundError("聊天对象不存在")
    await session.delete(chat)
    await session.commit()
    return chat


async def list_tags(session: AsyncSession) -> list[str]:
    """汇总所有已使用的标签。"""
    rows = await session.scalars(select(Chat.tags))
    tags: list[str] = []
    for raw in rows:
        try:
            value = json.loads(raw or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(value, list):
            for item in value:
                text = str(item)
                if text not in tags:
                    tags.append(text)
    return sorted(tags)
