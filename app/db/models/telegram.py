"""Telegram 资源模型：执行账号、控制 Bot 与聊天对象。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.tenant import TenantOwnedMixin

# 执行账号状态
ACCOUNT_PENDING = "pending_login"
ACCOUNT_ACTIVE = "active"
ACCOUNT_RESTRICTED = "restricted"
ACCOUNT_DISABLED = "disabled"
ACCOUNT_STATUSES = (ACCOUNT_PENDING, ACCOUNT_ACTIVE, ACCOUNT_RESTRICTED, ACCOUNT_DISABLED)

# 聊天对象类型与来源
CHAT_CHANNEL = "channel"
CHAT_SUPERGROUP = "supergroup"
CHAT_GROUP = "group"
SOURCE_KIND_LOCAL = "local"
SOURCE_KIND_REMOTE = "remote"

# 接收组用途
TARGET_ROLE_CONTENT = "content"
TARGET_ROLE_LEAD = "lead"
TARGET_ROLES = (TARGET_ROLE_CONTENT, TARGET_ROLE_LEAD)


class TgAccount(TenantOwnedMixin, TimestampMixin, Base):
    """执行账号：真正执行采集与投递的 Telegram 用户账号。"""

    __tablename__ = "tg_accounts"
    # 账号名只在租户内唯一：两个客户都可以有"主号"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_tg_accounts_tenant_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    phone_masked: Mapped[str] = mapped_column(String(32))
    # 敏感字段以 Fernet 加密存储
    phone_enc: Mapped[str] = mapped_column(String(255))
    api_id_enc: Mapped[str] = mapped_column(String(255))
    api_hash_enc: Mapped[str] = mapped_column(String(255))
    session_name: Mapped[str] = mapped_column(String(128))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default=ACCOUNT_PENDING, index=True)
    health_score: Mapped[int] = mapped_column(Integer, default=100)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<TgAccount {self.name} status={self.status}>"


class ControlBot(TenantOwnedMixin, TimestampMixin, Base):
    """控制 Bot：接收后台与群内指令、发送通知（不做采集与投递）。"""

    __tablename__ = "control_bots"
    # 机器人名只在租户内唯一
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_control_bots_tenant_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), index=True)
    bot_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bot_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    token_enc: Mapped[str] = mapped_column(String(255))
    admin_ids: Mapped[str] = mapped_column(Text, default="[]")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ControlBot {self.name} @{self.bot_username}>"


class Chat(TimestampMixin, Base):
    """聊天对象：监听源与接收目标共用一张表。"""

    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    chat_type: Mapped[str] = mapped_column(String(16), default=CHAT_GROUP, index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 用户自定义备注名，优先于 Telegram 原标题显示
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    joined: Mapped[bool] = mapped_column(Boolean, default=False)
    can_post: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    member_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tags: Mapped[str] = mapped_column(Text, default="[]")
    source_kind: Mapped[str] = mapped_column(String(16), default=SOURCE_KIND_LOCAL, index=True)
    # 角色与启停：同一个群既可以是监听源，也可以是接收组
    is_source: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_target: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    target_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    target_role: Mapped[str] = mapped_column(String(16), default=TARGET_ROLE_CONTENT)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Chat {self.tg_id} {self.title}>"
