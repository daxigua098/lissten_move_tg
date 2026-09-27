"""服务端会话：创建、校验、单条与全部撤销。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import hash_session_token
from app.db.models import WebSession


async def create_web_session(
    session: AsyncSession,
    *,
    token: str,
    username: str,
    role: str,
    expires_at: datetime,
    ip_address: str | None = None,
) -> WebSession:
    """写入会话记录（只存令牌哈希）。"""
    record = WebSession(
        token_hash=hash_session_token(token),
        username=username,
        role=role,
        expires_at=expires_at,
        ip_address=ip_address,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def find_web_session(session: AsyncSession, token: str) -> WebSession | None:
    """按明文令牌查找有效会话（未撤销且未过期）。"""
    if not token:
        return None
    record = await session.get(WebSession, hash_session_token(token))
    if record is None or record.revoked:
        return None
    expires_at = record.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at <= datetime.now(UTC):
        return None
    return record


async def is_web_session_valid(session: AsyncSession, token: str) -> bool:
    """会话是否有效。"""
    return await find_web_session(session, token) is not None


async def revoke_web_session(session: AsyncSession, token: str) -> bool:
    """撤销单条会话。"""
    if not token:
        return False
    record = await session.get(WebSession, hash_session_token(token))
    if record is None or record.revoked:
        return False
    record.revoked = True
    await session.commit()
    return True


async def revoke_all_web_sessions(
    session: AsyncSession,
    username: str,
    *,
    commit: bool = True,
) -> int:
    """撤销某个账号的全部会话，返回撤销数量。

    ``commit=False`` 供平台重置密码那种"要和别的写操作放同一个事务"的场景使用。
    """
    result = await session.execute(
        update(WebSession)
        .where(WebSession.username == username, WebSession.revoked.is_(False))
        .values(revoked=True)
    )
    if commit:
        await session.commit()
    return int(result.rowcount or 0)


async def revoke_other_sessions(session: AsyncSession, username: str, keep_token: str) -> int:
    """撤销该账号除当前令牌外的全部会话。"""
    keep_hash = hash_session_token(keep_token)
    result = await session.execute(
        update(WebSession)
        .where(
            WebSession.username == username,
            WebSession.revoked.is_(False),
            WebSession.token_hash != keep_hash,
        )
        .values(revoked=True)
    )
    await session.commit()
    return int(result.rowcount or 0)


async def list_active_sessions(session: AsyncSession, username: str) -> list[WebSession]:
    """列出某账号未撤销的会话。"""
    return list(
        await session.scalars(
            select(WebSession)
            .where(WebSession.username == username, WebSession.revoked.is_(False))
            .order_by(WebSession.created_at.desc())
        )
    )
