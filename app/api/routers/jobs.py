"""投递任务查询与重试。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import current_identity, require_role, session_dependency
from app.core.errors import NotFoundError
from app.db.base import as_utc
from app.db.models import JOB_PENDING, ROLE_SUB_ADMIN, DeliveryJob, Route, TenantChat
from app.services import delivery_service

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

STATUS_LABEL = {
    "pending": "待投递",
    "processing": "投递中",
    "success": "成功",
    "retrying": "重试中",
    "failed": "失败",
    "skipped": "已跳过",
}


async def _titles(session: AsyncSession, chat_ids: set[int]) -> dict[int, str]:
    if not chat_ids:
        return {}
    rows = await session.scalars(select(TenantChat).where(TenantChat.id.in_(chat_ids)))

    def display(item: TenantChat) -> str:
        return item.display_name or item.title or item.username or f"#{item.id}"

    return {item.id: display(item) for item in rows}


@router.get("", dependencies=[Depends(current_identity)])
async def list_jobs(
    status: str | None = Query(default=None),
    route_id: int | None = Query(default=None),
    target_chat_id: int | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """投递任务列表（所有登录角色可读）。"""
    conditions = []
    if status:
        conditions.append(DeliveryJob.status == status)
    if route_id is not None:
        conditions.append(DeliveryJob.route_id == route_id)
    if target_chat_id is not None:
        conditions.append(DeliveryJob.target_chat_id == target_chat_id)

    statement = select(DeliveryJob).order_by(DeliveryJob.id.desc())
    count_statement = select(func.count()).select_from(DeliveryJob)
    for condition in conditions:
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)

    chat_ids = {row.source_chat_id for row in rows} | {row.target_chat_id for row in rows}
    titles = await _titles(session, chat_ids)
    route_ids = {row.route_id for row in rows if row.route_id}
    route_names: dict[int, str] = {}
    if route_ids:
        for route in await session.scalars(select(Route).where(Route.id.in_(route_ids))):
            route_names[route.id] = route.name

    return {
        "items": [
            {
                "id": row.id,
                "route_id": row.route_id,
                "route_name": route_names.get(row.route_id) if row.route_id else None,
                "source_chat_id": row.source_chat_id,
                "source_title": titles.get(row.source_chat_id),
                "source_message_id": row.source_message_id,
                "target_chat_id": row.target_chat_id,
                "target_title": titles.get(row.target_chat_id),
                "target_message_id": row.target_message_id,
                "status": row.status,
                "status_label": STATUS_LABEL.get(row.status, row.status),
                "attempt_count": row.attempt_count,
                "max_attempts": row.max_attempts,
                "last_error": row.last_error,
                "ad_applied": row.ad_applied,
                "next_retry_at": as_utc(row.next_retry_at),
                "sent_at": as_utc(row.sent_at),
                "created_at": as_utc(row.created_at),
            }
            for row in rows
        ],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/stats", dependencies=[Depends(current_identity)])
async def job_stats(session: AsyncSession = Depends(session_dependency)) -> dict[str, Any]:
    """按状态统计投递任务。"""
    stats = await delivery_service.job_stats(session)
    return {
        "items": [
            {"status": status, "label": STATUS_LABEL.get(status, status), "count": count}
            for status, count in stats.items()
        ],
        "total": sum(stats.values()),
    }


@router.post(
    "/retry-failed",
    dependencies=[Depends(require_role(ROLE_SUB_ADMIN))],
)
async def retry_failed(session: AsyncSession = Depends(session_dependency)) -> dict[str, Any]:
    """把所有失败任务重新排队。"""
    retried = await delivery_service.retry_failed_jobs(session)
    return {"retried": retried}


@router.post("/{job_id}/retry", dependencies=[Depends(require_role(ROLE_SUB_ADMIN))])
async def retry_one(
    job_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """重试单条任务。"""
    job = await session.get(DeliveryJob, job_id)
    if job is None:
        raise NotFoundError("投递任务不存在")
    job.status = JOB_PENDING
    job.attempt_count = 0
    job.next_retry_at = None
    job.last_error = None
    await session.commit()
    await session.refresh(job)
    return {"id": job.id, "status": job.status, "retried": True}


@router.post("/{job_id}/skip", dependencies=[Depends(require_role(ROLE_SUB_ADMIN))])
async def skip_one(
    job_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """跳过单条任务。"""
    job = await session.get(DeliveryJob, job_id)
    if job is None:
        raise NotFoundError("投递任务不存在")
    await delivery_service.skip_job(session, job, reason="管理员手动跳过")
    return {"id": job.id, "status": job.status, "skipped": True}
