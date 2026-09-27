"""发信息账号（冷触达）运营态：档位、状态与按天计数。

与 ``tg_accounts``（``purpose=outreach``）一一对应；本模块只承载运营态与计数，
发消息 / 排队 / 联系锁等能力在后续阶段接入。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.outreach_capture import (
    OUTREACH_CONTACTED,
    OUTREACH_REFUSED,
    OUTREACH_REPLIED,
    OUTREACH_WAITING_SENDER_ACCOUNT,
)
from app.db.base import Base, TimestampMixin, utc_now
from app.db.models.tenant import TenantOwnedMixin

# 账号档位：决定每日首次私聊上限与首触冷却
TIER_NEW = "NEW"
TIER_WARMING = "WARMING"
TIER_STANDARD = "STANDARD"
TIER_MATURE = "MATURE"
TIERS = (TIER_NEW, TIER_WARMING, TIER_STANDARD, TIER_MATURE)
TIER_LABELS: dict[str, str] = {
    TIER_NEW: "新号（<14 天）",
    TIER_WARMING: "养号（2~8 周）",
    TIER_STANDARD: "普通（2~6 月）",
    TIER_MATURE: "成熟健康号",
}
# 档位 → (每日首次私聊上限, 首触冷却秒数)；保守运营值，不是 Telegram 官方规则
TIER_DEFAULTS: dict[str, tuple[int, int]] = {
    TIER_NEW: (3, 5 * 3600),
    TIER_WARMING: (5, 3 * 3600),
    TIER_STANDARD: (10, 2 * 3600),
    TIER_MATURE: (20, 90 * 60),
}

# 账号运营态（与登录态 tg_accounts.status 解耦）
STATE_NEW = "NEW"
STATE_READY = "READY"
STATE_COOLING = "COOLING"
STATE_CAPPED = "CAPPED"
STATE_LIMITED = "LIMITED"
STATE_PAUSED = "PAUSED"
STATE_DISABLED = "DISABLED"
ACCOUNT_STATES = (
    STATE_NEW,
    STATE_READY,
    STATE_COOLING,
    STATE_CAPPED,
    STATE_LIMITED,
    STATE_PAUSED,
    STATE_DISABLED,
)
ACCOUNT_STATE_LABELS: dict[str, str] = {
    STATE_NEW: "新接入",
    STATE_READY: "可用",
    STATE_COOLING: "冷却中",
    STATE_CAPPED: "今日额度已用完",
    STATE_LIMITED: "被限制",
    STATE_PAUSED: "人工暂停",
    STATE_DISABLED: "永久停用",
}


class OutreachAccountState(TenantOwnedMixin, TimestampMixin, Base):
    """发信息账号的冷触达运营态。"""

    __tablename__ = "outreach_account_states"

    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    tier: Mapped[str] = mapped_column(String(16), default=TIER_NEW)
    state: Mapped[str] = mapped_column(String(16), default=STATE_NEW, index=True)
    # 空表示按档位推导，非空则覆盖档位默认值
    daily_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cooldown_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_cold_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    flood_wait_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    limited_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    limited_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class OutreachAccountDaily(TenantOwnedMixin, TimestampMixin, Base):
    """发信息账号按天计数（租户本地日期）。"""

    __tablename__ = "outreach_account_daily"
    __table_args__ = (UniqueConstraint("account_id", "day", name="uq_outreach_account_daily"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="CASCADE"),
        index=True,
    )
    day: Mapped[date] = mapped_column(Date, index=True)
    first_contact_sent: Mapped[int] = mapped_column(Integer, default=0)
    follow_up_sent: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    limited_hits: Mapped[int] = mapped_column(Integer, default=0)


# 联系人态：前四个与 app.core.outreach_capture 的 OUTREACH_* 保持一致
CONTACT_QUEUED = "QUEUED"
CONTACT_FROZEN = "FROZEN"
CONTACT_STATES = (
    OUTREACH_WAITING_SENDER_ACCOUNT,
    CONTACT_QUEUED,
    OUTREACH_CONTACTED,
    OUTREACH_REPLIED,
    OUTREACH_REFUSED,
    CONTACT_FROZEN,
)
CONTACT_STATE_LABELS: dict[str, str] = {
    OUTREACH_WAITING_SENDER_ACCOUNT: "待发信息账号",
    CONTACT_QUEUED: "排队中",
    OUTREACH_CONTACTED: "已联系",
    OUTREACH_REPLIED: "已回复",
    OUTREACH_REFUSED: "已拒绝",
    CONTACT_FROZEN: "已冻结",
}

# 会话归属：普通账号 / Bot（第二阶段转交后）
OWNER_ACCOUNT = "account"
OWNER_BOT = "bot"

# 话术模板：平台共享（只读）与会员私有
TEMPLATE_SCOPE_PLATFORM = "platform"
TEMPLATE_SCOPE_TENANT = "tenant"
TEMPLATE_SCOPES = (TEMPLATE_SCOPE_PLATFORM, TEMPLATE_SCOPE_TENANT)
TEMPLATE_FIRST_CONTACT = "first_contact"
TEMPLATE_FOLLOW_UP = "follow_up"
TEMPLATE_KINDS = (TEMPLATE_FIRST_CONTACT, TEMPLATE_FOLLOW_UP)
TEMPLATE_KIND_LABELS: dict[str, str] = {
    TEMPLATE_FIRST_CONTACT: "首条招呼",
    TEMPLATE_FOLLOW_UP: "跟进",
}

# 冷触达任务
TASK_FIRST_CONTACT = "first_contact"
TASK_FOLLOW_UP = "follow_up"
TASK_KINDS = (TASK_FIRST_CONTACT, TASK_FOLLOW_UP)
TASK_QUEUED = "QUEUED"
TASK_ASSIGNED = "ASSIGNED"
TASK_SENDING = "SENDING"
TASK_SENT = "SENT"
TASK_FAILED = "FAILED"
TASK_BLOCKED = "BLOCKED"
TASK_REFUSED = "REFUSED"
TASK_CANCELLED = "CANCELLED"
TASK_STATUSES = (
    TASK_QUEUED,
    TASK_ASSIGNED,
    TASK_SENDING,
    TASK_SENT,
    TASK_FAILED,
    TASK_BLOCKED,
    TASK_REFUSED,
    TASK_CANCELLED,
)
TASK_STATUS_LABELS: dict[str, str] = {
    TASK_QUEUED: "排队中",
    TASK_ASSIGNED: "已分配账号",
    TASK_SENDING: "发送中",
    TASK_SENT: "已发送",
    TASK_FAILED: "发送失败",
    TASK_BLOCKED: "已阻塞",
    TASK_REFUSED: "已拒绝",
    TASK_CANCELLED: "已取消",
}


class OutreachContact(TenantOwnedMixin, TimestampMixin, Base):
    """全局联系锁：一个用户一份档案，承载跨账号锁与会话归属。"""

    __tablename__ = "outreach_contacts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "tg_user_id", name="uq_outreach_contacts_tenant_user"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    member_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("member_profiles.id", ondelete="SET NULL"),
        nullable=True,
    )
    first_lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
    )
    first_contact_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_contact_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_contact_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 会话归属：回复后锁定到具体账号（或第二阶段的 Bot），永不轮换
    owner_type: Mapped[str] = mapped_column(String(8), default=OWNER_ACCOUNT)
    owner_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    owner_bot_id: Mapped[int | None] = mapped_column(
        ForeignKey("control_bots.id", ondelete="SET NULL"),
        nullable=True,
    )
    contact_count: Mapped[int] = mapped_column(Integer, default=0)
    follow_up_count: Mapped[int] = mapped_column(Integer, default=0)
    contact_state: Mapped[str] = mapped_column(
        String(32),
        default=OUTREACH_WAITING_SENDER_ACCOUNT,
        index=True,
    )
    reply_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outbound_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 跨账号重新联系的最早时间；严格模式可置为永久（9999-12-31）
    global_lock_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    frozen_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    identity_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)


class OutreachTemplate(TimestampMixin, Base):
    """话术模板：平台共享（``tenant_id`` 为空，只读）+ 会员私有。

    会员「选用」平台模板时复制成自己的租户副本，记录来源与版本；
    因此本表不挂多租户作用域，查询时显式按 ``tenant_id`` 过滤。
    """

    __tablename__ = "outreach_templates"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "name", name="uq_outreach_templates_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(16), default=TEMPLATE_SCOPE_TENANT, index=True)
    # 平台模板为空；会员模板归属该租户
    tenant_id: Mapped[int | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16), default=TEMPLATE_FIRST_CONTACT)
    text: Mapped[str] = mapped_column(Text, default="")
    variables: Mapped[str] = mapped_column(Text, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    source_template_id: Mapped[int | None] = mapped_column(
        ForeignKey("outreach_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)


class OutreachSettings(TimestampMixin, Base):
    """租户级冷触达策略。"""

    __tablename__ = "outreach_settings"

    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        primary_key=True,
    )
    default_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=2 * 3600)
    # 跨账号重新联系：默认 30 天；严格模式视为永久
    cross_account_lock_days: Mapped[int] = mapped_column(Integer, default=30)
    strict_permanent_lock: Mapped[bool] = mapped_column(Boolean, default=False)
    follow_up_days: Mapped[int] = mapped_column(Integer, default=7)
    follow_up_max: Mapped[int] = mapped_column(Integer, default=1)
    # JSON 数组 [开始, 结束]；空表示不限制
    working_hours: Mapped[str] = mapped_column(Text, default="[]")
    daily_pool_cap: Mapped[int | None] = mapped_column(Integer, nullable=True)
    kill_switch: Mapped[bool] = mapped_column(Boolean, default=False)
    delete_session_on_account_delete: Mapped[bool] = mapped_column(Boolean, default=False)


class OutreachTask(TenantOwnedMixin, TimestampMixin, Base):
    """冷触达任务队列：幂等入队 + 串行消费。"""

    __tablename__ = "outreach_tasks"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_outreach_tasks_dedupe_key"),
        Index("ix_outreach_tasks_tenant_status_plan", "tenant_id", "status", "scheduled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    lead_id: Mapped[int | None] = mapped_column(
        ForeignKey("leads.id", ondelete="SET NULL"),
        nullable=True,
    )
    contact_id: Mapped[int] = mapped_column(
        ForeignKey("outreach_contacts.id", ondelete="CASCADE"),
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), default=TASK_FIRST_CONTACT)
    status: Mapped[str] = mapped_column(String(16), default=TASK_QUEUED, index=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    template_id: Mapped[int | None] = mapped_column(
        ForeignKey("outreach_templates.id", ondelete="SET NULL"),
        nullable=True,
    )
    rendered_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    scheduled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    dedupe_key: Mapped[str] = mapped_column(String(128))
