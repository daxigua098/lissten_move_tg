"""Telegram 资源模型：执行账号、控制 Bot 与聊天对象。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin
from app.db.models.tenant import TenantOwnedMixin

# 执行账号状态
ACCOUNT_PENDING = "pending_login"
ACCOUNT_ACTIVE = "active"
ACCOUNT_RESTRICTED = "restricted"
ACCOUNT_DISABLED = "disabled"
ACCOUNT_STATUSES = (ACCOUNT_PENDING, ACCOUNT_ACTIVE, ACCOUNT_RESTRICTED, ACCOUNT_DISABLED)

# 账号用途：执行账号（采集 / 搬运 / 监听）与发信息账号（冷触达 / 对话）物理隔离
ACCOUNT_PURPOSE_LISTEN = "listen"
ACCOUNT_PURPOSE_OUTREACH = "outreach"
ACCOUNT_PURPOSES = (ACCOUNT_PURPOSE_LISTEN, ACCOUNT_PURPOSE_OUTREACH)
ACCOUNT_PURPOSE_LABELS: dict[str, str] = {
    ACCOUNT_PURPOSE_LISTEN: "执行账号",
    ACCOUNT_PURPOSE_OUTREACH: "发信息账号",
}

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
    # 用途：listen=执行账号（采集 / 搬运 / 监听），outreach=发信息账号（冷触达 / 对话）
    purpose: Mapped[str] = mapped_column(
        String(16),
        default=ACCOUNT_PURPOSE_LISTEN,
        index=True,
    )
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
    # 会员接入发信息账号时确认「账号归我所有并已获授权使用」的留痕
    owner_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    owner_confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    owner_confirm_version: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # 发信息账号的接码地址（带 token，属凭据，加密存储）
    code_url_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 最近一次取到的登录验证码 / 二级密码（同样是凭据，取到就先存下来）
    last_code_enc: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_code_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_2fa_enc: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 接码平台提示「30 分钟内没有新验证码」时的冷却截止时间
    code_cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    # 发信息账号退役（软删）：保留审计与联系档案，只停止外呼
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retire_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)

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


class ChatDirectory(TimestampMixin, Base):
    """群 / 频道的客观信息：全平台共享一份，不带租户。

    "这个群是什么"（Telegram 原标题、类型、成员数）人人一致，因此只存一份；
    "我和这个群什么关系"（备注、角色、启停）在 ``tenant_chats`` 里按租户各存一份。
    """

    __tablename__ = "chat_directory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    chat_type: Mapped[str] = mapped_column(String(16), default=CHAT_GROUP, index=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_private: Mapped[bool] = mapped_column(Boolean, default=False)
    member_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_kind: Mapped[str] = mapped_column(String(16), default=SOURCE_KIND_LOCAL, index=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<ChatDirectory {self.tg_id} {self.title}>"


class TenantChat(TenantOwnedMixin, TimestampMixin, Base):
    """租户 × 群的关系：备注名、标签、是源还是接收组、启停。

    ``directory`` 用 ``lazy="joined"`` 预加载：客观字段通过下面几个只读属性
    （``tg_id`` / ``title`` / ``chat_type`` …）透传，业务代码可以照旧读，
    但**不能**再直接赋值——要改客观信息就改 ``chat.directory.xxx``。
    """

    __tablename__ = "tenant_chats"
    # 同一租户下同一个群只有一行配置（第二个租户配置同一个群时是新的一行）
    __table_args__ = (UniqueConstraint("tenant_id", "chat_id", name="uq_tenant_chats_tenant_chat"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        ForeignKey("chat_directory.id", ondelete="CASCADE"),
        index=True,
    )
    # 用户自定义备注名（各租户独立）
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tags: Mapped[str] = mapped_column(Text, default="[]")
    # 角色与启停：同一个群既可以是监听源，也可以是接收组
    is_source: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    source_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_target: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    target_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    target_role: Mapped[str] = mapped_column(String(16), default=TARGET_ROLE_CONTENT)
    joined: Mapped[bool] = mapped_column(Boolean, default=False)
    can_post: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    directory: Mapped[ChatDirectory] = relationship(lazy="joined")

    # --- 客观字段透传（只读，写入请改 directory） -------------------------

    @property
    def tg_id(self) -> int:
        return int(self.directory.tg_id)

    @property
    def chat_type(self) -> str:
        return self.directory.chat_type

    @property
    def title(self) -> str | None:
        return self.directory.title

    @property
    def username(self) -> str | None:
        return self.directory.username

    @property
    def is_private(self) -> bool:
        return bool(self.directory.is_private)

    @property
    def member_count(self) -> int | None:
        return self.directory.member_count

    @property
    def source_kind(self) -> str:
        return self.directory.source_kind

    def display(self) -> str:
        """展示用名称：备注名 > 原标题 > 用户名 > #tg_id。"""
        return self.display_name or self.title or self.username or f"#{self.tg_id}"

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<TenantChat {self.id} tenant={self.tenant_id} chat={self.chat_id}>"
