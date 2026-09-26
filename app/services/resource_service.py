"""资源库服务：按 tg_id 归一、状态机、人工锁定、筛选与导出。

两条贯穿全模块的规则在这里落地：

1. **按 tg_id 归一**：同一个群的 username / 邀请链接 / 数字 ID 只对应一条记录；
   私密群在"还没加入"时拿不到数字 ID，先以 invite_link 入库，加入后回填。
2. **人工值优先**：`manual_locked` 记下人工改过的字段，之后每次探测刷新都跳过它们
   ——运营改过的行业标签不会被下一次采集覆盖掉。
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.resource_probe import ProbeMetrics
from app.db.base import as_utc, utc_now
from app.db.models import (
    CONTENT_RATINGS,
    PROBE_FAILED,
    PROBE_OK,
    RATING_SENSITIVE,
    RATING_UNKNOWN,
    RESOURCE_ADOPTED,
    RESOURCE_CANDIDATE,
    RESOURCE_PROBED,
    RESOURCE_RETIRED,
    RESOURCE_STATUSES,
    ResourceProbeLog,
    TgResource,
)

# 人工可以锁定的字段（刷新时不覆盖）
MANUAL_FIELDS = ("language", "country", "categories", "title", "content_rating")

# 数据新鲜度分档（F-R01）
FRESH_SECONDS = 24 * 3600
WARM_SECONDS = 7 * 24 * 3600


@dataclass(frozen=True)
class ResourceRef:
    """一次发现拿到的资源标识。

    ``tg_id`` 可能为空（未加入的邀请链接），此时靠 ``invite_link`` 去重。
    """

    title: str = ""
    tg_id: int | None = None
    username: str | None = None
    invite_link: str | None = None
    chat_type: str = "group"
    about: str | None = None
    member_count: int | None = None
    member_count_approx: bool = False
    source_url: str | None = None
    # v1.2：三方目录站的条目带这些字段（一方搜索时为默认值）
    source_site: str | None = None
    language: str | None = None
    directory_rank: int | None = None
    directory_member_count: int | None = None
    content_rating: str | None = None


@dataclass(frozen=True)
class UpsertOutcome:
    """upsert 的结果。``resource=None`` 表示被黑名单挡住了。"""

    resource: TgResource | None
    created: bool
    skipped: str | None = None


# ------------------------------------------------------------------ JSON 小工具


def _load_list(raw: str | None) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def load_categories(resource: TgResource) -> list[str]:
    """行业标签。"""
    return _load_list(resource.categories)


def dump_categories(items: Iterable[str] | None) -> str:
    """序列化行业标签（去空、去重、保序）。"""
    result: list[str] = []
    for item in items or []:
        text = str(item).strip()
        if text and text not in result:
            result.append(text)
    return json.dumps(result, ensure_ascii=False)


def load_locked(resource: TgResource) -> list[str]:
    """人工锁定过的字段名。"""
    return _load_list(resource.manual_locked)


def dump_locked(items: Iterable[str] | None) -> str:
    """序列化锁定字段（只保留白名单里的字段名）。"""
    result: list[str] = []
    for item in items or []:
        text = str(item).strip()
        if text in MANUAL_FIELDS and text not in result:
            result.append(text)
    return json.dumps(result, ensure_ascii=False)


def load_title_history(resource: TgResource) -> list[dict[str, Any]]:
    """改名历史。"""
    try:
        value = json.loads(resource.title_history or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def load_samples(resource: TgResource) -> list[dict[str, Any]]:
    """最近一次探测留下的样本消息预览。"""
    try:
        value = json.loads(resource.sample_messages or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def dump_samples(items: Iterable[dict[str, Any]] | None, *, limit: int = 20) -> str:
    """序列化样本预览（最多存 limit 条）。"""
    return json.dumps(list(items or [])[:limit], ensure_ascii=False)


# ------------------------------------------------------------------ 查询


async def get_resource(session: AsyncSession, resource_id: int) -> TgResource | None:
    """按主键查询。"""
    return await session.get(TgResource, resource_id)


async def require_resource(session: AsyncSession, resource_id: int) -> TgResource:
    """按主键查询，不存在就报 404。"""
    resource = await session.get(TgResource, resource_id)
    if resource is None:
        raise NotFoundError("资源不存在")
    return resource


async def find_by_tg_id(session: AsyncSession, tg_id: int) -> TgResource | None:
    """按 Telegram ID 查询（去重主键）。"""
    return await session.scalar(select(TgResource).where(TgResource.tg_id == int(tg_id)))


async def find_by_key(
    session: AsyncSession,
    *,
    username: str | None = None,
    invite_link: str | None = None,
) -> TgResource | None:
    """按用户名 / 邀请链接查询（还没解析出 tg_id 的候选属于这类）。"""
    conditions = []
    if username:
        conditions.append(func.lower(TgResource.username) == username.strip().lstrip("@").lower())
    if invite_link:
        conditions.append(TgResource.invite_link == invite_link.strip())
    if not conditions:
        return None
    return await session.scalar(select(TgResource).where(or_(*conditions)).limit(1))


async def is_blacklisted(
    session: AsyncSession,
    *,
    tg_id: int | None = None,
    username: str | None = None,
    invite_link: str | None = None,
) -> bool:
    """这个标识是否已在黑名单里（自动发现据此直接跳过）。"""
    found = None
    if tg_id is not None:
        found = await find_by_tg_id(session, tg_id)
    if found is None:
        found = await find_by_key(session, username=username, invite_link=invite_link)
    return bool(found is not None and found.is_blacklisted)


# ------------------------------------------------------------------ 写入


async def upsert_resource(
    session: AsyncSession,
    ref: ResourceRef,
    *,
    discovered_by: str | None = None,
    discovered_from: str | None = None,
    first_seen_at: datetime | None = None,
    blacklist_check: bool = True,
) -> UpsertOutcome:
    """按 tg_id（或邀请链接）新增 / 更新一条资源。

    ``blacklist_check=True`` 时，命中黑名单的资源**不收录**（F-R15）。
    """
    if blacklist_check and await is_blacklisted(
        session,
        tg_id=ref.tg_id,
        username=ref.username,
        invite_link=ref.invite_link,
    ):
        return UpsertOutcome(None, False, skipped="blacklist")

    resource = None
    if ref.tg_id is not None:
        resource = await find_by_tg_id(session, ref.tg_id)
    if resource is None:
        resource = await find_by_key(
            session,
            username=ref.username,
            invite_link=ref.invite_link,
        )

    now = utc_now()
    if resource is None:
        resource = TgResource(
            tg_id=ref.tg_id,
            username=ref.username,
            invite_link=ref.invite_link,
            title=(ref.title or "").strip(),
            about=ref.about,
            chat_type=ref.chat_type,
            member_count=ref.member_count,
            member_count_approx=ref.member_count_approx,
            member_count_at=now if ref.member_count is not None else None,
            status=RESOURCE_CANDIDATE,
            discovered_by=discovered_by,
            discovered_from=(discovered_from or None),
            source_url=ref.source_url,
            source_site=ref.source_site,
            language=ref.language,
            directory_rank=ref.directory_rank,
            directory_member_count=ref.directory_member_count,
            directory_synced_at=now if ref.source_site else None,
            content_rating=ref.content_rating or RATING_UNKNOWN,
            first_seen_at=first_seen_at or now,
        )
        session.add(resource)
        await session.commit()
        await session.refresh(resource)
        return UpsertOutcome(resource, True)

    # 已存在：补齐标识、跟进标题改名，指标字段留给探测去更新
    changed = False
    if ref.tg_id is not None and resource.tg_id is None:
        # 私密群加入成功后回填数字 ID：如果已经有同 id 的记录，说明是重复发现
        clash = await find_by_tg_id(session, ref.tg_id)
        if clash is not None and clash.id != resource.id:
            return UpsertOutcome(clash, False, skipped="duplicate")
        resource.tg_id = ref.tg_id
        changed = True
    if ref.username and resource.username != ref.username:
        resource.username = ref.username
        changed = True
    if ref.invite_link and resource.invite_link != ref.invite_link:
        resource.invite_link = ref.invite_link
        changed = True
    if ref.title and ref.title.strip() and ref.title.strip() != resource.title:
        if resource.title and "title" not in load_locked(resource):
            _append_title(resource, resource.title, now)
        resource.title = ref.title.strip()
        changed = True
    if ref.about and not resource.about:
        resource.about = ref.about
        changed = True
    if ref.member_count is not None:
        resource.member_count = ref.member_count
        resource.member_count_approx = ref.member_count_approx
        resource.member_count_at = now
        changed = True
    if ref.chat_type and resource.chat_type != ref.chat_type:
        resource.chat_type = ref.chat_type
        changed = True
    if discovered_by and not resource.discovered_by:
        resource.discovered_by = discovered_by
        changed = True
    if discovered_from and not resource.discovered_from:
        resource.discovered_from = discovered_from
        changed = True
    if ref.source_url and not resource.source_url:
        resource.source_url = ref.source_url
        changed = True
    # v1.2 目录字段：source_site 只记第一次看到的站；名次与三方成员数每次都刷新
    if ref.source_site and not resource.source_site:
        resource.source_site = ref.source_site
        changed = True
    if ref.directory_member_count is not None:
        resource.directory_member_count = ref.directory_member_count
        changed = True
    if ref.directory_rank is not None:
        resource.directory_rank = ref.directory_rank
        changed = True
    if ref.source_site:
        resource.directory_synced_at = now
        changed = True
    # 目录给的语言只作提示：已经探测出语言（或人工锁定）就不覆盖
    if ref.language and not resource.language and "language" not in load_locked(resource):
        resource.language = ref.language
        changed = True
    # 分级只从 unknown 往上填：探测出的判定不会被目录或下一次发现的粗判覆盖
    if (
        ref.content_rating
        and resource.content_rating == RATING_UNKNOWN
        and "content_rating" not in load_locked(resource)
    ):
        resource.content_rating = ref.content_rating
        changed = True

    if changed:
        await session.commit()
        await session.refresh(resource)
    return UpsertOutcome(resource, False)


def _append_title(resource: TgResource, title: str, moment: datetime) -> None:
    """记录一次改名（保留最近 20 条够用了）。"""
    history = load_title_history(resource)
    history.append({"title": title, "at": moment.isoformat(timespec="seconds")})
    resource.title_history = json.dumps(history[-20:], ensure_ascii=False)


async def apply_metrics(
    session: AsyncSession,
    resource: TgResource,
    metrics: ProbeMetrics,
    *,
    probed_at: datetime | None = None,
    account_id: int | None = None,
    result: str = PROBE_OK,
    error: str | None = None,
    requests_used: int = 0,
    about: str | None = None,
    member_count: int | None = None,
    member_count_approx: bool = False,
    chat_type: str | None = None,
    username: str | None = None,
    next_refresh_at: datetime | None = None,
    sample_preview: Sequence[dict[str, Any]] | None = None,
) -> TgResource:
    """把一次探测的结果写进资源，并追加一条探测日志。

    人工锁定过的字段（``manual_locked``）跳过，不被机器值覆盖。
    """
    moment = probed_at or utc_now()
    locked = set(load_locked(resource))

    if chat_type:
        resource.chat_type = chat_type
    if username and not resource.username:
        resource.username = username
    if about:
        resource.about = about
    if member_count is not None:
        resource.member_count = member_count
        resource.member_count_approx = member_count_approx
        resource.member_count_at = moment
    if result == PROBE_OK:
        if "language" not in locked:
            resource.language = metrics.language
        if "country" not in locked:
            resource.country = metrics.country
        if "categories" not in locked:
            resource.categories = dump_categories(metrics.categories)
        resource.posts_per_day = metrics.posts_per_day
        resource.human_ratio = metrics.human_ratio
        resource.unique_senders = metrics.unique_senders
        resource.link_density = metrics.link_density
        resource.activity_score = metrics.activity_score
        resource.lead_potential = metrics.lead_potential
        resource.last_active_at = metrics.last_active_at
        resource.is_index_group = metrics.is_index_group
        resource.index_score = metrics.index_score
        # 内容分级（F-R22）：人工改过的分级不被探测覆盖
        if "content_rating" not in locked and metrics.content_rating:
            resource.content_rating = metrics.content_rating
        if sample_preview is not None:
            resource.sample_messages = dump_samples(sample_preview)
        if resource.status == RESOURCE_CANDIDATE:
            resource.status = RESOURCE_PROBED

    resource.last_probed_at = moment
    resource.probe_error = error if result == PROBE_FAILED else None
    if next_refresh_at is not None:
        resource.next_refresh_at = next_refresh_at

    session.add(
        ResourceProbeLog(
            resource_id=resource.id,
            probed_at=moment,
            member_count=member_count if member_count is not None else resource.member_count,
            activity_score=metrics.activity_score if result == PROBE_OK else None,
            posts_per_day=metrics.posts_per_day if result == PROBE_OK else None,
            human_ratio=metrics.human_ratio if result == PROBE_OK else None,
            link_density=metrics.link_density if result == PROBE_OK else None,
            lead_potential=metrics.lead_potential if result == PROBE_OK else None,
            last_active_at=metrics.last_active_at if result == PROBE_OK else None,
            result=result,
            error=error,
            account_id=account_id,
            requests_used=requests_used,
        )
    )
    await session.commit()
    await session.refresh(resource)
    return resource


async def update_manual_fields(
    session: AsyncSession,
    resource: TgResource,
    *,
    language: str | None = None,
    country: str | None = None,
    categories: Sequence[str] | None = None,
    note: str | None = None,
    content_rating: str | None = None,
) -> TgResource:
    """人工修正字段并锁定，之后的刷新不再覆盖它们。"""
    locked = set(load_locked(resource))
    if language is not None:
        resource.language = language.strip() or None
        locked.add("language")
    if country is not None:
        resource.country = country.strip() or None
        locked.add("country")
    if categories is not None:
        resource.categories = dump_categories(categories)
        locked.add("categories")
    if content_rating is not None:
        if content_rating not in CONTENT_RATINGS:
            raise ValidationFailedError(f"内容分级必须是 {'/'.join(CONTENT_RATINGS)} 之一")
        resource.content_rating = content_rating
        locked.add("content_rating")
    if note is not None:
        resource.note = note.strip() or None
    resource.manual_locked = dump_locked(locked)
    await session.commit()
    await session.refresh(resource)
    return resource


async def set_favorite(
    session: AsyncSession,
    resource: TgResource,
    favorite: bool,
) -> TgResource:
    """收藏 / 取消收藏。"""
    resource.is_favorite = favorite
    await session.commit()
    await session.refresh(resource)
    return resource


async def set_blacklisted(
    session: AsyncSession,
    resource: TgResource,
    blacklisted: bool,
    *,
    reason: str | None = None,
) -> TgResource:
    """拉黑 / 移出黑名单（黑名单资源不会被自动发现再次收录）。"""
    resource.is_blacklisted = blacklisted
    resource.blacklist_reason = (reason or "").strip() or None if blacklisted else None
    await session.commit()
    await session.refresh(resource)
    return resource


async def set_status(
    session: AsyncSession,
    resource: TgResource,
    status: str,
) -> TgResource:
    """直接改状态（界面上的"淘汰 / 恢复"）。"""
    if status not in RESOURCE_STATUSES:
        raise ValidationFailedError(f"状态必须是 {'/'.join(RESOURCE_STATUSES)} 之一")
    resource.status = status
    await session.commit()
    await session.refresh(resource)
    return resource


async def mark_adopted(
    session: AsyncSession,
    resource: TgResource,
    *,
    adopted_by: str | None = None,
    account_id: int | None = None,
    review_days: int = 7,
) -> TgResource:
    """标记为已采纳（F-R13），并排好第 7 天的产出回看。"""
    moment = utc_now()
    resource.status = RESOURCE_ADOPTED
    resource.adopted_at = moment
    resource.adopted_by = adopted_by
    if account_id is not None:
        resource.adopted_account_id = account_id
    resource.adopted_review_at = moment + timedelta(days=max(1, review_days))
    await session.commit()
    await session.refresh(resource)
    return resource


# ------------------------------------------------------------------ 刷新节奏


def refresh_days(resource: TgResource, config: AppConfig) -> int:
    """F-R10 的分层刷新：已采纳每天 / 候选每周 / 低分僵尸每月。"""
    section = config.resource
    if is_low_value(resource, section.activity_threshold):
        return section.refresh_low_days
    if resource.status == RESOURCE_ADOPTED:
        return section.refresh_adopted_days
    return section.refresh_candidate_days


def is_low_value(resource: TgResource, activity_threshold: float) -> bool:
    """低产出 / 僵尸：活跃度太低，或最后活跃时间已经过去 30 天。"""
    if resource.activity_score is not None and resource.activity_score < activity_threshold:
        return True
    last = as_utc(resource.last_active_at)
    if last is not None and (utc_now() - last).days >= 30:
        return True
    return False


def next_refresh_at(resource: TgResource, config: AppConfig, *, base: datetime | None = None):
    """算出下次建议刷新时间。"""
    moment = base or utc_now()
    return moment + timedelta(days=refresh_days(resource, config))


def data_freshness(resource: TgResource, *, now: datetime | None = None) -> str:
    """数据新鲜度：24h 内 fresh / 7 天内 warm / 更久 stale / 没测过 new。"""
    last = as_utc(resource.last_probed_at)
    if last is None:
        return "new"
    age = ((now or utc_now()) - last).total_seconds()
    if age <= FRESH_SECONDS:
        return "fresh"
    if age <= WARM_SECONDS:
        return "warm"
    return "stale"


# ------------------------------------------------------------------ 列表与导出

SORT_FIELDS = {
    "activity": (TgResource.activity_score.desc(), TgResource.id.asc()),
    "members": (TgResource.member_count.desc(), TgResource.id.asc()),
    "potential": (TgResource.lead_potential.desc(), TgResource.id.asc()),
    "recent_found": (TgResource.first_seen_at.desc(), TgResource.id.desc()),
    "recent_refresh": (TgResource.last_probed_at.desc(), TgResource.id.desc()),
}


def _filters(
    *,
    keyword: str | None = None,
    chat_type: str | None = None,
    languages: Sequence[str] | None = None,
    categories: Sequence[str] | None = None,
    member_min: int | None = None,
    member_max: int | None = None,
    min_activity: float | None = None,
    is_index_group: bool | None = None,
    freshness: str | None = None,
    active_within_days: int | None = None,
    adopted: bool | None = None,
    blacklisted: bool | None = None,
    favorite: bool | None = None,
    status: str | None = None,
    source_site: str | None = None,
    content_rating: str | None = None,
    include_sensitive: bool = True,
    due_refresh: bool = False,
) -> list[Any]:
    """把筛选条件翻成 SQL 条件列表（页面读库，不触发任何 Telegram 请求）。"""
    conditions: list[Any] = []
    if keyword:
        pattern = f"%{keyword}%"
        conditions.append(
            or_(
                TgResource.title.like(pattern),
                TgResource.username.like(pattern),
                TgResource.about.like(pattern),
            )
        )
    if chat_type:
        conditions.append(TgResource.chat_type == chat_type)
    if languages:
        conditions.append(TgResource.language.in_(list(languages)))
    if categories:
        # 行业是 JSON 数组，用 LIKE 逐个匹配（SQLite / PostgreSQL 都通用）
        for item in categories:
            conditions.append(TgResource.categories.like(f'%"{item}"%'))
    if member_min is not None:
        conditions.append(TgResource.member_count >= member_min)
    if member_max is not None:
        conditions.append(TgResource.member_count <= member_max)
    if min_activity is not None:
        conditions.append(TgResource.activity_score >= min_activity)
    if is_index_group is not None:
        conditions.append(TgResource.is_index_group.is_(is_index_group))
    if adopted is not None:
        if adopted:
            conditions.append(TgResource.status == RESOURCE_ADOPTED)
        else:
            conditions.append(TgResource.status != RESOURCE_ADOPTED)
    if blacklisted is not None:
        conditions.append(TgResource.is_blacklisted.is_(blacklisted))
    if favorite is not None:
        conditions.append(TgResource.is_favorite.is_(favorite))
    if status:
        conditions.append(TgResource.status == status)
    if source_site:
        conditions.append(TgResource.source_site == source_site)
    if content_rating:
        conditions.append(TgResource.content_rating == content_rating)
    # 卡片墙默认藏敏感内容（F-R22）：显式筛分级时以调用方的意图为准
    if not include_sensitive and not content_rating:
        conditions.append(TgResource.content_rating != RATING_SENSITIVE)
    if due_refresh:
        moment = utc_now()
        conditions.append(
            or_(
                TgResource.next_refresh_at.is_(None),
                TgResource.next_refresh_at <= moment,
            )
        )
    if active_within_days:
        conditions.append(
            TgResource.last_active_at >= utc_now() - timedelta(days=int(active_within_days))
        )
    if freshness:
        moment = utc_now()
        fresh_since = moment - timedelta(seconds=FRESH_SECONDS)
        warm_since = moment - timedelta(seconds=WARM_SECONDS)
        if freshness == "fresh":
            conditions.append(TgResource.last_probed_at >= fresh_since)
        elif freshness == "warm":
            conditions.append(TgResource.last_probed_at.between(warm_since, fresh_since))
        elif freshness == "stale":
            conditions.append(TgResource.last_probed_at < warm_since)
        elif freshness == "new":
            conditions.append(TgResource.last_probed_at.is_(None))
    return conditions


async def list_resources(
    session: AsyncSession,
    *,
    sort: str = "activity",
    limit: int = 100,
    offset: int = 0,
    **filters: Any,
) -> tuple[list[TgResource], int]:
    """分页查询资源库。"""
    conditions = _filters(**filters)
    statement = select(TgResource)
    count_statement = select(func.count()).select_from(TgResource)
    for condition in conditions:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)

    order = SORT_FIELDS.get(sort) or SORT_FIELDS["activity"]
    # NULL 排最后：没探测过的资源不该霸占"活跃度最高"的位置
    statement = statement.order_by(*order)
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)
    return rows, total


async def facet_options(session: AsyncSession) -> dict[str, list[str]]:
    """筛选项候选：语言与行业（从已有数据里汇总）。"""
    languages = [
        item for item in await session.scalars(select(TgResource.language).distinct()) if item
    ]
    categories: list[str] = []
    for raw in await session.scalars(select(TgResource.categories).distinct()):
        for item in _load_list(raw):
            if item not in categories:
                categories.append(item)
    return {"languages": sorted(languages), "categories": sorted(categories)}


def serialize_resource(
    resource: TgResource,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """资源对外结构。"""
    last_probed = as_utc(resource.last_probed_at)
    last_active = as_utc(resource.last_active_at)
    adopted_at = as_utc(resource.adopted_at)
    next_refresh = as_utc(resource.next_refresh_at)
    first_seen = as_utc(resource.first_seen_at)
    member_at = as_utc(resource.member_count_at)
    return {
        "id": resource.id,
        "tg_id": resource.tg_id,
        "username": resource.username,
        "invite_link": resource.invite_link,
        "title": resource.title,
        "name": resource.title or resource.username or f"#{resource.tg_id}",
        "about": resource.about,
        "chat_type": resource.chat_type,
        "member_count": resource.member_count,
        "member_count_approx": resource.member_count_approx,
        "member_count_at": member_at.isoformat() if member_at else None,
        "posts_per_day": resource.posts_per_day,
        "human_ratio": resource.human_ratio,
        "unique_senders": resource.unique_senders,
        "link_density": resource.link_density,
        "activity_score": resource.activity_score,
        "lead_potential": resource.lead_potential,
        "last_active_at": last_active.isoformat() if last_active else None,
        "language": resource.language,
        "country": resource.country,
        "categories": load_categories(resource),
        "is_index_group": resource.is_index_group,
        "index_score": resource.index_score,
        "manual_locked": load_locked(resource),
        "status": resource.status,
        "resource_state": resource.resource_state,
        "discovered_from": resource.discovered_from,
        "discovered_by": resource.discovered_by,
        "source_url": resource.source_url,
        "source_site": resource.source_site,
        "directory_rank": resource.directory_rank,
        "directory_member_count": resource.directory_member_count,
        "directory_synced_at": (
            as_utc(resource.directory_synced_at).isoformat()
            if resource.directory_synced_at
            else None
        ),
        "content_rating": resource.content_rating,
        "avatar_path": resource.avatar_path,
        "is_blacklisted": resource.is_blacklisted,
        "blacklist_reason": resource.blacklist_reason,
        "is_favorite": resource.is_favorite,
        "note": resource.note,
        "adopted_at": adopted_at.isoformat() if adopted_at else None,
        "adopted_by": resource.adopted_by,
        "adopted_account_id": resource.adopted_account_id,
        "adopted_review_at": (
            as_utc(resource.adopted_review_at).isoformat() if resource.adopted_review_at else None
        ),
        "adopted_lead_count": resource.adopted_lead_count,
        "last_probed_at": last_probed.isoformat() if last_probed else None,
        "probe_error": resource.probe_error,
        "next_refresh_at": next_refresh.isoformat() if next_refresh else None,
        "first_seen_at": first_seen.isoformat() if first_seen else None,
        "freshness": data_freshness(resource, now=now),
        "link": _display_link(resource),
    }


def _display_link(resource: TgResource) -> str | None:
    if resource.username:
        return f"https://t.me/{resource.username}"
    if resource.invite_link:
        return resource.invite_link
    return None


EXPORT_FIELDS = [
    ("id", "编号"),
    ("title", "名称"),
    ("tg_id", "TG ID"),
    ("username", "用户名"),
    ("chat_type", "类型"),
    ("member_count", "成员数"),
    ("member_count_approx", "成员数近似"),
    ("activity_score", "真人活跃度"),
    ("human_ratio", "真人占比"),
    ("unique_senders", "独立发言人数"),
    ("posts_per_day", "日均发帖"),
    ("link_density", "链接密度(每百条)"),
    ("lead_potential", "线索潜力"),
    ("language", "语言"),
    ("country", "国家"),
    ("categories", "行业"),
    ("is_index_group", "索引型"),
    ("status", "状态"),
    ("discovered_by", "发现方式"),
    ("discovered_from", "来源路径"),
    ("source_site", "来源站点"),
    ("directory_rank", "目录名次"),
    ("directory_member_count", "目录成员数"),
    ("content_rating", "内容分级"),
    ("link", "链接"),
    ("last_active_at", "最后活跃"),
    ("last_probed_at", "最后探测"),
    ("is_favorite", "收藏"),
    ("is_blacklisted", "黑名单"),
]


def resources_to_csv(rows: Iterable[TgResource]) -> str:
    """导出 CSV（带 BOM，Excel 直接打开不乱码）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([label for _field, label in EXPORT_FIELDS])
    for row in rows:
        item = serialize_resource(row)
        item["categories"] = "、".join(item["categories"])
        item["is_index_group"] = "是" if item["is_index_group"] else "否"
        item["is_favorite"] = "是" if item["is_favorite"] else "否"
        item["is_blacklisted"] = "是" if item["is_blacklisted"] else "否"
        item["member_count_approx"] = "是" if item["member_count_approx"] else "否"
        for key in ("last_active_at", "last_probed_at"):
            item[key] = (item[key] or "").replace("T", " ")[:19]
        writer.writerow([item.get(field, "") for field, _label in EXPORT_FIELDS])
    return "\ufeff" + buffer.getvalue()


