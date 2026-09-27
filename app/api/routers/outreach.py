"""冷触达（P1）：租户策略、话术模板、联系锁与任务队列。

本阶段只做「过闸入队 + 容量展示」，不发送任何消息。
"""

from __future__ import annotations

import contextlib
import json
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    current_identity,
    require_member_or_platform,
    session_dependency,
    tenant_scope_of,
)
from app.api.schemas.outreach import (
    BatchRetireRequest,
    HandoffConsumeRequest,
    OutreachSettingsUpdate,
    OutreachTemplateCreate,
    OutreachTemplateUpdate,
    ParticipationRequest,
    TaskDeleteRequest,
    UnknownDeliveryConfirmRequest,
)
from app.core.errors import NotFoundError, ValidationFailedError
from app.core.outreach_capture import parse_reachable_routes
from app.core.runtime_control import is_paused, set_paused
from app.db.base import as_utc
from app.db.models import (
    ACCOUNT_PURPOSE_OUTREACH,
    CONTACT_STATE_LABELS,
    ROLE_SUB_ADMIN,
    TASK_STATUS_LABELS,
    TEMPLATE_KIND_LABELS,
    Lead,
    OutreachContact,
    OutreachTask,
    OutreachTemplate,
    TgAccount,
)
from app.services import (
    outreach_account_service,
    outreach_handoff_service,
    outreach_queue_service,
    outreach_reply_service,
    outreach_sender_service,
    outreach_settings_service,
    outreach_template_service,
    tg_account_service,
    upload_service,
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
        "media_path": row.media_path,
        "media_kind": row.media_kind,
        "media_url": upload_service.public_url(row.media_path),
        "enabled": row.enabled,
        "version": row.version,
        "source_template_id": row.source_template_id,
        "source_version": row.source_version,
        "created_by": row.created_by,
        "updated_at": as_utc(row.updated_at),
    }


def _outreach_control_path(config: Any) -> Any:
    return config.path(config.runtime.outreach_control_file)


def _csv_ints(raw: str | None) -> list[int] | None:
    if not raw:
        return None
    try:
        values = [int(item.strip()) for item in raw.split(",") if item.strip()]
    except ValueError as exc:
        raise ValidationFailedError("筛选 ID 必须是逗号分隔的整数") from exc
    return values or None


async def _open_outreach_client(config: Any, account: Any, factory: Any) -> Any:
    """打开发信息账号连接（演练模式用模拟客户端）。"""
    from app.core.telegram_client import connect_user_client, session_file_path

    if config.app.demo_mode:
        from app.core.demo_client import DemoAccountClient

        return DemoAccountClient()
    _phone, api_id, api_hash = tg_account_service.decrypt_credentials(config, account)
    session_path = session_file_path(config, account.session_name)
    if factory is not None:
        return await factory(
            config,
            api_id=api_id,
            api_hash=api_hash,
            session_path=session_path,
        )
    return await connect_user_client(
        config,
        api_id=api_id,
        api_hash=api_hash,
        session_path=session_path,
    )


def _lead(row: Lead) -> dict[str, Any]:
    """线索详情：监听抓取到的东西都在这里。"""
    contacts: dict[str, Any] = {}
    try:
        payload = json.loads(row.contacts or "{}")
        if isinstance(payload, dict):
            contacts = payload
    except json.JSONDecodeError:
        contacts = {}
    return {
        "id": row.id,
        "source_title": row.source_title,
        "message_id": row.message_id,
        "message_at": as_utc(row.message_at),
        "sender_tg_id": row.sender_tg_id,
        "sender_username": row.sender_username,
        "sender_name": row.sender_name,
        "phone": row.phone,
        "wechat": row.wechat,
        "contacts": contacts,
        "keyword": row.keyword,
        "matched_mode": row.matched_mode,
        "score": row.score,
        "text": row.text,
        "reachable_routes": parse_reachable_routes(row.reachable_routes),
        "consent_type": row.consent_type,
        "capture_reason": row.capture_reason,
        "delivered": row.delivered,
        "delivered_at": as_utc(row.delivered_at),
        "created_at": as_utc(row.created_at),
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
        "owner_type": row.owner_type,
        "owner_account_id": row.owner_account_id,
        "owner_bot_id": row.owner_bot_id,
        "contact_count": row.contact_count,
        "follow_up_count": row.follow_up_count,
        "first_contact_at": as_utc(row.first_contact_at),
        "last_contact_at": as_utc(row.last_contact_at),
        "global_lock_until": as_utc(row.global_lock_until),
        "do_not_contact": row.do_not_contact,
        "identity_confirmed": row.identity_confirmed,
        "created_at": as_utc(row.created_at),
    }


