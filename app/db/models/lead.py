"""B 线（会员监听）数据模型：关键词组、会员档案与线索。

保留策略（与 config.retention 对齐）：
- 线索 leads：默认 3 天，删除前先归档；
- 会员档案 member_profiles：默认 3 天；
- 关键词组 keyword_groups / keywords：长期保留。
"""

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

from app.db.base import Base, TimestampMixin

# 命中方式
MATCH_CONTAINS = "contains"
MATCH_FUZZY = "fuzzy"
MATCH_MODES = (MATCH_CONTAINS, MATCH_FUZZY)

# 词组用途：关键词（用来判断是不是线索）/ 排除词（命中就整条忽略）
GROUP_KIND_KEYWORD = "keyword"
GROUP_KIND_EXCLUDE = "exclude"
GROUP_KINDS = (GROUP_KIND_KEYWORD, GROUP_KIND_EXCLUDE)


class KeywordGroup(TimestampMixin, Base):
    """词组：关键词组用来判断命中，排除词组用来挡掉噪声。

    同一种表结构：``kind`` 区分用途，一条 B 线可以多选引用多组，取并集。
    """

    __tablename__ = "keyword_groups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    description: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    kind: Mapped[str] = mapped_column(
        String(16),
        default=GROUP_KIND_KEYWORD,
        index=True,
    )

    keywords: Mapped[list[Keyword]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        order_by="Keyword.id",
    )

    @property
    def keyword_count(self) -> int:
        return len(self.keywords)


class Keyword(TimestampMixin, Base):
    """关键词：主词 + 别名。

    「体育 → 篮球 / 乒乓球」这类语义相近，靠别名实现：主词是体育，
    别名写篮球、足球、乒乓球，命中任意一个都算这条线索。
    """

    __tablename__ = "keywords"
    __table_args__ = (UniqueConstraint("group_id", "word"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("keyword_groups.id", ondelete="CASCADE"),
        index=True,
    )
    word: Mapped[str] = mapped_column(String(64))
    # 别名：逗号 / 顿号 / 换行分隔
    aliases: Mapped[str] = mapped_column(Text, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    group: Mapped[KeywordGroup] = relationship(back_populates="keywords")


class MemberProfile(TimestampMixin, Base):
    """会员档案：同一个人在群里发言的汇总信息。"""

    __tablename__ = "member_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    # 命中过关键词的用户：档案永久保留，不参与到期清理
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    first_hit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Lead(TimestampMixin, Base):
    """线索：一条被监听到、值得跟进的发言。"""

    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # 线索是业务数据，不能因为删线路/删群就消失（只解除引用，保留快照字段）
    route_id: Mapped[int | None] = mapped_column(
        ForeignKey("routes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    source_chat_id: Mapped[int | None] = mapped_column(
        ForeignKey("chats.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    message_id: Mapped[int] = mapped_column(BigInteger)
    message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sender_tg_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    sender_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    wechat: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contacts: Mapped[str] = mapped_column(Text, default="")

    keyword: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    keyword_group_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    matched_mode: Mapped[str] = mapped_column(String(16), default="")
    score: Mapped[float] = mapped_column(Float, default=0.0)

    text: Mapped[str] = mapped_column(Text, default="")
    source_title: Mapped[str] = mapped_column(String(128), default="")

    delivered: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    target_chat_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class HotKeyword(TimestampMixin, Base):
    """热门关键词：从监听到的会员发言里采词，按出现次数排名。

    用途是"发现用户在搜什么"，帮我们决定往词库里加什么词。
    **永不删除**：不参与任何保留策略清理，只累加。
    """

    __tablename__ = "hot_keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    count: Mapped[int] = mapped_column(Integer, default=0, index=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    sources: Mapped[str] = mapped_column(Text, default="")
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