async def probe_history(
    session: AsyncSession,
    resource_id: int,
    *,
    limit: int = 60,
) -> list[ResourceProbeLog]:
    """探测历史（趋势曲线的数据源，按时间正序）。"""
    rows = list(
        await session.scalars(
            select(ResourceProbeLog)
            .where(ResourceProbeLog.resource_id == resource_id)
            .order_by(ResourceProbeLog.probed_at.desc())
            .limit(limit)
        )
    )
    rows.reverse()
    return rows


def serialize_probe_log(row: ResourceProbeLog) -> dict[str, Any]:
    """一条探测日志。"""
    probed_at = as_utc(row.probed_at)
    return {
        "id": row.id,
        "probed_at": probed_at.isoformat() if probed_at else None,
        "member_count": row.member_count,
        "activity_score": row.activity_score,
        "posts_per_day": row.posts_per_day,
        "human_ratio": row.human_ratio,
        "link_density": row.link_density,
        "lead_potential": row.lead_potential,
        "result": row.result,
        "error": row.error,
        "requests_used": row.requests_used,
    }


# ---------------------------------------------------------------- 卡片墙计数

CHAT_TYPE_LABELS = {
    "channel": "频道",
    "supergroup": "超级群",
    "group": "群组",
}
SOURCE_SITE_LABELS = {
    "telegram": "Telegram 搜索",
    "combot": "Combot 目录",
    "tgme": "tg-me 列表",
    "manual": "人工添加",
}
RATING_LABELS = {
    "normal": "常规",
    "sensitive": "敏感",
    "unknown": "未判定",
}
# 目录站的语言代码是两字母，卡片墙的 chip 用中文更好读（认不出来的原样显示）
LANGUAGE_LABELS = {
    "zh": "中文",
    "en": "英文",
    "ru": "俄语",
    "ar": "阿拉伯语",
    "hi": "印地语",
    "fa": "波斯语",
    "tr": "土耳其语",
    "id": "印尼语",
    "es": "西班牙语",
    "vi": "越南语",
    "th": "泰语",
    "ja": "日语",
    "ko": "韩语",
}


