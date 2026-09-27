"""代理额度：余额表与流水表（P3 额度与代理）。

三条铁律：

1. **额度挂在代理账号上**（`users.account_type='agent'`），不挂在租户上；
2. **三类额度互不通用**：会员 / 代理 / 试用，各有各的余额与流水；
3. **一切变动都走事务并写流水**，余额永远能由流水倒推出来（对账以流水为准）。

平台账号**不建余额行**——"没有行"就等于"不受额度限制"。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, utc_now

# 三类额度
QUOTA_MEMBER = "member"
QUOTA_AGENT = "agent"
QUOTA_TRIAL = "trial"
QUOTA_TYPES = (QUOTA_MEMBER, QUOTA_AGENT, QUOTA_TRIAL)
QUOTA_LABELS: dict[str, str] = {
    QUOTA_MEMBER: "会员额度",
    QUOTA_AGENT: "代理额度",
    QUOTA_TRIAL: "试用额度",
}
# 额度类型 → 余额列名
QUOTA_FIELD: dict[str, str] = {
    QUOTA_MEMBER: "member_quota",
    QUOTA_AGENT: "agent_quota",
    QUOTA_TRIAL: "trial_quota",
}

# tenants.quota_type：该租户当前占的是哪类额度；平台开的号记 none
QUOTA_NONE = "none"
TENANT_QUOTA_TYPES = (QUOTA_MEMBER, QUOTA_TRIAL, QUOTA_NONE)

# 流水动作
ACTION_ALLOCATE_OUT = "allocate_out"
ACTION_ALLOCATE_IN = "allocate_in"
ACTION_RECLAIM_OUT = "reclaim_out"
ACTION_RECLAIM_IN = "reclaim_in"
ACTION_OPEN_MEMBER = "open_member"
ACTION_OPEN_TRIAL = "open_trial"
ACTION_OPEN_AGENT = "open_agent"
ACTION_EXPIRE_RELEASE = "expire_release"
ACTION_RENEW_CONSUME = "renew_consume"
ACTION_MANUAL_ADJUST = "manual_adjust"
QUOTA_ACTIONS = (
    ACTION_ALLOCATE_OUT,
    ACTION_ALLOCATE_IN,
    ACTION_RECLAIM_OUT,
    ACTION_RECLAIM_IN,
    ACTION_OPEN_MEMBER,
    ACTION_OPEN_TRIAL,
    ACTION_OPEN_AGENT,
    ACTION_EXPIRE_RELEASE,
    ACTION_RENEW_CONSUME,
    ACTION_MANUAL_ADJUST,
)
ACTION_LABELS: dict[str, str] = {
    ACTION_ALLOCATE_OUT: "划拨给下级",
    ACTION_ALLOCATE_IN: "上级划拨进来",
    ACTION_RECLAIM_OUT: "上级回收",
    ACTION_RECLAIM_IN: "回收下级余额",
    ACTION_OPEN_MEMBER: "开正式会员",
    ACTION_OPEN_TRIAL: "开试用账号",
    ACTION_OPEN_AGENT: "开下级代理",
    ACTION_EXPIRE_RELEASE: "到期释放",
    ACTION_RENEW_CONSUME: "续期占用",
    ACTION_MANUAL_ADJUST: "平台调账",
}


class AgentQuota(TimestampMixin, Base):
    """代理额度余额：一个代理一行，三个余额各自不为负。"""

    __tablename__ = "agent_quotas"
    __table_args__ = (
        CheckConstraint("member_quota >= 0", name="member_quota_non_negative"),
        CheckConstraint("agent_quota >= 0", name="agent_quota_non_negative"),
        CheckConstraint("trial_quota >= 0", name="trial_quota_non_negative"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    member_quota: Mapped[int] = mapped_column(Integer, default=0)
    agent_quota: Mapped[int] = mapped_column(Integer, default=0)
    trial_quota: Mapped[int] = mapped_column(Integer, default=0)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<AgentQuota user={self.user_id} member={self.member_quota} "
            f"agent={self.agent_quota} trial={self.trial_quota}>"
        )


class QuotaLedger(Base):
    """额度流水：一次变动一行；一次划拨写两行，靠 ``transfer_id`` 串起来。"""

    __tablename__ = "quota_ledger"
    __table_args__ = (
        CheckConstraint("change <> 0", name="change_non_zero"),
        Index("ix_quota_ledger_subject_created", "subject_user_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 同一笔转移的关联码；单边动作（开号扣减）为空
    transfer_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 余额变动方
    subject_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 冗余快照：账号删除后仍可追溯
    subject_username: Mapped[str] = mapped_column(String(64))
    quota_type: Mapped[str] = mapped_column(String(8), index=True)
    action: Mapped[str] = mapped_column(String(24), index=True)
    # 正数=增加，负数=减少
    change: Mapped[int] = mapped_column(Integer)
    # 变动后余额，用于对账
    balance_after: Mapped[int] = mapped_column(Integer)
    # 涉及的会员租户 / 下级账号
    related_tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    related_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    actor_username: Mapped[str] = mapped_column(String(64), default="system")
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        index=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<QuotaLedger {self.action} {self.quota_type} {self.change:+d} → {self.balance_after}>"
        )


__all__ = [
    "ACTION_ALLOCATE_IN",
    "ACTION_ALLOCATE_OUT",
    "ACTION_EXPIRE_RELEASE",
    "ACTION_LABELS",
    "ACTION_MANUAL_ADJUST",
    "ACTION_OPEN_AGENT",
    "ACTION_OPEN_MEMBER",
    "ACTION_OPEN_TRIAL",
    "ACTION_RECLAIM_IN",
    "ACTION_RECLAIM_OUT",
    "ACTION_RENEW_CONSUME",
    "AgentQuota",
    "QUOTA_ACTIONS",
    "QUOTA_AGENT",
    "QUOTA_FIELD",
    "QUOTA_LABELS",
    "QUOTA_MEMBER",
    "QUOTA_NONE",
    "QUOTA_TRIAL",
    "QUOTA_TYPES",
    "QuotaLedger",
    "TENANT_QUOTA_TYPES",
]
