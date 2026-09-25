"""线索与会员档案：入库、查询、导出与到期清理。"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.lead_extractor import ContactInfo, SenderInfo
from app.db.base import as_utc, utc_now
from app.db.models import Lead, MemberProfile


def _day_start(days_ago: int = 0) -> datetime:
    moment = datetime.now(UTC) - timedelta(days=days_ago)
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


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
) -> Lead:
    """写入一条线索（同一群同一条消息只记一次）。"""
    existing = await session.scalar(
        select(Lead).where(
            Lead.route_id == route_id,
            Lead.source_chat_id == source_chat_id,
            Lead.message_id == message_id,
        )
    )
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
    )
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
) -> MemberProfile | None:
    """按人汇总会员档案；没有用户 ID 的（频道匿名帖）跳过。"""
    if not sender.tg_user_id:
        return None
    moment = seen_at or utc_now()
    profile = await session.scalar(
        select(MemberProfile).where(MemberProfile.tg_user_id == sender.tg_user_id)
    )
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
        )
        session.add(profile)
    else:
        profile.username = sender.username or profile.username
        profile.display_name = sender.display_name or profile.display_name
        profile.phone = sender.phone or profile.phone
        profile.message_count += 1
        profile.last_seen_at = moment
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
    return statement


async def list_leads(
    session: AsyncSession,
    *,
    source_chat_id: int | None = None,
    keyword: str | None = None,
    sender_tg_id: int | None = None,
    days: int | None = None,
    delivered: bool | None = None,
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
    )
    count_statement = _filters(
        select(func.count()).select_from(Lead),
        source_chat_id=source_chat_id,
        keyword=keyword,
        sender_tg_id=sender_tg_id,
        days=days,
        delivered=delivered,
    )
    total = int(await session.scalar(count_statement) or 0)
    rows = list(
        await session.scalars(statement.order_by(Lead.id.desc()).limit(limit).offset(offset))
    )
    return rows, total


async def lead_stats(session: AsyncSession) -> dict[str, Any]:
    today = int(
        await session.scalar(
            select(func.count()).select_from(Lead).where(Lead.created_at >= _day_start())
        )
        or 0
    )
    total = int(await session.scalar(select(func.count()).select_from(Lead)) or 0)
    undelivered = int(
        await session.scalar(
            select(func.count()).select_from(Lead).where(Lead.delivered.is_(False))
        )
        or 0
    )
    members = int(await session.scalar(select(func.count()).select_from(MemberProfile)) or 0)
    return {
        "today": today,
        "total": total,
        "undelivered": undelivered,
        "members": members,
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
    """清理到期线索与档案；线索删除前先归档成 JSONL。"""
    lead_cutoff = utc_now() - timedelta(days=leads_days)
    expired = list(await session.scalars(select(Lead).where(Lead.created_at < lead_cutoff)))
    archived = 0
    if expired:
        # 写文件是阻塞操作，丢到线程里，别卡住事件循环
        archived = await asyncio.to_thread(_archive_leads, expired, archive_dir)
        for row in expired:
            await session.delete(row)

    profile_cutoff = utc_now() - timedelta(days=profiles_days)
    profiles = list(
        await session.scalars(
            select(MemberProfile).where(MemberProfile.last_seen_at < profile_cutoff)
        )
    )
    for row in profiles:
        await session.delete(row)

    if expired or profiles:
        await session.commit()
    return {"leads": len(expired), "profiles": len(profiles), "archived": archived}


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
        "delivered": row.delivered,
        "target_chat_id": row.target_chat_id,
        "target_message_id": row.target_message_id,
    }
