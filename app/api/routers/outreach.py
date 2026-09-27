"""冷触达（P1）：租户策略、话术模板、联系锁与任务队列。

本阶段只做「过闸入队 + 容量展示」，不发送任何消息。
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    current_identity,
    require_member_or_platform,
    session_dependency,
    tenant_scope_of,
)
from app.api.schemas.outreach import (
    OutreachSettingsUpdate,
    OutreachTemplateCreate,
    OutreachTemplateUpdate,
)
from app.db.base import as_utc
from app.db.models import (
    CONTACT_STATE_LABELS,
    ROLE_SUB_ADMIN,
    TASK_STATUS_LABELS,
    TEMPLATE_KIND_LABELS,
    OutreachContact,
    OutreachTask,
    OutreachTemplate,
)
from app.services import (
    outreach_queue_service,
    outreach_settings_service,
    outreach_template_service,
)

router = APIRouter(
    prefix="/api/outreach",
    tags=["outreach"],
    dependencies=[Depends(require_member_or_platform(ROLE_SUB_ADMIN))],
)


def _template(row: OutreachTemplate) -> dict[str, Any]:
    try:
        variables = json.loads(row.variables or "[]")
    except json.JSONDecodeError:
        variables = []
    return {
        "id": row.id,
        "scope": row.scope,
        "name": row.name,
        "kind": row.kind,
        "kind_label": TEMPLATE_KIND_LABELS.get(row.kind, row.kind),
        "text": row.text,
        "variables": variables,
        "enabled": row.enabled,
        "version": row.version,
        "source_template_id": row.source_template_id,
        "source_version": row.source_version,
        "created_by": row.created_by,
        "updated_at": as_utc(row.updated_at),
    }


def _contact(row: OutreachContact) -> dict[str, Any]:
    return {
        "id": row.id,
        "tg_user_id": row.tg_user_id,
        "username": row.username,
        "display_name": row.display_name,
        "phone": row.phone,
        "contact_state": row.contact_state,
        "state_label": CONTACT_STATE_LABELS.get(row.contact_state, row.contact_state),
        "reply_state": row.reply_state,
        "contact_count": row.contact_count,
        "follow_up_count": row.follow_up_count,
        "first_contact_at": as_utc(row.first_contact_at),
        "last_contact_at": as_utc(row.last_contact_at),
        "global_lock_until": as_utc(row.global_lock_until),
        "do_not_contact": row.do_not_contact,
        "identity_confirmed": row.identity_confirmed,
        "created_at": as_utc(row.created_at),
    }


def _task(row: OutreachTask, contact: OutreachContact | None) -> dict[str, Any]:
    return {
        "id": row.id,
        "kind": row.kind,
        "status": row.status,
        "status_label": TASK_STATUS_LABELS.get(row.status, row.status),
        "contact_id": row.contact_id,
        "contact_name": (contact.display_name or contact.username) if contact else None,
        "contact_tg_user_id": contact.tg_user_id if contact else None,
        "lead_id": row.lead_id,
        "account_id": row.account_id,
        "template_id": row.template_id,
        "scheduled_at": as_utc(row.scheduled_at),
        "sent_at": as_utc(row.sent_at),
        "attempt_count": row.attempt_count,
        "last_error": row.last_error,
        "created_at": as_utc(row.created_at),
    }


@router.get("/settings")
async def get_settings(
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """租户级冷触达策略。"""
    return await outreach_settings_service.read_settings(session, tenant_scope_of(identity))


@router.patch("/settings")
async def patch_settings(
    payload: OutreachSettingsUpdate,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """更新冷触达策略。"""
    return await outreach_settings_service.update_settings(
        session,
        tenant_scope_of(identity),
        **payload.model_dump(exclude_unset=True),
    )


@router.get("/templates")
async def list_templates(
    kind: str | None = Query(default=None),
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """平台共享模板 + 本租户模板。"""
    rows = await outreach_template_service.list_templates(
        session,
        tenant_scope_of(identity),
        kind=kind,
    )
    return {"items": [_template(row) for row in rows], "total": len(rows)}


@router.post("/templates", status_code=201)
async def create_template(
    payload: OutreachTemplateCreate,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """新建会员自己的话术模板。"""
    row = await outreach_template_service.create_template(
        session,
        tenant_scope_of(identity),
        name=payload.name,
        kind=payload.kind,
        text=payload.text,
        variables=payload.variables,
        created_by=identity.get("username"),
    )
    return _template(row)


@router.patch("/templates/{template_id}")
async def update_template(
    template_id: int,
    payload: OutreachTemplateUpdate,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """修改会员自己的模板（平台模板只读）。"""
    row = await outreach_template_service.update_template(
        session,
        tenant_scope_of(identity),
        template_id,
        **payload.model_dump(exclude_unset=True),
    )
    return _template(row)


@router.delete("/templates/{template_id}")
async def delete_template(
    template_id: int,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """删除会员自己的模板。"""
    row = await outreach_template_service.delete_template(
        session,
        tenant_scope_of(identity),
        template_id,
    )
    return {"id": row.id, "name": row.name, "deleted": True}


@router.post("/templates/{template_id}/adopt", status_code=201)
async def adopt_template(
    template_id: int,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """选用平台模板：复制成自己的模板。"""
    row = await outreach_template_service.adopt_template(
        session,
        tenant_scope_of(identity),
        template_id,
        created_by=identity.get("username"),
    )
    return _template(row)


@router.get("/contacts")
async def list_contacts(
    state: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """联系档案（全局锁与会话归属）。"""
    statement = select(OutreachContact).order_by(OutreachContact.id.desc())
    count_statement = select(func.count()).select_from(OutreachContact)
    if state:
        statement = statement.where(OutreachContact.contact_state == state)
        count_statement = count_statement.where(OutreachContact.contact_state == state)
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)
    return {
        "items": [_contact(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/tasks")
async def list_tasks(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """冷触达任务队列。"""
    statement = select(OutreachTask).order_by(OutreachTask.id.desc())
    count_statement = select(func.count()).select_from(OutreachTask)
    if status:
        statement = statement.where(OutreachTask.status == status)
        count_statement = count_statement.where(OutreachTask.status == status)
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)

    contact_ids = {row.contact_id for row in rows}
    contacts: dict[int, OutreachContact] = {}
    if contact_ids:
        for contact in await session.scalars(
            select(OutreachContact).where(OutreachContact.id.in_(contact_ids))
        ):
            contacts[contact.id] = contact
    return {
        "items": [_task(row, contacts.get(row.contact_id)) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/queue/plan")
async def plan_queue(
    limit: int = Query(default=200, ge=1, le=2000),
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """dry-run 调度：把可触达的线索排进队列（不发送）。"""
    return await outreach_queue_service.plan_pending(
        session,
        tenant_id=tenant_scope_of(identity),
        limit=limit,
    )


@router.get("/capacity")
async def get_capacity(
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """今日可用冷聊总量与排队情况。"""
    return await outreach_queue_service.capacity(
        session,
        tenant_id=tenant_scope_of(identity),
    )
