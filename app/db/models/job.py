"""投递任务模型。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.db.models.tenant import TenantOwnedMixin

# 投递状态
JOB_PENDING = "pending"
JOB_PROCESSING = "processing"
JOB_SUCCESS = "success"
JOB_RETRYING = "retrying"
JOB_FAILED = "failed"
JOB_SKIPPED = "skipped"
JOB_STATUSES = (
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_SUCCESS,
    JOB_RETRYING,
    JOB_FAILED,
    JOB_SKIPPED,
)


class DeliveryJob(TenantOwnedMixin, TimestampMixin, Base):
    """一条「源消息 → 某个接收目标」的投递任务。"""

    __tablename__ = "delivery_jobs"
    __table_args__ = (
        # 幂等键：同一条源消息对同一个目标只投递一次
        UniqueConstraint(
            "source_chat_id",
            "source_message_id",
            "target_chat_id",
            name="uq_delivery_source_message_target",
        ),
        Index("ix_delivery_status_next_retry", "status", "next_retry_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    route_id: Mapped[int | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_chat_id: Mapped[int] = mapped_column(Integer, index=True)
    source_message_id: Mapped[int] = mapped_column(BigInteger, index=True)
    source_message_ids: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_group_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    target_chat_id: Mapped[int] = mapped_column(Integer, index=True)
    target_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=JOB_PENDING, index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    ad_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"<DeliveryJob {self.source_chat_id}:{self.source_message_id}"
            f" → {self.target_chat_id} {self.status}>"
        )
