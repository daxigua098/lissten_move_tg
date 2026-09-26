"""接收组管理：穿梭框、用途标记、权限预检。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role, session_dependency
from app.api.routers.chats import page, serialize_chat
from app.api.schemas.chat import TagBatchRequest, TargetAddRequest, TargetUpdateRequest
from app.core.config import AppConfig
from app.core.errors import NotFoundError
from app.db.models import ROLE_SUB_ADMIN
from app.services import chat_service, chat_sync_service, route_service

router = APIRouter(
    prefix="/api/targets",
    tags=["targets"],
    dependencies=[Depends(require_role(ROLE_SUB_ADMIN))],
)


def _client_factory(request: Request) -> Any:
    return getattr(request.app.state, "account_client_factory", None)


@router.get("")
async def list_targets(
    role: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
    keyword: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """已加入的接收组。"""
    rows, total = await chat_service.list_targets(
        session,
        role=role,
        enabled=enabled,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    return page(rows, total, limit, offset)


@router.get("/available")
async def list_available(
    keyword: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """可选群组：还不是接收组的聊天对象。"""
    rows, total = await chat_service.list_pool(
        session,
        exclude_targets=True,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    return page(rows, total, limit, offset)


@router.post("/sync")
async def sync_dialogs(
    request: Request,
    account_id: int | None = Query(default=None),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """从执行账号同步已加入的群组/频道到群组池（左侧「可选群组」的来源）。"""
    return await chat_sync_service.sync_dialogs(
        session,
        request.app.state.config,
        account_id=account_id,
        client_factory=_client_factory(request),
    )


@router.post("", status_code=201)
async def add_targets(
    payload: TargetAddRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """把勾选的群组或链接加入接收组，并做发言权限预检。"""
    config: AppConfig = request.app.state.config
    factory = _client_factory(request)
    added: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    chats = await chat_service.get_chats_by_ids(session, payload.chat_ids)
    found_ids = {chat.id for chat in chats}
    for missing in [item for item in payload.chat_ids if item not in found_ids]:
        failures.append({"input": f"chat_id={missing}", "reason": "群组不存在"})

    for raw in payload.inputs:
        try:
            chats.append(
                await chat_sync_service.ensure_chat_from_input(
                    session,
                    config,
                    raw_input=raw,
                    join=payload.join,
                    account_id=payload.account_id,
                    client_factory=factory,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 单个链接失败不影响其他项
            failures.append({"input": raw, "reason": str(getattr(exc, "detail", exc))})

    access = {}
    if payload.check_access and chats and factory is not None:
        access = await chat_sync_service.check_targets_access(
            session,
            config,
            chats,
            client_factory=factory,
            account_id=payload.account_id,
        )

    for chat in chats:
        chat = await chat_service.set_target(
            session,
            chat,
            role=payload.role,
            enabled=True,
        )
        if payload.tags:
            await chat_service.batch_update_tags(
                session,
                [chat.id],
                tags=payload.tags,
                mode="add",
            )
        if chat.id in access and access[chat.id] is not None:
            chat = await chat_service.update_chat(session, chat.id, can_post=access[chat.id])
        added.append(serialize_chat(chat))

    return {"added": added, "failures": failures}


@router.patch("/{chat_id}")
async def update_target(
    chat_id: int,
    payload: TargetUpdateRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """更新接收组的启停、用途、标签与备注。"""
    chat = await chat_service.get_chat(session, chat_id)
    if chat is None or not chat.is_target:
        raise NotFoundError("接收组不存在")
    if payload.display_name is not None:
        chat.display_name = payload.display_name.strip() or None
    if payload.enabled is not None:
        chat.target_enabled = payload.enabled
        # 总开关：同步到所有线路上的该目标，不然界面显示关闭、实际还在投递
        await route_service.set_chat_target_enabled_everywhere(
            session,
            chat.id,
            payload.enabled,
        )
    if payload.role is not None:
        chat = await chat_service.set_target(session, chat, role=payload.role, enabled=True)
        if payload.enabled is not None:
            chat.target_enabled = payload.enabled
            await route_service.set_chat_target_enabled_everywhere(
                session,
                chat.id,
                payload.enabled,
            )
    if payload.tags is not None:
        chat.tags = chat_service.dump_tags(payload.tags)
    if payload.note is not None:
        chat.note = payload.note.strip() or None
    await session.commit()
    await session.refresh(chat)
    return serialize_chat(chat)


@router.delete("/{chat_id}")
async def remove_target(
    chat_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """把聊天对象移出接收组。"""
    chat = await chat_service.get_chat(session, chat_id)
    if chat is None or not chat.is_target:
        raise NotFoundError("接收组不存在")
    chat = await chat_service.unset_target(session, chat)
    return {"id": chat_id, "title": chat.title, "removed": True}


@router.post("/tags/batch")
async def batch_tags(
    payload: TagBatchRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """批量打标签。"""
    updated = await chat_service.batch_update_tags(
        session,
        payload.chat_ids,
        tags=payload.tags,
        mode=payload.mode,
    )
    return {"updated": updated}
