"""功能包模板、租户功能授权与用量限制（P2 账号体系）。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, utc_now

# 功能块：只有会员账号按块授权。
# 基础能力（登录、改密、TG 账号、机器人、运行总览）恒开，**不落库**，
# 避免"基础包被误删导致整个账号不可用"。
MODULE_CARRY = "carry"
MODULE_MONITOR = "monitor"
MODULE_DISCOVERY = "discovery"
MODULES = (MODULE_CARRY, MODULE_MONITOR, MODULE_DISCOVERY)
MODULE_LABELS: dict[str, str] = {
    MODULE_CARRY: "搬运帖子",
    MODULE_MONITOR: "监听会员",
    MODULE_DISCOVERY: "资源发现",
}

# 功能包模板类型
PLAN_TRIAL = "trial"
PLAN_STANDARD = "standard"
PLAN_CUSTOM = "custom"
PLAN_KINDS = (PLAN_TRIAL, PLAN_STANDARD, PLAN_CUSTOM)


class PlanTemplate(TimestampMixin, Base):
    """功能包模板：开号时的快捷选项，**不是**运行时授权依据。"""

    __tablename__ = "plan_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16), default=PLAN_STANDARD, index=True)
    # JSON 数组，如 ["carry"]
    modules: Mapped[str] = mapped_column(Text, default="[]")
    # JSON 对象，如 {"max_routes": 1, "allow_export": false}
    limits: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<PlanTemplate {self.code} kind={self.kind}>"


class TenantModule(Base):
    """租户功能授权：运行时的判断依据，一行一个功能块。

    开着即有一行，关掉只把 ``enabled`` 置 false（保留行以便追溯"曾经开过"）。
    """

    __tablename__ = "tenant_modules"

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    module: Mapped[str] = mapped_column(String(16), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    granted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<TenantModule t={self.tenant_id} {self.module} enabled={self.enabled}>"


class TenantLimit(TimestampMixin, Base):
    """租户用量限制：只有试用会写具体数值，正式会员留空 = 不限。"""

    __tablename__ = "tenant_limits"

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    max_routes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_tg_accounts: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_sources: Mapped[int | None] = mapped_column(Integer, nullable=True)
    allow_export: Mapped[bool] = mapped_column(Boolean, default=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<TenantLimit t={self.tenant_id} routes={self.max_routes}>"
