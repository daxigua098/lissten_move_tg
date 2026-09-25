"""监听源管理：穿梭框、标签、启停与移出。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role, session_dependency
from app.api.routers.chats import page, serialize_chat
from app.api.schemas.chat import SourceAddRequest, SourceUpdateRequest, TagBatchRequest
from app.core.config import AppConfig
from app.core.errors import NotFoundError
from app.db.models import ROLE_SUB_ADMIN
from app.services import chat_service, chat_sync_service

router = APIRouter(
    prefix="/api/sources",
    tags=["sources"],
    dependencies=[Depends(require_role(ROLE_SUB_ADMIN))],
)


def _client_factory(request: Request) -> Any:
    return getattr(request.app.state, "account_client_factory", None)


@router.get("")
async def list_sources(
    enabled: bool | None = Query(default=None),
    tag: str | None = Query(default=None),
    keyword: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """已加入的监听源。"""
    rows, total = await chat_service.list_sources(
        session,
        enabled=enabled,
        tag=tag,
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
    """可选群组：已同步到本地但还不是监听源的聊天对象。"""
    rows, total = await chat_service.list_pool(
        session,
        exclude_sources=True,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    return page(rows, total, limit, offset)


@router.get("/tags")
async def list_source_tags(
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """全部标签。"""
    return {"items": await chat_service.list_tags(session)}


@router.post("/sync")
async def sync_dialogs(
    request: Request,
    account_id: int | None = Query(default=None),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """从执行账号同步已加入的群组/频道到本地群组池。"""
    config: AppConfig = request.app.state.config
    return await chat_sync_service.sync_dialogs(
        session,
        config,
        account_id=account_id,
        client_factory=_client_factory(request),
    )


@router.post("", status_code=201)
async def add_sources(
    payload: SourceAddRequest,
    request: Request,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """把勾选的群组或粘贴的链接加入监听源。"""
    config: AppConfig = request.app.state.config
    factory = _client_factory(request)
    added: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []

    chats = await chat_service.get_chats_by_ids(session, payload.chat_ids)
    found_ids = {chat.id for chat in chats}
    for missing in [item for item in payload.chat_ids if item not in found_ids]:
        failures.append({"input": f"chat_id={missing}", "reason": "群组不存在"})

    for chat in chats:
        chat = await chat_service.set_source(session, chat, enabled=True)
        if payload.tags:
            await chat_service.batch_update_tags(
                session,
                [chat.id],
                tags=payload.tags,
                mode="add",
            )
            chat = await chat_service.get_chat(session, chat.id)
        added.append(serialize_chat(chat))

    for raw in payload.inputs:
        try:
            chat = await chat_sync_service.ensure_chat_from_input(
                session,
                config,
                raw_input=raw,
                join=payload.join,
                account_id=payload.account_id,
                client_factory=factory,
            )
            chat = await chat_service.set_source(session, chat, enabled=True)
            if payload.tags:
                await chat_service.batch_update_tags(
                    session,
                    [chat.id],
                    tags=payload.tags,
                    mode="add",
                )
                chat = await chat_service.get_chat(session, chat.id)
            added.append(serialize_chat(chat))
        except Exception as exc:  # noqa: BLE001 - 单个链接失败不影响其他项
            failures.append({"input": raw, "reason": str(getattr(exc, "detail", exc))})

    return {"added": added, "failures": failures}


@router.patch("/{chat_id}")
async def update_source(
    chat_id: int,
    payload: SourceUpdateRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """更新监听源的启停、标签与备注。"""
    chat = await chat_service.get_chat(session, chat_id)
    if chat is None or not chat.is_source:
        raise NotFoundError("监听源不存在")
    if payload.display_name is not None:
        chat.display_name = payload.display_name.strip() or None
    if payload.enabled is not None:
        chat.source_enabled = payload.enabled
    if payload.tags is not None:
        chat.tags = chat_service.dump_tags(payload.tags)
    if payload.note is not None:
        chat.note = payload.note.strip() or None
    await session.commit()
    await session.refresh(chat)
    return serialize_chat(chat)


@router.delete("/{chat_id}")
async def remove_source(
    chat_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """把聊天对象移出监听源。"""
    chat = await chat_service.get_chat(session, chat_id)
    if chat is None or not chat.is_source:
        raise NotFoundError("监听源不存在")
    chat = await chat_service.unset_source(session, chat)
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
