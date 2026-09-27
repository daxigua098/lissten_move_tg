"""冷触达发送记录：统一读取成功消息与失败/待核实任务。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import exists, false, func, literal, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import as_utc
from app.db.models import (
    DIRECTION_OUT,
    MESSAGE_KIND_AUTO_REPLY,
    MESSAGE_KIND_FIRST_CONTACT,
    MESSAGE_KIND_FOLLOW_UP,
    MESSAGE_KIND_HANDOFF,
    MESSAGE_KIND_LEGACY,
    MESSAGE_KIND_REPLY,
    TASK_BLOCKED,
    TASK_FAILED,
    TASK_SENT,
    TASK_UNKNOWN_DELIVERY,
    Lead,
    OutreachContact,
    OutreachMessage,
    OutreachTask,
    TgAccount,
)
from app.services import upload_service

RECORD_STATUSES = (TASK_SENT, TASK_FAILED, TASK_BLOCKED, TASK_UNKNOWN_DELIVERY)

MESSAGE_KIND_LABELS: dict[str, str] = {
    MESSAGE_KIND_FIRST_CONTACT: "首触消息",
    MESSAGE_KIND_FOLLOW_UP: "跟进消息",
    MESSAGE_KIND_AUTO_REPLY: "自动回复",
    MESSAGE_KIND_HANDOFF: "Bot 转交",
    MESSAGE_KIND_REPLY: "对方回复",
    MESSAGE_KIND_LEGACY: "历史记录",
}

TRIGGER_LABELS: dict[str, str] = {
    "manual": "人工立即发送",
    "scheduler": "后台自动调度",
    "legacy": "历史任务",
}


def _filters(
    statement,
    *,
    tenant_id: int,
    account_id: int | None,
    contact_id: int | None,
    status: str | None,
    message_kind: str | None,
    trigger_type: str | None,
    keyword: str | None,
    start: datetime | None,
    end: datetime | None,
):
    statement = statement.where(OutreachMessage.tenant_id == tenant_id)
    if account_id is not None:
        statement = statement.where(OutreachMessage.account_id == account_id)
    if contact_id is not None:
        statement = statement.where(OutreachMessage.contact_id == contact_id)
    if status and status != TASK_SENT:
        statement = statement.where(false())
    if message_kind:
        statement = statement.where(OutreachMessage.message_kind == message_kind)
    if trigger_type:
        statement = statement.where(OutreachTask.trigger_type == trigger_type)
    if keyword:
        statement = statement.where(OutreachMessage.text.contains(keyword.strip()))
    if start is not None:
        statement = statement.where(OutreachMessage.sent_at >= start)
    if end is not None:
        statement = statement.where(OutreachMessage.sent_at <= end)
    return statement


def _task_filters(
    statement,
    *,
    tenant_id: int,
    account_id: int | None,
    contact_id: int | None,
    status: str | None,
    message_kind: str | None,
    trigger_type: str | None,
    keyword: str | None,
    start: datetime | None,
    end: datetime | None,
):
    statement = statement.where(OutreachTask.tenant_id == tenant_id)
    if account_id is not None:
        statement = statement.where(OutreachTask.account_id == account_id)
    if contact_id is not None:
        statement = statement.where(OutreachTask.contact_id == contact_id)
    if status:
        statement = statement.where(OutreachTask.status == status)
    if message_kind:
        statement = statement.where(OutreachTask.kind == message_kind)
    if trigger_type:
        statement = statement.where(OutreachTask.trigger_type == trigger_type)
    if keyword:
        statement = statement.where(OutreachTask.rendered_text.contains(keyword.strip()))
    moment = func.coalesce(OutreachTask.sent_at, OutreachTask.updated_at, OutreachTask.created_at)
    if start is not None:
        statement = statement.where(moment >= start)
    if end is not None:
        statement = statement.where(moment <= end)
    return statement


async def list_records(
    session: AsyncSession,
    *,
    tenant_id: int,
    account_id: int | None = None,
    contact_id: int | None = None,
    status: str | None = None,
    message_kind: str | None = None,
    trigger_type: str | None = None,
    keyword: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    """发送记录支持成功消息、失败任务和待核实任务的统一分页。"""
    message_stmt = (
        select(
            literal("message").label("record_type"),
            OutreachMessage.id.label("record_id"),
            OutreachMessage.id.label("message_id"),
            OutreachTask.id.label("task_id"),
            OutreachMessage.contact_id.label("contact_id"),
            OutreachMessage.account_id.label("account_id"),
            OutreachMessage.message_kind.label("message_kind"),
            literal(TASK_SENT).label("status"),
            OutreachMessage.text.label("content"),
            OutreachMessage.media_path.label("media_path"),
            OutreachMessage.media_kind.label("media_kind"),
            OutreachMessage.tg_message_id.label("tg_message_id"),
            OutreachMessage.sent_at.label("sort_at"),
            OutreachTask.trigger_type.label("trigger_type"),
            OutreachTask.triggered_by.label("triggered_by"),
            OutreachTask.last_error.label("last_error"),
            OutreachContact.username.label("contact_username"),
            OutreachContact.display_name.label("contact_display_name"),
            OutreachContact.tg_user_id.label("contact_tg_user_id"),
            OutreachContact.phone.label("contact_phone"),
            OutreachMessage.recipient_username.label("recipient_username"),
            OutreachMessage.recipient_display_name.label("recipient_display_name"),
            OutreachMessage.recipient_tg_user_id.label("recipient_tg_user_id"),
            TgAccount.name.label("account_name"),
            Lead.source_title.label("source_title"),
            Lead.keyword.label("keyword"),
        )
        .select_from(OutreachMessage)
        .outerjoin(OutreachTask, OutreachTask.id == OutreachMessage.task_id)
        .outerjoin(OutreachContact, OutreachContact.id == OutreachMessage.contact_id)
        .outerjoin(TgAccount, TgAccount.id == OutreachMessage.account_id)
        .outerjoin(Lead, Lead.id == OutreachTask.lead_id)
        .where(OutreachMessage.direction == DIRECTION_OUT)
    )
    message_stmt = _filters(
        message_stmt,
        tenant_id=tenant_id,
        account_id=account_id,
        contact_id=contact_id,
        status=status,
        message_kind=message_kind,
        trigger_type=trigger_type,
        keyword=keyword,
        start=start,
        end=end,
    )

    linked_message = exists(
        select(OutreachMessage.id).where(OutreachMessage.task_id == OutreachTask.id)
    )
    task_sort = func.coalesce(
        OutreachTask.sent_at,
        OutreachTask.updated_at,
        OutreachTask.created_at,
    )
    task_stmt = (
        select(
            literal("task").label("record_type"),
            OutreachTask.id.label("record_id"),
            literal(None).label("message_id"),
            OutreachTask.id.label("task_id"),
            OutreachTask.contact_id.label("contact_id"),
            OutreachTask.account_id.label("account_id"),
            OutreachTask.kind.label("message_kind"),
            OutreachTask.status.label("status"),
            OutreachTask.rendered_text.label("content"),
            literal(None).label("media_path"),
            literal(None).label("media_kind"),
            literal(0).label("tg_message_id"),
            task_sort.label("sort_at"),
            OutreachTask.trigger_type.label("trigger_type"),
            OutreachTask.triggered_by.label("triggered_by"),
            OutreachTask.last_error.label("last_error"),
            OutreachContact.username.label("contact_username"),
            OutreachContact.display_name.label("contact_display_name"),
            OutreachContact.tg_user_id.label("contact_tg_user_id"),
            OutreachContact.phone.label("contact_phone"),
            literal(None).label("recipient_username"),
            literal(None).label("recipient_display_name"),
            literal(None).label("recipient_tg_user_id"),
            TgAccount.name.label("account_name"),
            Lead.source_title.label("source_title"),
            Lead.keyword.label("keyword"),
        )
        .select_from(OutreachTask)
        .outerjoin(OutreachContact, OutreachContact.id == OutreachTask.contact_id)
        .outerjoin(TgAccount, TgAccount.id == OutreachTask.account_id)
        .outerjoin(Lead, Lead.id == OutreachTask.lead_id)
        .where(OutreachTask.status.in_(RECORD_STATUSES), ~linked_message)
    )
    task_stmt = _task_filters(
        task_stmt,
        tenant_id=tenant_id,
        account_id=account_id,
        contact_id=contact_id,
        status=status,
        message_kind=message_kind,
        trigger_type=trigger_type,
        keyword=keyword,
        start=start,
        end=end,
    )

    combined = union_all(message_stmt, task_stmt).subquery()
    total = int(await session.scalar(select(func.count()).select_from(combined)) or 0)
    rows = (
        await session.execute(
            select(combined)
            .order_by(combined.c.sort_at.desc(), combined.c.record_id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).mappings()
    items = []
    for row in rows:
        item = dict(row)
        item["sort_at"] = as_utc(item.get("sort_at"))
        item["message_kind_label"] = MESSAGE_KIND_LABELS.get(
            item.get("message_kind"),
            item.get("message_kind") or "历史记录",
        )
        item["trigger_label"] = TRIGGER_LABELS.get(
            item.get("trigger_type"),
            item.get("trigger_type") or "",
        )
        item["media_url"] = upload_service.public_url(item.get("media_path"))
        items.append(item)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


async def list_contact_messages(
    session: AsyncSession,
    *,
    tenant_id: int,
    contact_id: int,
    limit: int = 200,
) -> dict[str, Any]:
    """联系人详情中的完整出入站消息时间线。"""
    rows = (
        await session.execute(
            select(
                OutreachMessage,
                TgAccount.name.label("account_name"),
            )
            .outerjoin(TgAccount, TgAccount.id == OutreachMessage.account_id)
            .where(
                OutreachMessage.tenant_id == tenant_id,
                OutreachMessage.contact_id == contact_id,
            )
            .order_by(OutreachMessage.sent_at.desc(), OutreachMessage.id.desc())
            .limit(limit)
        )
    ).all()
    items = []
    for message, account_name in rows:
        items.append(
            {
                "id": message.id,
                "task_id": message.task_id,
                "direction": message.direction,
                "message_kind": message.message_kind,
                "message_kind_label": MESSAGE_KIND_LABELS.get(
                    message.message_kind,
                    message.message_kind,
                ),
                "text": message.text,
                "account_id": message.account_id,
                "account_name": account_name,
                "tg_message_id": message.tg_message_id,
                "media_path": message.media_path,
                "media_kind": message.media_kind,
                "media_url": upload_service.public_url(message.media_path),
                "sent_at": as_utc(message.sent_at),
            }
        )
    return {"items": items, "total": len(items)}
