"""账号、会话、登录历史、审计日志与系统设置模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, utc_now

# 角色常量与等级：等级用于 require_role 的比较
ROLE_VIEWER = "viewer"
ROLE_SUB_ADMIN = "sub_admin"
ROLE_SUPER_ADMIN = "super_admin"
ROLE_RANK: dict[str, int] = {
    ROLE_VIEWER: 1,
    ROLE_SUB_ADMIN: 2,
    ROLE_SUPER_ADMIN: 3,
}

# 账号类型：平台自用 / 代理 / 会员（多租户销售模型）
ACCOUNT_TYPE_PLATFORM = "platform"
ACCOUNT_TYPE_AGENT = "agent"
ACCOUNT_TYPE_MEMBER = "member"
ACCOUNT_TYPES = (ACCOUNT_TYPE_PLATFORM, ACCOUNT_TYPE_AGENT, ACCOUNT_TYPE_MEMBER)

# 租户类型与状态
TENANT_KIND_SELF = "self"
TENANT_KIND_MEMBER = "member"
TENANT_KINDS = (TENANT_KIND_SELF, TENANT_KIND_MEMBER)

TENANT_STATUS_ACTIVE = "active"
TENANT_STATUS_EXPIRED = "expired"
TENANT_STATUS_SUSPENDED = "suspended"
TENANT_STATUSES = (TENANT_STATUS_ACTIVE, TENANT_STATUS_EXPIRED, TENANT_STATUS_SUSPENDED)

# 自营租户固定主键：存量业务数据迁移时全部挂到它下面，永不过期
SELF_TENANT_ID = 1
SELF_TENANT_NAME = "自营"


class Tenant(TimestampMixin, Base):
    """租户：业务数据的唯一归属单位，会员账号与租户 1:1。"""

    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(
            "kind <> 'member' OR owner_user_id IS NOT NULL",
            name="member_owner_required",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    kind: Mapped[str] = mapped_column(String(16), default=TENANT_KIND_MEMBER)
    status: Mapped[str] = mapped_column(
        String(16),
        default=TENANT_STATUS_ACTIVE,
        index=True,
    )
    # 有效期（UTC）；自营租户为 NULL 表示永不过期。P4 负责到期判定与强停
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    # 该租户的登录账号：只有会员租户有值，且一个账号最多属于一个租户
    owner_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
    )
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Tenant {self.id} {self.name} kind={self.kind} status={self.status}>"


class User(TimestampMixin, Base):
    """后台账号：平台账号、代理账号与会员账号共用一张表。"""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 登录发生在确定租户之前，因此用户名必须全局唯一，不能降级为租户内唯一
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(32), default=ROLE_VIEWER, index=True)
    account_type: Mapped[str] = mapped_column(
        String(16),
        default=ACCOUNT_TYPE_PLATFORM,
        index=True,
    )
    # 只有会员账号有租户；平台与代理账号为空。
    # 与 tenants.owner_user_id 互为外键（循环依赖），这里用 use_alter 打断排序环，
    # 否则元数据排序会报警告；SQLite 下建表时依旧内联成普通外键。
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL", use_alter=True),
        nullable=True,
        index=True,
    )
    display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 默认要求首次登录改密（内置管理员与新建子管理员都适用）
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<User {self.username} type={self.account_type} "
            f"role={self.role} enabled={self.enabled}>"
        )


class WebSession(Base):
    """服务端会话（只存令牌哈希，可远程撤销）。"""

    __tablename__ = "web_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(32))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class LoginHistory(Base):
    """登录尝试历史。"""

    __tablename__ = "login_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )


class AuditLog(Base):
    """写操作审计日志。"""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), default="anonymous", index=True)
    method: Mapped[str] = mapped_column(String(16))
    path: Mapped[str] = mapped_column(String(512), index=True)
    status_code: Mapped[int] = mapped_column(Integer)
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )


class SystemSetting(Base):
    """可在后台修改的运行时设置（键值对，值为 JSON 字符串）。"""

    __tablename__ = "system_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="{}")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )
