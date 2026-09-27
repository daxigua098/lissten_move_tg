"""聊天对象服务：群目录（客观信息）+ 租户群关系（备注、角色、启停）。

拆表口径（见《多租户地基_字段级设计_v1.0.md》第 5 节）：

- ``chat_directory``：这个群"是什么"（tg_id / 标题 / 类型 / 成员数），全平台一份；
- ``tenant_chats``：我"和这个群什么关系"（备注名、标签、源/接收组、启停），按租户一份。

两个表的本地 ID 在迁移时是同一个（ID 沿用），所以线路引用、接收目标与水位线的
值一个都不用改。租户侧 ID 就是过去 ``chats.id`` 的延续。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, ValidationFailedError
from app.core.telegram_client import ChatProfile
from app.db.models import (
    SELF_TENANT_ID,
    SOURCE_KIND_LOCAL,
    TARGET_ROLE_CONTENT,
    TARGET_ROLES,
    ChatDirectory,
    TenantChat,
)


def load_tags(chat: TenantChat) -> list[str]:
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


async def get_chat(session: AsyncSession, chat_id: int) -> TenantChat | None:
    """按租户侧 ID（沿用原 chats.id）查询。"""
    return await session.get(TenantChat, chat_id)


async def get_directory_by_tg_id(session: AsyncSession, tg_id: int) -> ChatDirectory | None:
    """按 Telegram ID 查群目录。"""
    return await session.scalar(select(ChatDirectory).where(ChatDirectory.tg_id == int(tg_id)))


async def get_chat_by_tg_id(
    session: AsyncSession,
    tg_id: int,
    *,
    tenant_id: int = SELF_TENANT_ID,
) -> TenantChat | None:
    """按 Telegram ID 查某租户下的群配置。"""
    return await session.scalar(
        select(TenantChat)
        .join(ChatDirectory, TenantChat.chat_id == ChatDirectory.id)
        .where(
            ChatDirectory.tg_id == int(tg_id),
            TenantChat.tenant_id == tenant_id,
        )
    )


def apply_profile(directory: ChatDirectory, profile: Any) -> None:
    """把 Telegram 拉到的客观信息写进目录行（不碰租户侧字段）。"""
    directory.chat_type = profile.chat_type
    directory.title = profile.title or directory.title
    directory.username = profile.username or directory.username
    directory.is_private = profile.is_private
    if profile.member_count is not None:
        directory.member_count = profile.member_count


async def migrate_chat(
    session: AsyncSession,
    *,
    old_tg_id: int,
    profile: Any,
) -> TenantChat | None:
    """群升级成超级群后，把群目录迁到新 id。

    只改目录的 tg_id / 类型 / 标题，租户侧 ID 不变——线路引用、接收目标与水位线
    都保留。新 id 已经有目录时（先同步到过），把租户配置并过去再删掉旧目录。
    """
    old = await get_directory_by_tg_id(session, old_tg_id)
    if old is None or int(profile.tg_id) == int(old_tg_id):
        return None

    new_tg_id = int(profile.tg_id)
    clash = await get_directory_by_tg_id(session, new_tg_id)
    if clash is not None and clash.id != old.id:
        for row in list(
            await session.scalars(select(TenantChat).where(TenantChat.chat_id == old.id))
        ):
            existing = await session.scalar(
                select(TenantChat).where(
                    TenantChat.tenant_id == row.tenant_id,
                    TenantChat.chat_id == clash.id,
                )
            )
            if existing is not None:
                await session.delete(row)
            else:
                row.chat_id = clash.id
        apply_profile(clash, profile)
        await session.delete(old)
        await session.commit()
        return None

    old.tg_id = new_tg_id
    apply_profile(old, profile)
    await session.commit()
    return await get_chat_by_tg_id(session, new_tg_id)


def _scoped_conditions(
    *,
    chat_type: str | None = None,
    joined: bool | None = None,
    source_kind: str | None = None,
    tag: str | None = None,
    keyword: str | None = None,
    target_role: str | None = None,
) -> list[Any]:
    conditions: list[Any] = []
    if chat_type:
        conditions.append(ChatDirectory.chat_type == chat_type)
    if joined is not None:
        conditions.append(TenantChat.joined.is_(joined))
    if source_kind:
        conditions.append(ChatDirectory.source_kind == source_kind)
    if tag:
        conditions.append(TenantChat.tags.like(f'%"{tag}"%'))
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(ChatDirectory.title.like(pattern) | ChatDirectory.username.like(pattern))
    if target_role:
        conditions.append(TenantChat.target_role == target_role)
    return conditions


def _page_statements(
    conditions: list[Any],
    *,
    tenant_id: int,
    order_by: Any = None,
) -> tuple[Any, Any]:
    join = ChatDirectory.__table__.join(
        TenantChat.__table__,
        TenantChat.chat_id == ChatDirectory.id,
    )
    statement = select(TenantChat).select_from(join)
    count_statement = select(func.count()).select_from(join)
    for condition in conditions:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)
    statement = statement.where(TenantChat.tenant_id == tenant_id)
    count_statement = count_statement.where(TenantChat.tenant_id == tenant_id)
    if order_by is None:
        order_clauses: tuple[Any, ...] = (TenantChat.id,)
    elif isinstance(order_by, (tuple, list)):
        order_clauses = tuple(order_by)
    else:
        order_clauses = (order_by,)
    return statement.order_by(*order_clauses), count_statement


async def _fetch_page(
    session: AsyncSession,
    statement: Any,
    count_statement: Any,
    *,
    limit: int,
    offset: int,
) -> tuple[list[TenantChat], int]:
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)
    return rows, total


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
    tenant_id: int = SELF_TENANT_ID,
) -> tuple[list[TenantChat], int]:
    """分页查询聊天对象。"""
    conditions = _scoped_conditions(
        chat_type=chat_type,
        joined=joined,
        source_kind=source_kind,
        tag=tag,
        keyword=keyword,
    )
    statement, count_statement = _page_statements(conditions, tenant_id=tenant_id)
    return await _fetch_page(session, statement, count_statement, limit=limit, offset=offset)


async def upsert_chat_from_profile(
    session: AsyncSession,
    profile: ChatProfile,
    *,
    joined: bool = True,
    can_post: bool | None = None,
    tags: list[str] | None = None,
    note: str | None = None,
    source_kind: str = SOURCE_KIND_LOCAL,
    tenant_id: int = SELF_TENANT_ID,
) -> TenantChat:
    """按 Telegram ID 新增或更新：客观信息进目录，租户配置进 tenant_chats。"""
    if not profile.tg_id:
        raise ValidationFailedError("缺少 Telegram 群 ID，无法登记")

    directory = await get_directory_by_tg_id(session, profile.tg_id)
    if directory is None:
        directory = ChatDirectory(
            tg_id=int(profile.tg_id),
            chat_type=profile.chat_type,
            title=profile.title,
            username=profile.username,
            is_private=profile.is_private,
            member_count=profile.member_count,
            source_kind=source_kind,
        )
        session.add(directory)
        await session.flush()
    else:
        apply_profile(directory, profile)
        if source_kind and directory.source_kind != source_kind:
            directory.source_kind = source_kind

    chat = await get_chat_by_tg_id(session, profile.tg_id, tenant_id=tenant_id)
    if chat is None:
        chat = TenantChat(
            tenant_id=tenant_id,
            chat_id=directory.id,
            joined=joined,
            can_post=can_post,
            tags=dump_tags(tags),
            note=(note or None),
        )
        session.add(chat)
    else:
        chat.joined = joined
        if can_post is not None:
            chat.can_post = can_post
        if tags:
            chat.tags = dump_tags(tags)
        if note:
            chat.note = note

    chat.directory = directory
    await session.commit()
    return chat


async def update_chat(
    session: AsyncSession,
    chat_id: int,
    *,
    display_name: str | None = None,
    tags: list[str] | None = None,
    note: str | None = None,
    joined: bool | None = None,
    can_post: bool | None = None,
) -> TenantChat:
    """更新备注名、标签与权限状态（都是租户侧字段）。"""
    chat = await get_chat(session, chat_id)
    if chat is None:
        raise NotFoundError("聊天对象不存在")
    if display_name is not None:
        chat.display_name = display_name.strip() or None
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


async def delete_chat(session: AsyncSession, chat_id: int) -> TenantChat:
    """删除本租户的聊天对象（群目录保留给别的租户继续用）。"""
    chat = await get_chat(session, chat_id)
    if chat is None:
        raise NotFoundError("聊天对象不存在")
    await session.delete(chat)
    await session.commit()
    return chat


async def list_tags(session: AsyncSession, *, tenant_id: int = SELF_TENANT_ID) -> list[str]:
    """汇总本租户已使用的标签。"""
    rows = await session.scalars(select(TenantChat.tags).where(TenantChat.tenant_id == tenant_id))
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


async def list_sources(
    session: AsyncSession,
    *,
    enabled: bool | None = None,
    tag: str | None = None,
    keyword: str | None = None,
    limit: int = 100,
    offset: int = 0,
    tenant_id: int = SELF_TENANT_ID,
) -> tuple[list[TenantChat], int]:
    """监听源列表。"""
    return await _list_by_role(
        session,
        role_column=TenantChat.is_source,
        enabled_column=TenantChat.source_enabled,
        enabled=enabled,
        tag=tag,
        keyword=keyword,
        limit=limit,
        offset=offset,
        tenant_id=tenant_id,
    )


async def list_targets(
    session: AsyncSession,
    *,
    role: str | None = None,
    enabled: bool | None = None,
    tag: str | None = None,
    keyword: str | None = None,
    limit: int = 100,
    offset: int = 0,
    tenant_id: int = SELF_TENANT_ID,
) -> tuple[list[TenantChat], int]:
    """接收组列表。"""
    return await _list_by_role(
        session,
        role_column=TenantChat.is_target,
        enabled_column=TenantChat.target_enabled,
        enabled=enabled,
        tag=tag,
        keyword=keyword,
        target_role=role,
        limit=limit,
        offset=offset,
        tenant_id=tenant_id,
    )


async def _list_by_role(
    session: AsyncSession,
    *,
    role_column: Any,
    enabled_column: Any,
    enabled: bool | None,
    tag: str | None,
    keyword: str | None,
    target_role: str | None = None,
    limit: int = 100,
    offset: int = 0,
    tenant_id: int = SELF_TENANT_ID,
) -> tuple[list[TenantChat], int]:
    conditions = [role_column.is_(True)]
    if enabled is not None:
        conditions.append(enabled_column.is_(enabled))
    conditions.extend(_scoped_conditions(tag=tag, keyword=keyword, target_role=target_role))
    statement, count_statement = _page_statements(conditions, tenant_id=tenant_id)
    return await _fetch_page(session, statement, count_statement, limit=limit, offset=offset)


async def list_pool(
    session: AsyncSession,
    *,
    exclude_sources: bool = False,
    exclude_targets: bool = False,
    keyword: str | None = None,
    limit: int = 200,
    offset: int = 0,
    tenant_id: int = SELF_TENANT_ID,
) -> tuple[list[TenantChat], int]:
    """可选群组池：已经同步到本地、但还没被选为源/接收组的聊天对象。"""
    conditions: list[Any] = []
    if exclude_sources:
        conditions.append(TenantChat.is_source.is_(False))
    if exclude_targets:
        conditions.append(TenantChat.is_target.is_(False))
    conditions.extend(_scoped_conditions(keyword=keyword))
    statement, count_statement = _page_statements(
        conditions,
        tenant_id=tenant_id,
        order_by=(ChatDirectory.title, TenantChat.id),
    )
    return await _fetch_page(session, statement, count_statement, limit=limit, offset=offset)


async def get_chats_by_ids(session: AsyncSession, chat_ids: list[int]) -> list[TenantChat]:
    """按本地 ID 批量取聊天对象。"""
    if not chat_ids:
        return []
    rows = await session.scalars(select(TenantChat).where(TenantChat.id.in_(chat_ids)))
    return list(rows)


async def set_source(
    session: AsyncSession, chat: TenantChat, *, enabled: bool = True
) -> TenantChat:
    """把聊天对象标记为监听源。"""
    chat.is_source = True
    chat.source_enabled = enabled
    await session.commit()
    await session.refresh(chat)
    return chat


async def unset_source(session: AsyncSession, chat: TenantChat) -> TenantChat:
    """把聊天对象移出监听源（仍保留在群组池里）。"""
    chat.is_source = False
    chat.source_enabled = True
    await session.commit()
    await session.refresh(chat)
    return chat


async def set_target(
    session: AsyncSession,
    chat: TenantChat,
    *,
    role: str = TARGET_ROLE_CONTENT,
    enabled: bool = True,
) -> TenantChat:
    """把聊天对象标记为接收组。"""
    if role not in TARGET_ROLES:
        raise ValidationFailedError(f"用途必须是 {'/'.join(TARGET_ROLES)} 之一")
    chat.is_target = True
    chat.target_enabled = enabled
    chat.target_role = role
    await session.commit()
    await session.refresh(chat)
    return chat


async def unset_target(session: AsyncSession, chat: TenantChat) -> TenantChat:
    """把聊天对象移出接收组。"""
    chat.is_target = False
    chat.target_enabled = True
    await session.commit()
    await session.refresh(chat)
    return chat


async def batch_update_tags(
    session: AsyncSession,
    chat_ids: list[int],
    *,
    tags: list[str],
    mode: str = "add",
) -> int:
    """批量打标签：add 追加 / replace 覆盖 / remove 移除。"""
    if mode not in {"add", "replace", "remove"}:
        raise ValidationFailedError("标签操作必须是 add / replace / remove 之一")
    chats = await get_chats_by_ids(session, chat_ids)
    for chat in chats:
        current = load_tags(chat)
        if mode == "replace":
            updated = list(tags)
        elif mode == "remove":
            updated = [item for item in current if item not in tags]
        else:
            updated = current + [item for item in tags if item not in current]
        chat.tags = dump_tags(updated)
    await session.commit()
    return len(chats)
