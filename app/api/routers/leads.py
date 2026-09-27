"""线索池：查询、统计与导出。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_module, session_dependency
from app.db.models import MODULE_MONITOR
from app.services import lead_service

router = APIRouter(
    prefix="/api/leads",
    tags=["leads"],
    dependencies=[Depends(require_module(MODULE_MONITOR))],
)


@router.get("")
async def list_leads(
    source_chat_id: int | None = Query(default=None),
    keyword: str | None = Query(default=None),
    sender_tg_id: int | None = Query(default=None),
    days: int | None = Query(default=None, ge=1, le=365),
    delivered: bool | None = Query(default=None),
    only_hits: bool | None = Query(default=None, description="只看命中关键词的线索"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """线索列表（按时间倒序）。"""
    rows, total = await lead_service.list_leads(
        session,
        source_chat_id=source_chat_id,
        keyword=keyword,
        sender_tg_id=sender_tg_id,
        days=days,
        delivered=delivered,
        only_hits=only_hits,
        limit=limit,
        offset=offset,
    )
    return {
        "items": [lead_service.serialize_lead(row) for row in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/stats")
async def lead_stats(session: AsyncSession = Depends(session_dependency)) -> dict[str, Any]:
    """线索统计。"""
    return await lead_service.lead_stats(session)


@router.get("/export.csv")
async def export_leads(
    source_chat_id: int | None = Query(default=None),
    keyword: str | None = Query(default=None),
    days: int | None = Query(default=None, ge=1, le=365),
    only_hits: bool | None = Query(default=None),
    session: AsyncSession = Depends(session_dependency),
) -> Response:
    """导出线索为 CSV（按来源群 / 关键词 / 天数筛选）。"""
    rows, _total = await lead_service.list_leads(
        session,
        source_chat_id=source_chat_id,
        keyword=keyword,
        days=days,
        only_hits=only_hits,
        limit=5000,
        offset=0,
    )
    csv_text = lead_service.leads_to_csv(rows)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
    )
