"""Telethon 客户端工厂与资料读取。

网络相关逻辑集中在这里；业务层通过参数注入工厂，测试可传替身。
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import AppConfig
from app.core.content_cleaner import (
    KIND_DOCUMENT,
    KIND_OTHER,
    KIND_PHOTO,
    KIND_POLL,
    KIND_SERVICE,
    KIND_TEXT,
    KIND_VIDEO,
    MessageView,
)
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.source_resolver import ResolvedTarget

DEFAULT_SESSION_DIR = "data/sessions"


@dataclass(frozen=True)
class AccountProfile:
    """账号或 Bot 自身资料。"""

    tg_user_id: int
    username: str | None
    display_name: str | None
    phone: str | None


@dataclass(frozen=True)
class ChatProfile:
    """聊天对象资料。"""

    tg_id: int
    chat_type: str
    title: str | None
    username: str | None
    is_private: bool
    member_count: int | None = None


@dataclass(frozen=True)
class ChatFullInfo:
    """群 / 频道的完整资料（探测用）。"""

    about: str | None = None
    member_count: int | None = None
    member_count_approx: bool = False


# Telegram 在成员数超过这个量级后只给近似值（官方未公开阈值，取常见口径）
APPROX_MEMBER_THRESHOLD = 5000


def session_file_path(config: AppConfig, session_name: str) -> Path:
    """执行账号的 session 文件路径（Telethon 会自行补 .session 后缀）。"""
    name = (session_name or "").strip() or "default"
    candidate = Path(name)
    if candidate.suffix == ".session":
        candidate = candidate.with_suffix("")
    if not candidate.is_absolute():
        candidate = Path(DEFAULT_SESSION_DIR) / candidate.name
    return config.path(candidate)


def build_proxy(proxy: str | None) -> Any:
    """把 `socks5://host:port` 转成 Telethon 需要的代理配置。

    生效需要安装代理依赖：`pip install "telethon[proxy]"`。
    """
    if not proxy:
        return None
    text = proxy.strip()
    scheme, _, address = text.partition("://")
    host, _, port = address.partition(":")
    if not host or not port.isdigit():
        raise ValidationFailedError(f"代理格式不正确：{proxy}（示例 socks5://127.0.0.1:1080）")
    return {
        "proxy_type": scheme or "socks5",
        "addr": host,
        "port": int(port),
        "rdns": True,
    }


def build_user_client(
    config: AppConfig,
    *,
    api_id: int,
    api_hash: str,
    session_path: Path | str,
) -> Any:
    """构造执行账号用的 Telethon 客户端（未连接）。"""
    from telethon import TelegramClient

    if api_id <= 0 or not api_hash:
        raise ValidationFailedError("缺少 API ID / API Hash，无法创建 Telegram 客户端")
    return TelegramClient(
        str(session_path),
        api_id,
        api_hash,
        proxy=build_proxy(config.telegram.proxy),
    )


def build_bot_client(config: AppConfig, token: str) -> Any:
    """构造控制 Bot 用的临时客户端（内存 session，不落盘）。"""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    if not config.telegram.configured:
        raise ValidationFailedError(
            "未配置 TG_API_ID / TG_API_HASH，无法校验 Bot Token（到 my.telegram.org 申请）"
        )
    return TelegramClient(StringSession(), config.telegram.api_id, config.telegram.api_hash)


async def fetch_account_profile(client: Any) -> AccountProfile:
    """读取当前登录账号的资料。"""
    me = await client.get_me()
    display = " ".join(part for part in [me.first_name, me.last_name] if part) or None
    return AccountProfile(
        tg_user_id=int(me.id),
        username=me.username or None,
        display_name=display,
        phone=me.phone or None,
    )


async def fetch_bot_profile(client: Any) -> AccountProfile:
    """读取 Bot 自身资料（用于校验 Token）。"""
    me = await client.get_me()
    return AccountProfile(
        tg_user_id=int(me.id),
        username=me.username or None,
        display_name=me.first_name or None,
        phone=None,
    )


def _chat_type(entity: Any) -> str:
    if getattr(entity, "broadcast", False):
        return "channel"
    if getattr(entity, "megagroup", False):
        return "supergroup"
    return "group"


def profile_from_entity(entity: Any) -> ChatProfile:
    """从 Telethon 实体构造聊天资料。"""
    return ChatProfile(
        tg_id=int(entity.id),
        chat_type=_chat_type(entity),
        title=getattr(entity, "title", None),
        username=getattr(entity, "username", None),
        is_private=not bool(getattr(entity, "username", None)),
        member_count=getattr(entity, "participants_count", None),
    )


def message_view_from_telethon(message: Any) -> MessageView:
    """把 Telethon 消息适配成净化引擎需要的 MessageView。"""
    if getattr(message, "action", None) is not None:
        kind = KIND_SERVICE
    elif getattr(message, "photo", None) is not None:
        kind = KIND_PHOTO
    elif getattr(message, "video", None) is not None:
        kind = KIND_VIDEO
    elif getattr(message, "document", None) is not None:
        kind = KIND_DOCUMENT
    elif getattr(message, "poll", None) is not None:
        kind = KIND_POLL
    elif (getattr(message, "message", None) or "").strip():
        kind = KIND_TEXT
    else:
        kind = KIND_OTHER

    sender = getattr(message, "sender", None)
    return MessageView(
        message_id=int(getattr(message, "id", 0) or 0),
        kind=kind,
        text=getattr(message, "message", None) or "",
        grouped_id=getattr(message, "grouped_id", None),
        is_post=bool(getattr(message, "post", False)),
        sender_id=getattr(message, "sender_id", None),
        sender_username=getattr(sender, "username", None),
        sender_is_bot=bool(getattr(sender, "bot", False)),
        is_forwarded=bool(getattr(message, "fwd_from", None)),
        date=getattr(message, "date", None),
    )


async def iter_source_messages(
    client: Any,
    entity: Any,
    *,
    min_id: int = 0,
    limit: int = 500,
    skip_pinned: bool = True,
) -> list[Any]:
    """按消息 ID 从小到大拉取历史消息（跳过置顶可选）。"""
    messages = await client.get_messages(entity, min_id=int(min_id), limit=limit, reverse=True)
    if skip_pinned:
        return [item for item in messages if not getattr(item, "pinned", False)]
    return list(messages)


async def forward_to_target(
    client: Any,
    *,
    source_entity: Any,
    target_entity: Any,
    message_ids: list[int],
    drop_author: bool = True,
) -> Any:
    """把源消息转发到目标；drop_author=True 即"去来源标记"的 copy 模式。"""
    return await client.forward_messages(
        target_entity,
        message_ids,
        source_entity,
        drop_author=drop_author,
    )


async def repost_message(
    client: Any,
    *,
    target_entity: Any,
    message: Any,
    caption: str | None,
) -> Any:
    """把源消息的媒体重新上传到目标，并替换为净化后的文案。"""
    if getattr(message, "media", None) is None:
        # 纯文本消息没有媒体可上传，直接发净化后的文案
        return await client.send_message(target_entity, caption or "")
    return await client.send_file(
        target_entity,
        file=message,
        caption=caption or None,
    )


async def repost_album(
    client: Any,
    *,
    target_entity: Any,
    messages: list[Any],
    caption: str | None,
) -> Any:
    """把一条帖子的多条媒体作为**一条相册**发出去。

    关键点：这里传的是源消息对象，Telethon 会把它们转成
    ``InputMediaPhoto`` / ``InputMediaDocument``（引用原文件），
    再用 ``messages.sendMultiMedia`` 发送——**不下载、不重新上传**，
    目标群里看到的就是一条多图/多视频消息，而不是逐张刷屏。

    文案挂在这条相册的第一张上（Telegram 的相册就是这样显示的）。
    """
    items = [item for item in messages if getattr(item, "media", None) is not None]
    if not items:
        return await client.send_message(target_entity, caption or "")
    if len(items) == 1:
        return await client.send_file(target_entity, file=items[0], caption=caption or None)
    return await client.send_file(target_entity, file=items, caption=caption or None)


async def send_ad(
    client: Any,
    *,
    target_entity: Any,
    text: str | None,
    image_path: str | None = None,
    link_url: str | None = None,
    link_text: str | None = None,
) -> Any:
    """发送广告素材：图片 + 文案 + 链接按钮，或纯文案。"""
    buttons = None
    if link_url and link_text:
        from telethon import Button

        buttons = [[Button.url(link_text, link_url)]]

    if image_path and await asyncio.to_thread(Path(image_path).is_file):
        return await client.send_file(
            target_entity,
            str(image_path),
            caption=text or None,
            buttons=buttons,
        )
    return await client.send_message(target_entity, text or "", buttons=buttons)


def render_ad_text(template: str, *, source_title: str, route_name: str) -> str:
    """替换广告文案里的变量。"""
    text = template or ""
    return (
        text.replace("{源名}", source_title or "")
        .replace("{原发言人}", source_title or "")
        .replace("{线路名}", route_name or "")
    )


SESSION_LOCK_HINT = (
    "执行账号的会话文件正被搬运运行时占用，请稍后重试；"
    "如果一直失败，先在「运行总览」暂停运行时再操作。"
)


def _is_session_locked(exc: BaseException) -> bool:
    """会话文件被另一个进程（搬运运行时）写锁占用。"""
    for item in (exc, getattr(exc, "__cause__", None), getattr(exc, "__context__", None)):
        if isinstance(item, sqlite3.OperationalError) and "locked" in str(item).lower():
            return True
    return False


async def connect_user_client(
    config: AppConfig,
    *,
    api_id: int,
    api_hash: str,
    session_path: Path | str,
    attempts: int = 3,
) -> Any:
    """连接执行账号客户端，并确认已登录。

    后台服务与搬运运行时是两个进程，却共用同一个 Telethon 会话文件。SQLite 写锁
    冲突时（"database is locked"）这里退避重试；仍然失败就给出可读的提示，
    而不是把 500 内部错误抛给界面。
    """
    last_exc: BaseException | None = None
    for index in range(1, attempts + 1):
        client = build_user_client(
            config,
            api_id=api_id,
            api_hash=api_hash,
            session_path=session_path,
        )
        try:
            await client.connect()
        except Exception as exc:  # noqa: BLE001 - 需要区分“会话被占用”与其他失败
            last_exc = exc
            with contextlib.suppress(Exception):
                await client.disconnect()
            if not _is_session_locked(exc) or index == attempts:
                break
            await asyncio.sleep(1.5 * index)
            continue

        if not await client.is_user_authorized():
            with contextlib.suppress(Exception):
                await client.disconnect()
            raise ValidationFailedError(
                "该执行账号尚未登录，请先运行：python main.py account-login --name 别名"
            )
        with contextlib.suppress(Exception):
            # 预热实体缓存：失败不影响连接本身，解析时还有 resolve_entity 兜底
            await warm_entity_cache(client)
        return client

    if last_exc is not None and _is_session_locked(last_exc):
        raise ConflictError(SESSION_LOCK_HINT) from last_exc
    raise last_exc if last_exc is not None else RuntimeError("无法连接执行账号")


def _is_basic_group(entity: Any) -> bool:
    """基础群：Telegram 早期的小群，Telethon 里是 ``Chat``，没有广播/超级群标志。"""
    try:
        from telethon.tl import types as tl_types
    except ImportError:  # pragma: no cover - 演练模式的替身实体不依赖 telethon
        return False
    if not isinstance(entity, tl_types.Chat):
        return False
    # 已经升级成超级群的旧群，内容在新群里，跳过以免重复
    if getattr(entity, "migrated_to", None) is not None:
        return False
    return not getattr(entity, "deactivated", False)


def is_group_or_channel(entity: Any) -> bool:
    """是否是群组或频道（含基础群）。

    只按 ``broadcast`` / ``megagroup`` 判断会把基础群整批漏掉——它们这两个标志都是
    假的，于是「本号明明加了这个群，后台里却看不到」。私聊与机器人则要排除。
    """
    if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
        return True
    return _is_basic_group(entity)


async def fetch_dialog_entities(client: Any, *, limit: int = 500) -> list[Any]:
    """拉取账号已加入的群组/频道实体（含基础群，不含私聊与用户）。"""
    entities: list[Any] = []
    async for dialog in client.iter_dialogs(limit=limit):
        entity = getattr(dialog, "entity", None)
        if entity is None or getattr(entity, "id", None) is None:
            continue
        if not is_group_or_channel(entity):
            continue
        entities.append(entity)
    return entities


async def fetch_migrations(client: Any, *, limit: int = 500) -> list[tuple[int, Any]]:
    """找出「基础群被升级成超级群」的情况，返回 `[(旧 id, 新实体)]`。

    群升级后旧 id 就成了空壳：Bot API 会直接报
    「group chat was upgraded to a supergroup chat」。同步时顺手把本地记录迁过去。
    """
    pairs: list[tuple[int, Any]] = []
    async for dialog in client.iter_dialogs(limit=limit):
        entity = getattr(dialog, "entity", None)
        migrated = getattr(entity, "migrated_to", None)
        if entity is None or migrated is None:
            continue
        try:
            upgraded = await client.get_entity(migrated)
        except Exception:  # noqa: BLE001 - 拿不到新群就跳过，不影响其他同步
            continue
        pairs.append((int(entity.id), upgraded))
    return pairs


async def fetch_dialogs(client: Any, *, limit: int = 500) -> list[ChatProfile]:
    """拉取账号已加入的群组与频道资料（含基础群，忽略私聊与用户）。"""
    entities = await fetch_dialog_entities(client, limit=limit)
    return [profile_from_entity(entity) for entity in entities]


async def warm_entity_cache(client: Any, *, limit: int = 500) -> int:
    """把账号可见的对话灌进 Telethon 的实体缓存，返回预热条数。

    本地只保存数字 id，Telethon 按裸 id 解析对象时依赖内存里的实体缓存；新进程
    刚连上时缓存是空的，基础群这种正数 id 又无法从号段推断类型，最容易解析失败。
    先遍历一次对话列表即可填满缓存，后续 ``get_entity(tg_id)`` 才能稳定命中。
    """
    count = 0
    async for dialog in client.iter_dialogs(limit=limit):
        if getattr(dialog, "entity", None) is not None:
            count += 1
    return count


async def resolve_entity(client: Any, tg_id: int) -> Any:
    """按本地保存的 tg_id 解析 Telegram 实体。

    直接解析失败时退化为扫描一次对话列表，避免因为实体缓存不完整而整条线路报错。
    """
    try:
        return await client.get_entity(int(tg_id))
    except Exception:  # noqa: BLE001 - 解析失败时走下面的兜底
        pass
    async for dialog in client.iter_dialogs(limit=None):
        entity = getattr(dialog, "entity", None)
        if entity is not None and int(getattr(entity, "id", 0) or 0) == int(tg_id):
            return entity
    raise NotFoundError(f"Telegram 里找不到对象 {tg_id}：可能已退出该群/频道，或该账号无权访问")


async def join_invite(client: Any, invite_hash: str) -> ChatProfile:
    """通过邀请链接加入私有群/频道。"""
    from telethon.tl import functions

    result = await client(functions.messages.ImportChatInviteRequest(hash=invite_hash))
    chats = getattr(result, "chats", None) or []
    if not chats:
        raise ValidationFailedError("加入失败：Telegram 未返回群组信息")
    return profile_from_entity(chats[0])


async def check_can_post(client: Any, tg_id: int) -> bool | None:
    """检查执行账号能否在该群/频道发帖；判断不出来时返回 None。

    这里有个很容易踩的坑：Telethon 的 ``get_permissions(entity)``（不带 user）
    返回的是这个群的**默认限制**（``ChatBannedRights``），其中 ``send_messages=False``
    表示「没有限制」，把它当成「不能发帖」会得到完全相反的结论。要判断"我能不能发"，
    必须带上当前用户：``get_permissions(entity, me)``。
    """
    try:
        entity = await resolve_entity(client, tg_id)
        me = await client.get_me(input_peer=True)
        permissions = await client.get_permissions(entity, me)
    except Exception:  # noqa: BLE001 - 权限预检失败不应阻断添加流程
        return None
    if permissions is None:
        return None
    if getattr(permissions, "has_left", False) or getattr(permissions, "is_banned", False):
        return False
    if getattr(permissions, "is_creator", False):
        return True
    if getattr(entity, "broadcast", False):
        # 广播频道：只有带发帖权限的管理员能发言，普通订阅者不行
        return bool(
            getattr(permissions, "is_admin", False) and getattr(permissions, "post_messages", False)
        )
    if getattr(permissions, "is_admin", False):
        return True
    # 普通成员：群 / 超级群默认能发言，除非群本身禁止了成员发言
    default_rights = getattr(entity, "default_banned_rights", None)
    if default_rights is not None and getattr(default_rights, "send_messages", False):
        return False
    return True


async def fetch_chat_profile(client: Any, target: ResolvedTarget) -> ChatProfile:
    """解析聊天对象资料；私有邀请链接先查询邀请信息。"""
    if target.kind == "invite":
        from telethon.tl import functions

        invite = await client(functions.messages.CheckChatInviteRequest(hash=target.value))
        chat = getattr(invite, "chat", None)
        if chat is None:
            # 尚未加入：仅用邀请信息描述，加入动作由调用方显式触发
            return ChatProfile(
                tg_id=0,
                chat_type="group",
                title=getattr(invite, "title", None),
                username=None,
                is_private=True,
                member_count=getattr(invite, "participants_count", None),
            )
        return ChatProfile(
            tg_id=int(chat.id),
            chat_type=_chat_type(chat),
            title=getattr(chat, "title", None),
            username=getattr(chat, "username", None),
            is_private=True,
            member_count=getattr(chat, "participants_count", None),
        )

    identifier: Any = int(target.value) if target.kind == "tg_id" else target.value
    entity = await client.get_entity(identifier)
    return ChatProfile(
        tg_id=int(entity.id),
        chat_type=_chat_type(entity),
        title=getattr(entity, "title", None),
        username=getattr(entity, "username", None),
        is_private=not bool(getattr(entity, "username", None)),
        member_count=getattr(entity, "participants_count", None),
    )


# ------------------------------------------------------------------ 资源发现


async def fetch_chat_full(client: Any, tg_id: int) -> ChatFullInfo:
    """读取群 / 频道的简介与成员数（F-R06）。

    频道与超级群走 ``channels.getFullChannel``，基础群走 ``messages.getFullChat``；
    两者都失败时退回实体自带的字段，保证探测不会整体失败。
    """
    entity = await resolve_entity(client, tg_id)
    member_count = getattr(entity, "participants_count", None)
    about: str | None = None

    try:
        from telethon.tl import functions

        if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
            result = await client(functions.channels.GetFullChannelRequest(channel=entity))
        else:
            result = await client(functions.messages.GetFullChatRequest(chat_id=entity.id))
        full = getattr(result, "full_chat", None)
        about = getattr(full, "about", None)
        count = getattr(full, "participants_count", None)
        if count is not None:
            member_count = int(count)
    except Exception:  # noqa: BLE001 - 拿不到简介不算探测失败
        pass

    if member_count is None:
        return ChatFullInfo(about=about)
    return ChatFullInfo(
        about=about,
        member_count=int(member_count),
        member_count_approx=int(member_count) >= APPROX_MEMBER_THRESHOLD,
    )


async def sample_chat_messages(
    client: Any,
    tg_id: int,
    *,
    limit: int = 100,
) -> list[MessageView]:
    """采样最近若干条消息（F-R06 的指标原料）。"""
    entity = await resolve_entity(client, tg_id)
    messages = await client.get_messages(entity, limit=int(limit))
    if messages is None:
        return []
    if not isinstance(messages, list):
        messages = [messages]
    return [message_view_from_telethon(item) for item in messages if item is not None]


async def search_public_chats(
    client: Any,
    keyword: str,
    *,
    limit: int = 20,
) -> list[ChatProfile]:
    """按关键词搜索公开群 / 频道（F-R02 的 ``contacts.Search``）。

    搜索结果里会混进用户，用"有没有 title"把它们滤掉——群和频道一定有 title。
    """
    from telethon.tl import functions

    result = await client(functions.contacts.SearchRequest(q=keyword, limit=int(limit)))
    chats = getattr(result, "chats", None) or []
    return [
        profile_from_entity(item)
        for item in chats
        if getattr(item, "title", None) and getattr(item, "id", None)
    ]


async def search_global_messages(
    client: Any,
    query: str,
    *,
    limit: int = 20,
) -> list[Any]:
    """全局消息搜索（F-R05 句式搜索）。"""
    from telethon.tl import functions, types

    request = functions.messages.SearchGlobalRequest(
        q=query,
        filter=types.InputMessagesFilterEmpty(),
        min_date=None,
        max_date=None,
        offset_rate=0,
        offset_peer=types.InputPeerEmpty(),
        offset_id=0,
        limit=int(limit),
    )
    result = await client(request)
    messages = getattr(result, "messages", None) or []
    return [item for item in messages if getattr(item, "message", None)]


async def join_chat(client: Any, entity: Any) -> Any:
    """加入公开群 / 频道（用 @username 或数字 ID 解析出的实体）。"""
    from telethon.tl import functions

    return await client(functions.channels.JoinChannelRequest(channel=entity))


async def leave_chat(client: Any, entity: Any) -> Any:
    """退出群 / 频道。"""
    from telethon.tl import functions

    return await client(functions.channels.LeaveChannelRequest(channel=entity))


# 加群失败分类（F-R12）：把 Telegram 的报错原文收敛成几类，界面好展示也好统计
JOIN_ERROR_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (("invite_hash_expired", "expired", "invitehash"), "invite_invalid", "邀请链接已失效"),
    (
        ("invite_request_sent", "request_sent", "admin approval", "approval"),
        "approval_required",
        "需要管理员审批",
    ),
    (
        ("user_already_participant", "already participant", "already_participant"),
        "already_member",
        "已在群内",
    ),
    (("users_too_much", "too many", "chat_full", "participants too much"), "full", "群已满员"),
    (("flood_wait", "flood", "peer_flood", "too many requests"), "restricted", "被限流，需要等待"),
    (("chat_write_forbidden", "banned", "kicked", "user_banned"), "restricted", "被限制加入"),
    (
        ("channel_private", "private", "chat_admin_required"),
        "restricted",
        "需要管理员权限或群已转私密",
    ),
)


def classify_join_error(exc: BaseException | str) -> tuple[str, str]:
    """把加群异常翻成 ``(分类, 中文说明)``。

    返回 ``("already_member", ...)`` 表示"其实已经进去了"，调用方按成功处理。
    """
    text = str(exc).lower()
    for needles, category, label in JOIN_ERROR_RULES:
        if any(needle in text for needle in needles):
            return category, label
    return "unknown", str(exc)[:200] or "未知错误"
