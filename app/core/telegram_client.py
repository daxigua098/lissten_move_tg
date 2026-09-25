"""Telethon 客户端工厂与资料读取。

网络相关逻辑集中在这里；业务层通过参数注入工厂，测试可传替身。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.config import AppConfig
from app.core.errors import ValidationFailedError
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
