"""关键词组管理：组、关键词与别名，附带匹配试跑。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role, session_dependency
from app.api.schemas.lead import (
    KeywordCreateRequest,
    KeywordGroupCreateRequest,
    KeywordGroupUpdateRequest,
    KeywordMatchRequest,
    KeywordUpdateRequest,
)
from app.core.keyword_matcher import match_text
from app.db.models import ROLE_SUB_ADMIN
from app.services import keyword_service

router = APIRouter(
    prefix="/api/keyword-groups",
    tags=["keywords"],
    dependencies=[Depends(require_role(ROLE_SUB_ADMIN))],
)


@router.get("")
async def list_groups(
    kind: str | None = Query(default=None, pattern="^(keyword|exclude|merge)$"),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """词组列表（含组内词）；kind 区分关键词组与排除词组。"""
    rows = await keyword_service.list_groups(session, kind)
    return {"items": [keyword_service.serialize_group(row) for row in rows]}


@router.post("", status_code=201)
async def create_group(
    payload: KeywordGroupCreateRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """新建关键词组。"""
    group = await keyword_service.create_group(
        session,
        name=payload.name,
        description=payload.description,
        kind=payload.kind,
    )
    return keyword_service.serialize_group(group, with_keywords=False)


@router.post("/seed")
async def seed_groups(session: AsyncSession = Depends(session_dependency)) -> dict[str, Any]:
    """写入预置的中文类别别名库（已有分组时不动）。"""
    created = await keyword_service.ensure_seed_groups(session)
    rows = await keyword_service.list_groups(session)
    return {"created": created, "items": [keyword_service.serialize_group(row) for row in rows]}


@router.post("/match")
async def match_preview(
    payload: KeywordMatchRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """拿一段文本试跑关键词，看看会不会命中（调词表时很有用）。"""
    entries = await keyword_service.load_entries(session, payload.group_ids or None)
    exclude_words = list(payload.exclude_keywords) + await keyword_service.load_exclude_words(
        session,
        payload.exclude_group_ids,
    )
    hits = match_text(
        payload.text,
        entries,
        sensitivity=payload.sensitivity,
        match_contains=payload.match_contains,
        match_fuzzy=payload.match_fuzzy,
        exclude=tuple(dict.fromkeys(exclude_words)),
    )
    return {
        "keyword_total": len(entries),
        "exclude_total": len(set(exclude_words)),
        "hits": [
            {
                "keyword": item.keyword,
                "matched": item.matched,
                "mode": item.mode,
                "score": item.score,
                "group_id": item.group_id,
            }
            for item in hits
        ],
    }


@router.patch("/{group_id}")
async def update_group(
    group_id: int,
    payload: KeywordGroupUpdateRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """修改关键词组。"""
    group = await keyword_service.update_group(
        session,
        group_id,
        name=payload.name,
        description=payload.description,
        enabled=payload.enabled,
    )
    return keyword_service.serialize_group(group, with_keywords=False)


@router.delete("/{group_id}")
async def delete_group(
    group_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """删除关键词组（组内关键词一起删）。"""
    await keyword_service.delete_group(session, group_id)
    return {"deleted": True}


@router.post("/{group_id}/keywords", status_code=201)
async def add_keyword(
    group_id: int,
    payload: KeywordCreateRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """在组里新增关键词。"""
    keyword = await keyword_service.add_keyword(
        session,
        group_id,
        word=payload.word,
        aliases=payload.aliases,
        enabled=payload.enabled,
    )
    return keyword_service.serialize_keyword(keyword)


keyword_router = APIRouter(
    prefix="/api/keywords",
    tags=["keywords"],
    dependencies=[Depends(require_role(ROLE_SUB_ADMIN))],
)


@keyword_router.patch("/{keyword_id}")
async def update_keyword(
    keyword_id: int,
    payload: KeywordUpdateRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """修改关键词（含别名）。"""
    keyword = await keyword_service.update_keyword(
        session,
        keyword_id,
        word=payload.word,
        aliases=payload.aliases,
        enabled=payload.enabled,
    )
    return keyword_service.serialize_keyword(keyword)


@keyword_router.delete("/{keyword_id}")
async def delete_keyword(
    keyword_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """删除关键词。"""
    await keyword_service.delete_keyword(session, keyword_id)
    return {"deleted": True}