async def _group_counts(
    session: AsyncSession,
    column: Any,
    *,
    limit: int | None = None,
) -> list[tuple[str, int]]:
    """按某一列分组计数，按数量倒序。"""
    statement = (
        select(column, func.count())
        .select_from(TgResource)
        .where(column.is_not(None))
        .group_by(column)
        .order_by(func.count().desc(), column.asc())
    )
    if limit is not None:
        statement = statement.limit(limit)
    rows = await session.execute(statement)
    return [(str(value), int(count)) for value, count in rows if value]


async def _count_where(session: AsyncSession, *conditions: Any) -> int:
    total = await session.scalar(select(func.count()).select_from(TgResource).where(*conditions))
    return int(total or 0)


def _labeled(items: list[tuple[str, int]], labels: dict[str, str] | None = None) -> list[dict]:
    return [
        {"value": value, "label": (labels or {}).get(value, value), "count": count}
        for value, count in items
    ]


async def counts(session: AsyncSession) -> dict[str, Any]:
    """卡片墙的分类 chips 与快捷榜计数（纯读库，F-R23）。"""
    categories: dict[str, int] = {}
    for raw in await session.scalars(select(TgResource.categories)):
        for item in _load_list(raw):
            categories[item] = categories.get(item, 0) + 1
    top_categories = sorted(categories.items(), key=lambda kv: (-kv[1], kv[0]))[:16]

    moment = utc_now()
    return {
        "languages": _labeled(
            await _group_counts(session, TgResource.language, limit=12),
            LANGUAGE_LABELS,
        ),
        "categories": _labeled(top_categories),
        "chat_types": _labeled(
            await _group_counts(session, TgResource.chat_type),
            CHAT_TYPE_LABELS,
        ),
        "sources": _labeled(
            await _group_counts(session, TgResource.source_site),
            SOURCE_SITE_LABELS,
        ),
        "ratings": _labeled(
            await _group_counts(session, TgResource.content_rating),
            RATING_LABELS,
        ),
        "quick": {
            "active": await _count_where(session, TgResource.activity_score.is_not(None)),
            "potential": await _count_where(session, TgResource.lead_potential.is_not(None)),
            "new": await _count_where(session, TgResource.status == RESOURCE_CANDIDATE),
            "due": await _count_where(
                session,
                TgResource.is_blacklisted.is_(False),
                or_(
                    TgResource.next_refresh_at.is_(None),
                    TgResource.next_refresh_at <= moment,
                ),
            ),
            "adopted": await _count_where(session, TgResource.status == RESOURCE_ADOPTED),
            "sensitive": await _count_where(
                session,
                TgResource.content_rating == RATING_SENSITIVE,
            ),
        },
    }


