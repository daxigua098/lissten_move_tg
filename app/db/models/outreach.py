"""发信息账号（冷触达）运营态：档位、状态与按天计数。

与 ``tg_accounts``（``purpose=outreach``）一一对应；本模块只承载运营态与计数，
发消息 / 排队 / 联系锁等能力在后续阶段接入。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
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