def _task(
    row: OutreachTask,
    contact: OutreachContact | None,
    lead: Lead | None = None,
) -> dict[str, Any]:
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
        "priority": row.priority,
        "priority_reason": row.priority_reason,
        "scheduled_at": as_utc(row.scheduled_at),
        "next_plan_at": as_utc(row.next_retry_at),
        "source_title": lead.source_title if lead is not None else None,
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
        media_path=payload.media_path,
        media_kind=payload.media_kind,
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
    lead_ids = {row.lead_id for row in rows if row.lead_id is not None}
    contacts: dict[int, OutreachContact] = {}
    leads: dict[int, Lead] = {}
    if contact_ids:
        for contact in await session.scalars(
            select(OutreachContact).where(OutreachContact.id.in_(contact_ids))
        ):
            contacts[contact.id] = contact
    if lead_ids:
        for lead in await session.scalars(select(Lead).where(Lead.id.in_(lead_ids))):
            leads[lead.id] = lead
    return {
        "items": [
            _task(
                row,
                contacts.get(row.contact_id),
                leads.get(row.lead_id) if row.lead_id else None,
            )
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.post("/queue/plan")
async def plan_queue(
    limit: int = Query(default=200, ge=1, le=2000),
    route_ids: str | None = Query(default=None, description="逗号分隔的线路 ID"),
    source_chat_ids: str | None = Query(default=None, description="逗号分隔的来源群 ID"),
    keyword: str | None = Query(default=None, max_length=64),
    only_hits: bool = Query(default=False),
    authorized_only: bool = Query(default=False),
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """把可触达的线索排进队列（不发送）。"""
    return await outreach_queue_service.plan_pending(
        session,
        tenant_id=tenant_scope_of(identity),
        limit=limit,
        route_ids=_csv_ints(route_ids),
        source_chat_ids=_csv_ints(source_chat_ids),
        keyword=keyword,
        only_hits=only_hits,
        authorized_only=authorized_only,
    )


@router.get("/queue/preview")
async def preview_queue(
    limit: int = Query(default=200, ge=1, le=2000),
    route_ids: str | None = Query(default=None),
    source_chat_ids: str | None = Query(default=None),
    keyword: str | None = Query(default=None, max_length=64),
    only_hits: bool = Query(default=False),
    authorized_only: bool = Query(default=False),
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """只读预演，不创建任务也不修改受阻状态。"""
    return await outreach_queue_service.preview_pending(
        session,
        tenant_id=tenant_scope_of(identity),
        limit=limit,
        route_ids=_csv_ints(route_ids),
        source_chat_ids=_csv_ints(source_chat_ids),
        keyword=keyword,
        only_hits=only_hits,
        authorized_only=authorized_only,
    )


@router.post("/tasks/delete")
async def delete_tasks(
    payload: TaskDeleteRequest,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """删除队列任务（单条 / 多选都走这里）。"""
    return await outreach_queue_service.delete_tasks(
        session,
        payload.task_ids,
        tenant_id=tenant_scope_of(identity),
    )


@router.post("/queue/clear")
async def clear_queue(
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """清空冷触达队列（删待发送任务；历史联系记录不动）。"""
    return await outreach_queue_service.clear_queue(
        session,
        tenant_id=tenant_scope_of(identity),
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


@router.post("/contacts/{contact_id}/takeover")
async def takeover_contact(
    contact_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """A 模式人工接管：标记为人工处理，不再有任何自动动作。"""
    contact = await session.get(OutreachContact, contact_id)
    if contact is None:
        raise NotFoundError("联系人不存在")
    await outreach_sender_service.takeover(session, contact)
    return _contact(contact)


@router.post("/contacts/{contact_id}/handoff")
async def handoff_contact(
    contact_id: int,
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """引导进 Bot：由归属账号发一条带一次性深链接的消息。"""
    config: Any = request.app.state.config
    contact = await session.get(OutreachContact, contact_id)
    if contact is None:
        raise NotFoundError("联系人不存在")
    if not contact.owner_account_id:
        raise ValidationFailedError("该会话还没有归属账号，不能转交")
    account = await session.get(TgAccount, contact.owner_account_id)
    if account is None:
        raise ValidationFailedError("归属账号不存在")
    settings = await outreach_settings_service.read_settings(session, contact.tenant_id)
    factory = getattr(request.app.state, "account_client_factory", None)
    client = await _open_outreach_client(config, account, factory)
    try:
        return await outreach_handoff_service.send_handoff(
            session,
            client=client,
            account=account,
            contact=contact,
            settings=settings,
            created_by=identity.get("username"),
        )
    finally:
        with contextlib.suppress(Exception):
            await client.disconnect()


@router.post("/handoff/consume")
async def consume_handoff(
    payload: HandoffConsumeRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """用户按下 Bot 的 Start：令牌作废并把会话归属转给 Bot。"""
    contact = await outreach_handoff_service.consume(
        session,
        payload.token,
        tg_user_id=payload.tg_user_id,
    )
    if contact is None:
        raise NotFoundError("转交令牌无效或已过期")
    return _contact(contact)


@router.post("/contacts/{contact_id}/resume-auto")
async def resume_auto_reply(
    contact_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """人工处理完，把这个会话交回自动回复。"""
    contact = await session.get(OutreachContact, contact_id)
    if contact is None:
        raise NotFoundError("联系人不存在")
    await outreach_reply_service.resume_auto(session, contact)
    return _contact(contact)


@router.post("/contacts/{contact_id}/suppress")
async def suppress_contact(
    contact_id: int,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """把联系人加入永久免打扰名单。"""
    contact = await session.get(OutreachContact, contact_id)
    if contact is None:
        raise NotFoundError("联系人不存在")
    await outreach_sender_service.add_suppression(
        session,
        contact,
        reason="manual",
        created_by=identity.get("username"),
    )
    await outreach_sender_service.cancel_queued(session, contact.id, reason="人工拉黑")
    await session.commit()
    await session.refresh(contact)
    return _contact(contact)


@router.delete("/contacts/{contact_id}/suppress")
async def unsuppress_contact(
    contact_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """解除免打扰（仅人工操作）。"""
    contact = await session.get(OutreachContact, contact_id)
    if contact is None:
        raise NotFoundError("联系人不存在")
    await outreach_sender_service.remove_suppression(session, contact)
    await session.commit()
    await session.refresh(contact)
    return _contact(contact)


@router.post("/queue/dispatch")
async def dispatch_queue(
    request: Request,
    limit: int = Query(default=1, ge=1, le=20),
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """立即发送：按闸门取号，最多发送 ``limit`` 条（运维手动触发）。"""
    config: Any = request.app.state.config
    tenant_id = tenant_scope_of(identity)
    settings = await outreach_settings_service.read_settings(session, tenant_id)
    if settings.get("kill_switch"):
        raise ValidationFailedError("冷触达已熔断，请先到「策略」里关闭熔断")

    factory = getattr(request.app.state, "account_client_factory", None)
    clients: dict[int, Any] = {}
    results: list[dict[str, Any]] = []
    blocked_reason = ""
    try:
        for _ in range(limit):
            allowed, blocked_reason = await outreach_sender_service.enforce_send_window(
                session,
                tenant_id,
            )
            if not allowed:
                break
            task = await outreach_queue_service.next_ready_task(session, tenant_id)
            if task is None:
                break
            account = await outreach_sender_service.pick_account(session, tenant_id)
            if account is None:
                break
            contact = await session.get(OutreachContact, task.contact_id)
            if contact is None:
                break
            client = clients.get(account.id)
            if client is None:
                client = await _open_outreach_client(config, account, factory)
                clients[account.id] = client
            results.append(
                await outreach_sender_service.send_task(
                    session,
                    client=client,
                    account=account,
                    task=task,
                    contact=contact,
                    config=config,
                )
            )
    finally:
        for client in clients.values():
            with contextlib.suppress(Exception):
                await client.disconnect()

    sent = sum(1 for item in results if item.get("status") == "SENT")
    response: dict[str, Any] = {"sent": sent, "results": results}
    if blocked_reason:
        response["blocked_reason"] = blocked_reason
    return response


@router.get("/runtime/status")
async def runtime_status(
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """冷触达开关与队列概况。"""
    config: Any = request.app.state.config
    capacity = await outreach_queue_service.capacity(
        session,
        tenant_id=tenant_scope_of(identity),
    )
    return {
        "paused": is_paused(_outreach_control_path(config)),
        **capacity,
    }


@router.post("/runtime/pause")
async def runtime_pause(request: Request) -> dict[str, Any]:
    """暂停冷触达（可逆，不影响搬运 / 监听）。"""
    config: Any = request.app.state.config
    set_paused(_outreach_control_path(config), True)
    return {"paused": True}


@router.post("/runtime/resume")
async def runtime_resume(request: Request) -> dict[str, Any]:
    """恢复冷触达。"""
    config: Any = request.app.state.config
    set_paused(_outreach_control_path(config), False)
    return {"paused": False}


@router.post("/accounts/batch-retire")
async def batch_retire_accounts(
    payload: BatchRetireRequest,
    request: Request,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """批量退役发信息账号：冻结会话、回收在途任务，默认只软删。"""
    config: Any = request.app.state.config
    settings = await outreach_settings_service.read_settings(
        session,
        tenant_scope_of(identity),
    )
    delete_session = bool(
        payload.delete_session or settings.get("delete_session_on_account_delete")
    )
    from app.core.telegram_client import session_file_path

    items: list[dict[str, Any]] = []
    for account_id in payload.account_ids:
        account = await session.get(TgAccount, account_id)
        if account is None or account.purpose != ACCOUNT_PURPOSE_OUTREACH:
            continue
        items.append(
            await outreach_account_service.retire(
                session,
                account,
                reason=payload.reason,
                hard=payload.hard,
                delete_session=delete_session,
                session_path=session_file_path(config, account.session_name),
            )
        )
    return {
        "items": items,
        "count": len(items),
        "frozen_contacts": sum(item["frozen_contacts"] for item in items),
        "delete_session": delete_session,
        "hard": payload.hard,
    }


@router.post("/accounts/participation")
async def set_accounts_participation(
    payload: ParticipationRequest,
    identity: dict[str, Any] = Depends(current_identity),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """勾选参与冷触达（READY）/ 取消（PAUSED）；策略对这些账号一律生效。"""
    return await outreach_account_service.set_participation(
        session,
        payload.account_ids,
        enabled=payload.enabled,
        tenant_id=tenant_scope_of(identity),
    )


@router.get("/tasks/{task_id}/detail")
async def task_detail(
    task_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """队列单条详情：任务 + 联系人 + 监听抓取到的来源线索。"""
    task = await session.get(OutreachTask, task_id)
    if task is None:
        raise NotFoundError("任务不存在")
    contact = await session.get(OutreachContact, task.contact_id)
    lead_id = task.lead_id or (contact.first_lead_id if contact is not None else None)
    lead = await session.get(Lead, lead_id) if lead_id else None
    return {
        "task": _task(task, contact, lead),
        "contact": _contact(contact) if contact is not None else None,
        "lead": _lead(lead) if lead is not None else None,
    }


@router.post("/tasks/{task_id}/confirm-delivery")
async def confirm_delivery(
    task_id: int,
    payload: UnknownDeliveryConfirmRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """人工确认「待核实」任务是否已经送达。"""
    task = await session.get(OutreachTask, task_id)
    if task is None:
        raise NotFoundError("任务不存在")
    return await outreach_sender_service.confirm_unknown_delivery(
        session,
        task=task,
        delivered=payload.delivered,
    )


@router.post("/tasks/{task_id}/retry")
async def retry_task(
    task_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """人工重试临时失败的发送任务。"""
    task = await session.get(OutreachTask, task_id)
    if task is None:
        raise NotFoundError("任务不存在")
    return await outreach_sender_service.retry_failed_task(session, task=task)