async def stats(session: AsyncSession) -> dict[str, Any]:
    """资源库概览（页面顶部用）。"""
    total = int(await session.scalar(select(func.count()).select_from(TgResource)) or 0)
    candidates = int(
        await session.scalar(
            select(func.count())
            .select_from(TgResource)
            .where(TgResource.status == RESOURCE_CANDIDATE)
        )
        or 0
    )
    probed = int(
        await session.scalar(
            select(func.count()).select_from(TgResource).where(TgResource.status == RESOURCE_PROBED)
        )
        or 0
    )
    adopted = int(
        await session.scalar(
            select(func.count())
            .select_from(TgResource)
            .where(TgResource.status == RESOURCE_ADOPTED)
        )
        or 0
    )
    retired = int(
        await session.scalar(
            select(func.count())
            .select_from(TgResource)
            .where(TgResource.status == RESOURCE_RETIRED)
        )
        or 0
    )
    favorites = int(
        await session.scalar(
            select(func.count()).select_from(TgResource).where(TgResource.is_favorite.is_(True))
        )
        or 0
    )
    blacklisted = int(
        await session.scalar(
            select(func.count()).select_from(TgResource).where(TgResource.is_blacklisted.is_(True))
        )
        or 0
    )
    index_groups = int(
        await session.scalar(
            select(func.count()).select_from(TgResource).where(TgResource.is_index_group.is_(True))
        )
        or 0
    )
    return {
        "total": total,
        "candidates": candidates,
        "probed": probed,
        "adopted": adopted,
        "retired": retired,
        "favorites": favorites,
        "blacklisted": blacklisted,
        "index_groups": index_groups,
    }


