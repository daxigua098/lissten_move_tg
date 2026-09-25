"""登录历史与失败锁定判定。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utc_now
from app.db.models import LoginHistory


async def record_login_attempt(
    session: AsyncSession,
    *,
    username: str,
    ip_address: str | None,
    user_agent: str | None,
    success: bool,
    reason: str | None = None,
) -> LoginHistory:
    """记录一次登录尝试。"""
    record = LoginHistory(
        username=(username or "").strip() or "unknown",
        ip_address=ip_address,
        user_agent=(user_agent or "")[:512] or None,
        success=success,
        reason=reason,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def count_recent_failures(
    session: AsyncSession,
    username: str,
    *,
    window_minutes: int,
    now: datetime | None = None,
) -> int:
    """统计窗口内的失败次数。"""
    since = (now or utc_now()) - timedelta(minutes=window_minutes)
    statement = (
        select(func.count())
        .select_from(LoginHistory)
        .where(
            LoginHistory.username == (username or "").strip(),
            LoginHistory.success.is_(False),
            LoginHistory.created_at >= since,
        )
    )
    return int(await session.scalar(statement) or 0)


async def locked_until(
    session: AsyncSession,
    username: str,
    *,
    window_minutes: int,
    max_failures: int,
    now: datetime | None = None,
) -> datetime | None:
    """若已达到失败上限，返回解锁时间；否则返回 None。"""
    current = now or utc_now()
    since = current - timedelta(minutes=window_minutes)
    statement = (
        select(LoginHistory.created_at)
        .where(
            LoginHistory.username == (username or "").strip(),
            LoginHistory.success.is_(False),
            LoginHistory.created_at >= since,
        )
        .order_by(LoginHistory.created_at.desc())
    )
    timestamps = list(await session.scalars(statement))
    if len(timestamps) < max_failures:
        return None
    latest = timestamps[0]
    if latest.tzinfo is None:
        latest = latest.replace(tzinfo=UTC)
    return latest + timedelta(minutes=window_minutes)


async def list_login_history(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
    username: str | None = None,
    success: bool | None = None,
) -> tuple[list[LoginHistory], int]:
    """分页查询登录历史。"""
    statement = select(LoginHistory).order_by(LoginHistory.id.desc())
    count_statement = select(func.count()).select_from(LoginHistory)
    if username:
        statement = statement.where(LoginHistory.username == username)
        count_statement = count_statement.where(LoginHistory.username == username)
    if success is not None:
        statement = statement.where(LoginHistory.success.is_(success))
        count_statement = count_statement.where(LoginHistory.success.is_(success))
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)
    return rows, total
