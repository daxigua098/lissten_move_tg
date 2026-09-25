"""写操作审计日志。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditLog


async def write_audit_log(
    session: AsyncSession,
    *,
    username: str,
    method: str,
    path: str,
    status_code: int,
    ip_address: str | None = None,
) -> AuditLog:
    """写入一条审计记录。"""
    record = AuditLog(
        username=(username or "anonymous")[:64],
        method=method[:16],
        path=path[:512],
        status_code=status_code,
        ip_address=ip_address,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def list_audit_logs(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
    username: str | None = None,
    path: str | None = None,
) -> tuple[list[AuditLog], int]:
    """分页查询审计日志。"""
    statement = select(AuditLog).order_by(AuditLog.id.desc())
    count_statement = select(func.count()).select_from(AuditLog)
    if username:
        statement = statement.where(AuditLog.username == username)
        count_statement = count_statement.where(AuditLog.username == username)
    if path:
        statement = statement.where(AuditLog.path.like(f"%{path}%"))
        count_statement = count_statement.where(AuditLog.path.like(f"%{path}%"))
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)
    return rows, total
