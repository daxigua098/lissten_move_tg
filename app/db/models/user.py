"""账号、会话、登录历史、审计日志与系统设置模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
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
# 会员账号固定这个角色（一人一号，没有内部权限差异），不参与 ROLE_RANK 比较
ROLE_OWNER = "owner"

# 账号类型：平台自用 / 代理 / 会员（多租户销售模型）
ACCOUNT_TYPE_PLATFORM = "platform"
ACCOUNT_TYPE_AGENT = "agent"
ACCOUNT_TYPE_MEMBER = "member"
ACCOUNT_TYPES = (ACCOUNT_TYPE_PLATFORM, ACCOUNT_TYPE_AGENT, ACCOUNT_TYPE_MEMBER)


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
    # 直属上级：代理的上级是代理或平台，会员的上级是开设它的代理。
    # 代理树靠这个自引用表达，不限层级（递归查询在 service 层解决）。
    parent_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
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
    # 登录后回填，便于按租户踢会话（P1 只记归属，不做隔离）
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
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
    # 登录成功后回填，失败尝试为空
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
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
    # 写操作所属租户，便于筛选与对账
    tenant_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
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
