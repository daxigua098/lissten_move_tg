"""热门关键词：排名查询与"加入词库"。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role, session_dependency
from app.api.schemas.lead import HotKeywordPromoteRequest
from app.core.errors import ConflictError
from app.db.models import ROLE_SUB_ADMIN
from app.services import hot_keyword_service, keyword_service

router = APIRouter(
    prefix="/api/hot-keywords",
    tags=["hot-keywords"],
    dependencies=[Depends(require_role(ROLE_SUB_ADMIN))],
)


@router.get("")
async def list_hot_keywords(
    days: int | None = Query(default=None, ge=1, le=365),
    min_count: int = Query(default=1, ge=1, le=100000),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """按出现次数排名（已做长词优先降噪）。"""
    items, total, message_total = await hot_keyword_service.rank_hot_keywords(
        session,
        days=days,
        min_count=min_count,
        limit=limit,
        offset=offset,
        merge_rules=await keyword_service.load_merge_rules(session),
    )
    return {
        "items": items,
        "total": total,
        "message_total": message_total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/stats")
async def hot_keyword_stats(session: AsyncSession = Depends(session_dependency)) -> dict[str, Any]:
    """概览统计。"""
    return await hot_keyword_service.stats(session)


@router.post("/promote", status_code=201)
async def promote_to_library(
    payload: HotKeywordPromoteRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """把热门词加进指定关键词组；已经存在就当成功，不报错。"""
    group = await keyword_service.get_group(session, payload.group_id)
    token = payload.token.strip()
    try:
        keyword = await keyword_service.add_keyword(
            session,
            group.id,
            word=token,
            aliases=",".join(item for item in payload.aliases if item and item != token),
        )
    except ConflictError:
        # 词库里已经有这个词：对使用者来说就是"已经在里面了"，不算失败
        return {"added": False, "group": group.name, "token": token, "reason": "already_exists"}
    return {
        "added": True,
        "group": group.name,
        "token": token,
        "keyword_id": keyword.id,
    }