async def due_for_refresh(session: AsyncSession, *, limit: int = 20) -> list[TgResource]:
    """到点该刷新的资源（F-R10 / F-R16）。"""
    moment = utc_now()
    rows = await session.scalars(
        select(TgResource)
        .where(
            TgResource.is_blacklisted.is_(False),
            or_(
                TgResource.next_refresh_at.is_(None),
                TgResource.next_refresh_at <= moment,
            ),
            TgResource.last_probed_at.is_not(None),
        )
        .order_by(TgResource.next_refresh_at.asc())
        .limit(limit)
    )
    return list(rows)


async def next_probe_candidates(
    session: AsyncSession,
    *,
    limit: int = 5,
) -> list[TgResource]:
    """探测队列：没探过的候选 + 到点该刷新的资源，按排队时间先到先做。

    队列顺序就是 ``next_refresh_at`` 升序——新候选入库时排"现在"（立刻可做），
    探测完再排到 "现在 + 分层刷新间隔"。来自索引型群的链接入库时排队时间会
    提前一点（见 ``resource_probe_service.absorb_links``），所以优先被探测。
    """
    moment = utc_now()
    rows = await session.scalars(
        select(TgResource)
        .where(
            TgResource.is_blacklisted.is_(False),
            or_(
                TgResource.last_probed_at.is_(None),
                TgResource.next_refresh_at.is_(None),
                TgResource.next_refresh_at <= moment,
            ),
        )
        .order_by(TgResource.next_refresh_at.asc().nulls_first(), TgResource.id.asc())
        .limit(limit)
    )
    return list(rows)


def ensure_probeable(resource: TgResource) -> None:
    """探测前置校验：黑名单资源不探测。"""
    if resource.is_blacklisted:
        raise ConflictError("该资源在黑名单里，不会探测")


async def refresh_queue_size(session: AsyncSession) -> int:
    """刷新队列长度（配额看板用）。"""
    moment = utc_now()
    return int(
        await session.scalar(
            select(func.count())
            .select_from(TgResource)
            .where(
                TgResource.is_blacklisted.is_(False),
                TgResource.next_refresh_at.is_not(None),
                TgResource.next_refresh_at <= moment,
            )
        )
        or 0
    )
