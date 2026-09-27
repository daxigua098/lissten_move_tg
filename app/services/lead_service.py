"""线索与会员档案：入库、查询、导出与到期清理。"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lead_extractor import ContactInfo, SenderInfo
from app.core.outreach_capture import (
    CONSENT_MEMBER,
    CONSENT_NONE,
    CONSENT_PRIOR_REPLY,
    OUTREACH_BLOCKED_STATUSES,
    OUTREACH_REPLIED,
    OUTREACH_WAITING_SENDER_ACCOUNT,
    ROUTE_PEER_REFERENCE,
    ROUTE_PHONE,
    ROUTE_SHARED_GROUP,
    ROUTE_USERNAME,
    consent_satisfies_capture,
    detect_consent_type,
    detect_reachable_routes,
    parse_reachable_routes,
    reachable_routes_json,
)
from app.db.base import as_utc, utc_now
from app.db.models import ContactSuppression, Lead, MemberProfile


def _day_start(days_ago: int = 0) -> datetime:
    moment = datetime.now(UTC) - timedelta(days=days_ago)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


@dataclass(frozen=True)
class CaptureDecision:
    """一条监听消息能否成为冷私聊候选。"""

    allowed: bool
    reason: str
    reachable_routes: tuple[str, ...] = ()
    consent_type: str = CONSENT_NONE
    route_owner_account_id: int | None = None


async def evaluate_capture_eligibility(
    session: AsyncSession,
    *,
    tenant_id: int,
    sender: SenderInfo,
    text: str | None,
    capture_mode: str,
    source_account_id: int | None,
) -> CaptureDecision:
    """按“真人 + 可触达 + 未阻断 + 严格模式授权”判断是否入库。"""
    if not sender.is_user or sender.is_bot:
        return CaptureDecision(False, "NOT_REAL_USER")
    if not sender.tg_user_id:
        return CaptureDecision(False, "NO_TG_USER_ID")

    routes = detect_reachable_routes(
        username=sender.username,
        phone=sender.phone,
        has_peer_reference=sender.has_peer_reference,
        has_source_account=source_account_id is not None,
    )
    if not routes:
        return CaptureDecision(False, "NO_REACHABLE_ROUTE")
    # 冷私聊必须能明确找到人：发送者自己的用户名或手机号。
    # PEER_REFERENCE / SHARED_GROUP 只能证明监听账号见过对方，卡面也可能显示“未提供”，
    # 因此不能单独构成入库条件。
    if ROUTE_USERNAME not in routes and ROUTE_PHONE not in routes:
        return CaptureDecision(False, "NO_DIRECT_CONTACT")

    suppressed = await session.scalar(
        select(ContactSuppression.id).where(
            ContactSuppression.tenant_id == tenant_id,
            ContactSuppression.tg_user_id == sender.tg_user_id,
        )
    )
    if suppressed is not None:
        return CaptureDecision(False, "DO_NOT_CONTACT")

    profile = await session.scalar(
        select(MemberProfile).where(
            MemberProfile.tenant_id == tenant_id,
            MemberProfile.tg_user_id == sender.tg_user_id,
        )
    )
    if profile is not None:
        if profile.conversation_owner_account_id is not None:
            return CaptureDecision(False, "CONVERSATION_OWNER_CONFLICT")
        if (
            profile.first_contact_at is not None
            or profile.outreach_status in OUTREACH_BLOCKED_STATUSES
        ):
            return CaptureDecision(False, "ALREADY_CONTACTED")

    prior_reply = bool(
        profile is not None
        and (
            profile.outreach_status == OUTREACH_REPLIED
            or profile.consent_type == CONSENT_PRIOR_REPLY
        )
    )
    member_consent = bool(profile is not None and profile.consent_type == CONSENT_MEMBER)
    consent_type = detect_consent_type(
        text,
        prior_reply=prior_reply,
        member_consent=member_consent,
    )
    if not consent_satisfies_capture(capture_mode, consent_type):
        return CaptureDecision(False, "CONSENT_REQUIRED")

    route_owner_account_id = None
    if ROUTE_PEER_REFERENCE in routes or ROUTE_SHARED_GROUP in routes:
        route_owner_account_id = source_account_id
    reason = f"ROUTES={','.join(routes)};CONSENT={consent_type}"
    return CaptureDecision(
        True,
        reason,
        reachable_routes=routes,
        consent_type=consent_type,
        route_owner_account_id=route_owner_account_id,
    )


async def record_lead(
    session: AsyncSession,
    *,
    route_id: int,
    source_chat_id: int,
    message_id: int,
    message_at: datetime | None,
    sender: SenderInfo,
    contacts: ContactInfo,
    keyword: str | None,
    keyword_group_id: int | None,
    matched_mode: str,
    score: float,
    text: str,
    source_title: str,
    tenant_id: int | None = None,
    reachable_routes: tuple[str, ...] = (),
    consent_type: str = CONSENT_NONE,
    outreach_status: str = OUTREACH_WAITING_SENDER_ACCOUNT,
    capture_reason: str = "",
    route_owner_account_id: int | None = None,
) -> Lead:
    """写入一条线索（同一群同一条消息只记一次）。"""
    filters = [
        Lead.route_id == route_id,
        Lead.source_chat_id == source_chat_id,
        Lead.message_id == message_id,
    ]
    if tenant_id is not None:
        filters.append(Lead.tenant_id == tenant_id)
    existing = await session.scalar(select(Lead).where(*filters))
    if existing is not None:
        return existing

    lead = Lead(
        route_id=route_id,
        source_chat_id=source_chat_id,
        message_id=message_id,
        message_at=message_at,
        sender_tg_id=sender.tg_user_id,
        sender_username=sender.username,
        sender_name=sender.display_name,
        phone=sender.phone or contacts.primary_phone,
        wechat=contacts.primary_wechat,
        contacts=contacts.dump(),
        keyword=keyword,
        keyword_group_id=keyword_group_id,
        matched_mode=matched_mode,
        score=score,
        text=(text or "")[:4000],
        source_title=source_title,
        reachable_routes=reachable_routes_json(reachable_routes),
        consent_type=consent_type,
        outreach_status=outreach_status,
        capture_reason=capture_reason,
        route_owner_account_id=route_owner_account_id,
    )
    if tenant_id is not None:
        lead.tenant_id = tenant_id
    session.add(lead)
    await session.commit()
    await session.refresh(lead)
    return lead


async def mark_delivered(
    session: AsyncSession,
    lead: Lead,
    *,
    target_chat_id: int,
    target_message_id: int | None,
) -> None:
    lead.delivered = True
    lead.delivered_at = utc_now()
    lead.target_chat_id = target_chat_id
    lead.target_message_id = target_message_id
    await session.commit()


async def recent_lead_exists(
    session: AsyncSession,
    *,
    route_id: int,
    sender_tg_id: int | None,
    keyword: str | None,
    minutes: int,
) -> bool:
    """冷却窗口内同一个人 + 同一个关键词是否已经记过，避免刷屏。"""
    if not minutes or sender_tg_id is None:
        return False
    since = utc_now() - timedelta(minutes=minutes)
    statement = (
        select(func.count())
        .select_from(Lead)
        .where(
            Lead.route_id == route_id,
            Lead.sender_tg_id == sender_tg_id,
            Lead.created_at >= since,
        )
    )
    if keyword:
        statement = statement.where(Lead.keyword == keyword)
    return bool(await session.scalar(statement))


async def upsert_member(
    session: AsyncSession,
    sender: SenderInfo,
    *,
    seen_at: datetime | None = None,
    hit: bool = False,
    tenant_id: int | None = None,
    reachable_routes: tuple[str, ...] = (),
    consent_type: str = CONSENT_NONE,
    outreach_status: str = OUTREACH_WAITING_SENDER_ACCOUNT,
) -> MemberProfile | None:
    """按人汇总会员档案；没有用户 ID 的（频道匿名帖）跳过。

    ``hit=True`` 表示这条发言命中了关键词：该档案会被 pin 住，永久保留。
    """
    if not sender.tg_user_id:
        return None
    moment = seen_at or utc_now()
    filters = [MemberProfile.tg_user_id == sender.tg_user_id]
    if tenant_id is not None:
        filters.append(MemberProfile.tenant_id == tenant_id)
    profile = await session.scalar(select(MemberProfile).where(*filters))
    route_payload = reachable_routes_json(reachable_routes)
    if profile is None:
        profile = MemberProfile(
            tg_user_id=sender.tg_user_id,
            username=sender.username,
            display_name=sender.display_name,
            phone=sender.phone,
            is_bot=sender.is_bot,
            message_count=1,
            first_seen_at=moment,
            last_seen_at=moment,
            reachable_routes=route_payload,
            consent_type=consent_type,
            outreach_status=outreach_status,
        )
        if tenant_id is not None:
            profile.tenant_id = tenant_id
        session.add(profile)
    else:
        profile.username = sender.username or profile.username
        profile.display_name = sender.display_name or profile.display_name
        profile.phone = sender.phone or profile.phone
        profile.message_count += 1
        profile.last_seen_at = moment
        if reachable_routes:
            profile.reachable_routes = route_payload
        if consent_type != CONSENT_NONE:
            profile.consent_type = consent_type
        if profile.outreach_status not in OUTREACH_BLOCKED_STATUSES:
            profile.outreach_status = outreach_status
    if hit and not profile.pinned:
        profile.pinned = True
        profile.first_hit_at = moment
    await session.commit()
    return profile


def _filters(
    statement: Select[Any],
    *,
    source_chat_id: int | None,
    keyword: str | None,
    sender_tg_id: int | None,
    days: int | None,
    delivered: bool | None,
    only_hits: bool | None = None,
) -> Select[Any]:
    if source_chat_id is not None:
        statement = statement.where(Lead.source_chat_id == source_chat_id)
    if keyword:
        statement = statement.where(Lead.keyword.contains(keyword))
    if sender_tg_id is not None:
        statement = statement.where(Lead.sender_tg_id == sender_tg_id)
    if days:
        statement = statement.where(Lead.created_at >= _day_start(days))
    if delivered is not None:
        statement = statement.where(Lead.delivered.is_(delivered))
    if only_hits:
        statement = statement.where(Lead.keyword.is_not(None))
    return statement


async def list_leads(
    session: AsyncSession,
    *,
    source_chat_id: int | None = None,
    keyword: str | None = None,
    sender_tg_id: int | None = None,
    days: int | None = None,
    delivered: bool | None = None,
    only_hits: bool | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[Lead], int]:
    statement = _filters(
        select(Lead),
        source_chat_id=source_chat_id,
        keyword=keyword,
        sender_tg_id=sender_tg_id,
        days=days,
        delivered=delivered,
        only_hits=only_hits,
    )
    count_statement = _filters(
        select(func.count()).select_from(Lead),
        source_chat_id=source_chat_id,
        keyword=keyword,
        sender_tg_id=sender_tg_id,
        days=days,
        delivered=delivered,
        only_hits=only_hits,
    )
    total = int(await session.scalar(count_statement) or 0)
    rows = list(
        await session.scalars(statement.order_by(Lead.id.desc()).limit(limit).offset(offset))
    )
    return rows, total


async def lead_stats(session: AsyncSession) -> dict[str, Any]:
    """一次聚合线索指标，再聚合档案指标，避免接口下发多条 COUNT。"""
    today = _day_start()
    lead_row = (
        await session.execute(
            select(
                func.count(Lead.id),
                func.coalesce(
                    func.sum(case((Lead.created_at >= today, 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((Lead.delivered.is_(False), 1), else_=0)),
                    0,
                ),
                func.coalesce(
                    func.sum(case((Lead.keyword.is_not(None), 1), else_=0)),
                    0,
                ),
            )
        )
    ).one()
    member_row = (
        await session.execute(
            select(
                func.count(MemberProfile.id),
                func.coalesce(
                    func.sum(case((MemberProfile.pinned.is_(True), 1), else_=0)),
                    0,
                ),
            )
        )
    ).one()
    total, today_count, undelivered, hits = (int(value or 0) for value in lead_row)
    members, pinned_members = (int(value or 0) for value in member_row)
    return {
        "today": today_count,
        "total": total,
        "hits": hits,
        "undelivered": undelivered,
        "members": members,
        "pinned_members": pinned_members,
    }


EXPORT_FIELDS = [
    ("id", "编号"),
    ("created_at", "入库时间"),
    ("message_at", "发言时间"),
    ("source_title", "来源群"),
    ("sender_name", "昵称"),
    ("sender_username", "用户名"),
    ("sender_tg_id", "用户ID"),
    ("phone", "手机号"),
    ("wechat", "微信号"),
    ("keyword", "命中关键词"),
    ("matched_mode", "命中方式"),
    ("score", "相似度"),
    ("text", "发言原文"),
    ("delivered", "是否已推送"),
]


def lead_rows_for_export(rows: list[Lead]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in rows:
        created = as_utc(row.created_at)
        message_at = as_utc(row.message_at)
        items.append(
            {
                "id": row.id,
                "created_at": created.strftime("%Y-%m-%d %H:%M:%S") if created else "",
                "message_at": message_at.strftime("%Y-%m-%d %H:%M:%S") if message_at else "",
                "source_title": row.source_title,
                "sender_name": row.sender_name or "",
                "sender_username": f"@{row.sender_username}" if row.sender_username else "",
                "sender_tg_id": row.sender_tg_id or "",
                "phone": row.phone or "",
                "wechat": row.wechat or "",
                "keyword": row.keyword or "",
                "matched_mode": row.matched_mode,
                "score": row.score,
                "text": row.text.replace("\n", " "),
                "delivered": "是" if row.delivered else "否",
            }
        )
    return items


def leads_to_csv(rows: list[Lead]) -> str:
    """导出 CSV（带 BOM，Excel 直接打开不乱码）。"""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([label for _field, label in EXPORT_FIELDS])
    keys = [field for field, _label in EXPORT_FIELDS]
    for item in lead_rows_for_export(rows):
        writer.writerow([item.get(key, "") for key in keys])
    return "\ufeff" + buffer.getvalue()


async def purge_expired(
    session: AsyncSession,
    *,
    leads_days: int,
    profiles_days: int,
    archive_dir: Path,
) -> dict[str, int]:
    """清理到期数据，分两档：

    - **命中关键词的线索与对应用户永久保留**，不参与清理；
    - 未命中的线索（全量入库）与档案按天数清理，线索删除前先归档成 JSONL。
    """
    lead_cutoff = utc_now() - timedelta(days=leads_days)
    expired = list(
        await session.scalars(
            select(Lead).where(
                Lead.created_at < lead_cutoff,
                Lead.keyword.is_(None),
            )
        )
    )
    archived = 0
    if expired:
        # 写文件是阻塞操作，丢到线程里，别卡住事件循环
        archived = await asyncio.to_thread(_archive_leads, expired, archive_dir)
        for row in expired:
            await session.delete(row)

    profile_cutoff = utc_now() - timedelta(days=profiles_days)
    profiles = list(
        await session.scalars(
            select(MemberProfile).where(
                MemberProfile.last_seen_at < profile_cutoff,
                MemberProfile.pinned.is_(False),
            )
        )
    )
    for row in profiles:
        await session.delete(row)

    if expired or profiles:
        await session.commit()
    return {"leads": len(expired), "profiles": len(profiles), "archived": archived}


async def purge_delivered(session: AsyncSession, *, archive_dir: Path) -> dict[str, int]:
    """清空「已完成」的线索：只删已推送的，未推送的一条不动。

    删除前仍然先归档成 JSONL（跟保留策略清理同一套），删错了还能从归档里找回来。
    """
    rows = list(await session.scalars(select(Lead).where(Lead.delivered.is_(True))))
    if not rows:
        return {"deleted": 0, "archived": 0}
    # 写文件是阻塞操作，丢到线程里，别卡住事件循环
    archived = await asyncio.to_thread(_archive_leads, rows, archive_dir)
    for row in rows:
        await session.delete(row)
    await session.commit()
    return {"deleted": len(rows), "archived": archived}


async def purge_all(session: AsyncSession, *, archive_dir: Path) -> dict[str, int]:
    """清空线索池：删除全部线索与会员档案，删除前先归档线索。"""
    leads = list(await session.scalars(select(Lead)))
    profiles = list(await session.scalars(select(MemberProfile)))
    archived = 0
    if leads:
        archived = await asyncio.to_thread(_archive_leads, leads, archive_dir)
        for row in leads:
            await session.delete(row)
    for row in profiles:
        await session.delete(row)
    if leads or profiles:
        await session.commit()
    return {"deleted": len(leads), "profiles": len(profiles), "archived": archived}


def _archive_leads(rows: list[Lead], archive_dir: Path) -> int:
    """把即将删除的线索追加写进当天的 JSONL 归档文件。"""
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / f"leads-{utc_now():%Y%m%d}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        for row in rows:
            payload = {
                **lead_rows_for_export([row])[0],
                "route_id": row.route_id,
                "source_chat_id": row.source_chat_id,
                "message_id": row.message_id,
                "archived_at": utc_now().isoformat(timespec="seconds"),
            }
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return len(rows)


def serialize_lead(row: Lead) -> dict[str, Any]:
    created = as_utc(row.created_at)
    message_at = as_utc(row.message_at)
    return {
        "id": row.id,
        "route_id": row.route_id,
        "source_chat_id": row.source_chat_id,
        "source_title": row.source_title,
        "message_id": row.message_id,
        "message_at": message_at.isoformat() if message_at else None,
        "created_at": created.isoformat() if created else None,
        "sender_tg_id": row.sender_tg_id,
        "sender_username": row.sender_username,
        "sender_name": row.sender_name,
        "phone": row.phone,
        "wechat": row.wechat,
        "contacts": row.contacts,
        "keyword": row.keyword,
        "matched_mode": row.matched_mode,
        "score": row.score,
        "text": row.text,
        "reachable_routes": parse_reachable_routes(row.reachable_routes),
        "consent_type": row.consent_type,
        "outreach_status": row.outreach_status,
        "capture_reason": row.capture_reason,
        "route_owner_account_id": row.route_owner_account_id,
        "delivered": row.delivered,
        "target_chat_id": row.target_chat_id,
        "target_message_id": row.target_message_id,
    }


def serialize_member(row: MemberProfile) -> dict[str, Any]:
    first_seen = as_utc(row.first_seen_at)
    last_seen = as_utc(row.last_seen_at)
    first_hit = as_utc(row.first_hit_at)
    return {
        "id": row.id,
        "tg_user_id": row.tg_user_id,
        "username": row.username,
        "display_name": row.display_name,
        "phone": row.phone,
        "is_bot": row.is_bot,
        "message_count": row.message_count,
        "pinned": row.pinned,
        "reachable_routes": parse_reachable_routes(row.reachable_routes),
        "consent_type": row.consent_type,
        "outreach_status": row.outreach_status,
        "conversation_owner_account_id": row.conversation_owner_account_id,
        "first_contact_at": row.first_contact_at.isoformat() if row.first_contact_at else None,
        "last_contact_at": row.last_contact_at.isoformat() if row.last_contact_at else None,
        "first_hit_at": first_hit.isoformat() if first_hit else None,
        "first_seen_at": first_seen.isoformat() if first_seen else None,
        "last_seen_at": last_seen.isoformat() if last_seen else None,
    }
