"""到期提醒记录（P4-05）：按"租户 + 阶段 + 受众"去重，一个阶段只提醒一次。

受众分两类：

- ``agent``：开设该租户的代理，阶段 7 / 3 / 1 天（提醒他预留额度）；
- ``member``：会员本人，阶段 3 / 1 天。

提醒渠道目前只实现 ``inapp``（站内：登录后的横幅 + 后台到期列表的"已提醒"标记）。
以后要接 TG / 邮件 / 短信，只需在 ``reminder_service`` 里补一个发送实现，
去重表不用动——所以渠道字段现在就落库。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, utc_now

AUDIENCE_AGENT = "agent"
AUDIENCE_MEMBER = "member"
AUDIENCES = (AUDIENCE_AGENT, AUDIENCE_MEMBER)

# 渠道：站内。预留 tg / email，接通道时只要加常量与发送实现
CHANNEL_INAPP = "inapp"
CHANNEL_TELEGRAM = "telegram"
CHANNELS = (CHANNEL_INAPP, CHANNEL_TELEGRAM)


class TenantReminder(TimestampMixin, Base):
    """一条到期提醒：谁（受众）在哪个阶段被提醒过。"""

    __tablename__ = "tenant_reminders"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "stage",
            "audience",
            name="uq_tenant_reminder_stage",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    # '7d' / '3d' / '1d'
    stage: Mapped[str] = mapped_column(String(8), nullable=False)
    audience: Mapped[str] = mapped_column(String(16), default=AUDIENCE_AGENT, nullable=False)
    channel: Mapped[str] = mapped_column(String(16), default=CHANNEL_INAPP, nullable=False)
    # 生成提醒时算出的剩余天数与到期时刻（追溯用，续期后不会被改写）
    days_left: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<TenantReminder t={self.tenant_id} {self.stage} {self.audience}>"
