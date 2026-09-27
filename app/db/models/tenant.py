"""租户模型与业务表归属混入（P1 多租户地基）。

约定：

- 租户是业务数据的唯一归属单位，会员账号与租户 1:1；
- 自营业务本身也是一个租户，**固定 `id=1`**，存量数据全部挂在它下面；
- 业务表的 `tenant_id` 非空，Python 侧默认值指向自营租户——这是给"还没接
  身份的写入路径"留的单租户兼容口径，P1-04 起由 service 层按当前身份显式写入。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.quota import QUOTA_NONE

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
    # 开设该租户的代理账号；到期释放额度时回到他这里（平台开的号为空）
    owner_agent_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 当前占用的是哪类额度：member / trial / none（平台开的号记 none）
    quota_type: Mapped[str] = mapped_column(String(8), default=QUOTA_NONE, index=True)
    # 是否还占着这 1 个额度：到期释放靠它做幂等，续期靠它判断要不要再扣
    quota_held: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Tenant {self.id} {self.name} kind={self.kind} status={self.status}>"


class TenantOwnedMixin:
    """业务表归属：`tenant_id` 非空，删租户随之级联删除业务数据。

    混入方式（SQLAlchemy 会把列复制到每张子表）：

        class Route(TenantOwnedMixin, TimestampMixin, Base): ...
    """

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        default=SELF_TENANT_ID,
        index=True,
        nullable=False,
    )
