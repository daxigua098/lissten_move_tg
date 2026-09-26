"""资源发现模块的数据模型（需求书 v1.1 第 5 章）。

五张表：

- ``tg_resources``：群 / 频道资源，``tg_id`` 唯一，候选 → 已探测 → 已采纳 → 已淘汰；
- ``resource_probe_logs``：每次探测的指标快照，趋势曲线的数据源；
- ``resource_discover_tasks``：关键词 / 句式 / 热门词发现任务；
- ``resource_join_tasks``：加群队列（限速排队，按账号串行）；
- ``resource_quotas``：按账号按天的配额计数。

保留策略：资源库与探测日志长期保留（日志按 ``retention`` 可配），
**不参与**线上线索的 3 天清理——资源是选源依据，删了就白干了。
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# 资源状态机：候选 → 已探测 → 已采纳 → 已淘汰
RESOURCE_CANDIDATE = "candidate"
RESOURCE_PROBED = "probed"
RESOURCE_ADOPTED = "adopted"
RESOURCE_RETIRED = "retired"
RESOURCE_STATUSES = (
    RESOURCE_CANDIDATE,
    RESOURCE_PROBED,
    RESOURCE_ADOPTED,
    RESOURCE_RETIRED,
)

# 资源在 Telegram 侧的可达状态
STATE_ACTIVE = "active"
STATE_PRIVATE = "private"
STATE_BANNED = "banned"
STATE_LEFT = "left"
RESOURCE_STATES = (STATE_ACTIVE, STATE_PRIVATE, STATE_BANNED, STATE_LEFT)

# 发现来源（F-R02 ~ F-R05）
DISCOVER_KEYWORD = "keyword"
DISCOVER_LINK = "link"
DISCOVER_HOTWORD = "hotword"
DISCOVER_PHRASE = "phrase"
DISCOVER_MANUAL = "manual"
DISCOVER_SOURCES = (
    DISCOVER_KEYWORD,
    DISCOVER_LINK,
    DISCOVER_HOTWORD,
    DISCOVER_PHRASE,
    DISCOVER_MANUAL,
)

# 加群队列
JOIN_ACTION_JOIN = "join"
JOIN_ACTION_LEAVE = "leave"
JOIN_ACTIONS = (JOIN_ACTION_JOIN, JOIN_ACTION_LEAVE)

JOIN_PENDING = "pending"
JOIN_RUNNING = "running"
JOIN_SUCCESS = "success"
JOIN_FAILED = "failed"
JOIN_WAITING_APPROVAL = "waiting_approval"
JOIN_STATUSES = (
    JOIN_PENDING,
    JOIN_RUNNING,
    JOIN_SUCCESS,
    JOIN_FAILED,
    JOIN_WAITING_APPROVAL,
)

# 探测结果
PROBE_OK = "ok"
PROBE_FAILED = "failed"
PROBE_SKIP = "skip"
PROBE_RESULTS = (PROBE_OK, PROBE_FAILED, PROBE_SKIP)


class TgResource(TimestampMixin, Base):
    """一个公开群 / 频道资源。"""

    __tablename__ = "tg_resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 去重键。**可空**：私密群的邀请链接在"还没加入"时拿不到数字 ID，
    # 先以 tg_id=NULL + invite_link 入库，加入成功后回填并按 tg_id 归一。
    # 唯一索引在 SQLite / PostgreSQL 下都允许多个 NULL，正好满足这个用法。
    tg_id: Mapped[int | None] = mapped_column(BigInteger, unique=True, index=True, nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    invite_link: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    title: Mapped[str] = mapped_column(String(128), default="")
    title_history: Mapped[str] = mapped_column(Text, default="[]")
    about: Mapped[str | None] = mapped_column(Text, nullable=True)
    chat_type: Mapped[str] = mapped_column(String(16), default="group", index=True)
    member_count: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    member_count_approx: Mapped[bool] = mapped_column(Boolean, default=False)
    member_count_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    posts_per_day: Mapped[float | None] = mapped_column(Float, nullable=True)
    human_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    unique_senders: Mapped[int | None] = mapped_column(Integer, nullable=True)
    link_density: Mapped[float | None] = mapped_column(Float, nullable=True)
    activity_score: Mapped[float | None] = mapped_column(Float, nullable=True, index=True)
    lead_potential: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_active_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    language: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    categories: Mapped[str] = mapped_column(Text, default="[]")
    is_index_group: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    index_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 人工修正过的字段 JSON 数组（刷新时跳过，不被机器值覆盖）
    manual_locked: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(
        String(16),
        default=RESOURCE_CANDIDATE,
        index=True,
    )
    resource_state: Mapped[str | None] = mapped_column(String(16), nullable=True)
    discovered_from: Mapped[str | None] = mapped_column(String(255), nullable=True)
    discovered_by: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_blacklisted: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    blacklist_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_favorite: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    # 人工备注（P-R02 的操作项；与黑名单原因分开存，避免互相覆盖）
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    adopted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    adopted_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adopted_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    # 采纳后回看（F-R14）：第 7 天看这个源到底产出多少
    adopted_review_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    adopted_lead_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_probed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    probe_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    next_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    first_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<TgResource {self.tg_id} {self.title} {self.status}>"


class ResourceProbeLog(TimestampMixin, Base):
    """一次探测的指标快照。"""

    __tablename__ = "resource_probe_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("tg_resources.id", ondelete="CASCADE"),
        index=True,
    )
    probed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    member_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    activity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    posts_per_day: Mapped[float | None] = mapped_column(Float, nullable=True)
    human_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    link_density: Mapped[float | None] = mapped_column(Float, nullable=True)
    lead_potential: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_active_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    result: Mapped[str] = mapped_column(String(16), default=PROBE_OK, index=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    requests_used: Mapped[int] = mapped_column(Integer, default=0)


class ResourceDiscoverTask(TimestampMixin, Base):
    """发现任务：一个关键词 / 句式 / 热门词。"""

    __tablename__ = "resource_discover_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), default=DISCOVER_KEYWORD, index=True)
    keyword: Mapped[str] = mapped_column(String(64))
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    hits: Mapped[int] = mapped_column(Integer, default=0)
    new_found: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    flood_waits: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("kind", "keyword", name="uq_discover_kind_keyword"),)


class ResourceJoinTask(TimestampMixin, Base):
    """加群 / 退群队列。"""

    __tablename__ = "resource_join_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("tg_resources.id", ondelete="CASCADE"),
        index=True,
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[str] = mapped_column(String(16), default=JOIN_ACTION_JOIN)
    status: Mapped[str] = mapped_column(String(16), default=JOIN_PENDING, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    scheduled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        index=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class ResourceQuota(TimestampMixin, Base):
    """按账号按天滚动的配额计数。"""

    __tablename__ = "resource_quotas"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="CASCADE"),
        index=True,
    )
    day: Mapped[date] = mapped_column(Date, index=True)
    searches: Mapped[int] = mapped_column(Integer, default=0)
    joins: Mapped[int] = mapped_column(Integer, default=0)
    leaves: Mapped[int] = mapped_column(Integer, default=0)
    probes: Mapped[int] = mapped_column(Integer, default=0)
    flood_waits: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("account_id", "day", name="uq_quota_account_day"),)
