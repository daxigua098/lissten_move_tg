"""线路模型：源 + 业务类型 + 规则 + 接收目标。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, utc_now

# 业务类型与状态常量
BUSINESS_CARRY = "A"
BUSINESS_MONITOR = "B"
BUSINESS_TYPES = (BUSINESS_CARRY, BUSINESS_MONITOR)

TRANSFER_MODE_COPY = "copy"
TRANSFER_MODE_FORWARD = "forward"
TRANSFER_MODES = (TRANSFER_MODE_COPY, TRANSFER_MODE_FORWARD)

AD_POLICY_NONE = "none"
AD_POLICY_EVERY = "every"
AD_POLICY_NTH = "nth"
AD_POLICIES = (AD_POLICY_NONE, AD_POLICY_EVERY, AD_POLICY_NTH)

LISTEN_MODE_ALL = "all"
LISTEN_MODE_KEYWORD = "keyword"
LISTEN_MODES = (LISTEN_MODE_ALL, LISTEN_MODE_KEYWORD)

SENSITIVITY_LOOSE = "loose"
SENSITIVITY_STANDARD = "standard"
SENSITIVITY_STRICT = "strict"
SENSITIVITIES = (SENSITIVITY_LOOSE, SENSITIVITY_STANDARD, SENSITIVITY_STRICT)

BACKFILL_IDLE = "idle"
BACKFILL_RUNNING = "running"
BACKFILL_DONE = "done"
BACKFILL_FAILED = "failed"


class AdAsset(TimestampMixin, Base):
    """广告素材：文案 + 图片 + 链接按钮，线路引用它而不是各自维护文案。"""

    __tablename__ = "ad_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    link_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    link_text: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<AdAsset {self.name}>"


class Route(TimestampMixin, Base):
    """线路：一条源到一组目标的搬运/监听规则。"""

    __tablename__ = "routes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    # 多源线路：一条「线路」可以同时监听多个源，落库时每个源一行，用同一个
    # bundle_id 串起来（水位线按「线路+目标」存，一个源一行才准确）。
    bundle_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    source_chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"),
        index=True,
    )
    business_type: Mapped[str] = mapped_column(String(2), default=BUSINESS_CARRY, index=True)
    exec_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="SET NULL"),
        nullable=True,
    )
    notify_bot_id: Mapped[int | None] = mapped_column(
        ForeignKey("control_bots.id", ondelete="SET NULL"),
        nullable=True,
    )
    priority: Mapped[int] = mapped_column(Integer, default=100)
    delay_seconds: Mapped[float] = mapped_column(Float, default=1.0)
    hourly_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    daily_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # A 线 / B 线的规则配置（JSON），默认空对象，读取时按模型补默认值
    a_config: Mapped[str] = mapped_column(Text, default="{}")
    b_config: Mapped[str] = mapped_column(Text, default="{}")

    targets: Mapped[list[RouteTarget]] = relationship(
        back_populates="route",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<Route {self.name} {self.business_type}>"


class RouteTarget(TimestampMixin, Base):
    """线路的接收目标。"""

    __tablename__ = "route_targets"
    __table_args__ = (UniqueConstraint("route_id", "target_chat_id", name="uq_route_target"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_id: Mapped[int] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"),
        index=True,
    )
    target_chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"),
        index=True,
    )
    target_role: Mapped[str] = mapped_column(String(16), default="content")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    route: Mapped[Route] = relationship(back_populates="targets")


class RouteTargetProgress(TimestampMixin, Base):
    """目标级水位线：保证新增目标只补自己缺的，老目标不重复搬运。"""

    __tablename__ = "route_target_progress"

    route_id: Mapped[int] = mapped_column(
        ForeignKey("routes.id", ondelete="CASCADE"),
        primary_key=True,
    )
    target_chat_id: Mapped[int] = mapped_column(
        ForeignKey("chats.id", ondelete="CASCADE"),
        primary_key=True,
    )
    last_delivered_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    backfill_status: Mapped[str] = mapped_column(String(16), default=BACKFILL_IDLE)
    backfill_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def touch(self) -> None:
        """更新水位线时间。"""
        self.last_run_at = utc_now()
