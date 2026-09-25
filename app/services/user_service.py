"""后台账号业务逻辑：创建、认证、改密、增删改与保护规则。"""

from __future__ import annotations

import re

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.errors import (
    BuiltinPasswordEnvError,
    ConflictError,
    InvalidCredentialsError,
    LastSuperAdminError,
    NotFoundError,
    PasswordWeakError,
    SelfOperationError,
    UserExistsError,
    ValidationFailedError,
)
from app.core.security import dummy_verify, hash_password, verify_password
from app.db.models import ROLE_RANK, ROLE_SUPER_ADMIN, User

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{3,64}$")
_LETTER_PATTERN = re.compile(r"[A-Za-z]")
_DIGIT_PATTERN = re.compile(r"\d")


def validate_username(username: str) -> str:
    """校验并规范化用户名。"""
    value = (username or "").strip()
    if not USERNAME_PATTERN.match(value):
        raise ValidationFailedError("用户名只能包含字母、数字、点、下划线、连字符，长度 3–64 位")
    return value


def validate_password(password: str, *, min_length: int) -> str:
    """校验密码强度：长度达标且同时包含字母与数字。"""
    value = password or ""
    if len(value) < min_length:
        raise PasswordWeakError(f"新密码至少需要 {min_length} 位")
    if not _LETTER_PATTERN.search(value) or not _DIGIT_PATTERN.search(value):
        raise PasswordWeakError("新密码需要同时包含字母与数字")
    return value


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    """按 ID 查询账号。"""
    return await session.get(User, user_id)


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    """按用户名查询账号。"""
    return await session.scalar(select(User).where(User.username == (username or "").strip()))


async def count_users(
    session: AsyncSession,
    *,
    role: str | None = None,
    enabled: bool | None = None,
) -> int:
    """统计账号数量。"""
    statement = select(func.count()).select_from(User)
    if role is not None:
        statement = statement.where(User.role == role)
    if enabled is not None:
        statement = statement.where(User.enabled == enabled)
    return int(await session.scalar(statement) or 0)


async def list_users(
    session: AsyncSession,
    *,
    limit: int = 20,
    offset: int = 0,
    role: str | None = None,
    enabled: bool | None = None,
) -> tuple[list[User], int]:
    """分页查询账号列表。"""
    statement = select(User).order_by(User.id)
    count_statement = select(func.count()).select_from(User)
    if role is not None:
        statement = statement.where(User.role == role)
        count_statement = count_statement.where(User.role == role)
    if enabled is not None:
        statement = statement.where(User.enabled == enabled)
        count_statement = count_statement.where(User.enabled == enabled)
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)
    return rows, total


async def count_active_super_admins(session: AsyncSession) -> int:
    """统计启用状态的超级管理员数量。"""
    return await count_users(session, role=ROLE_SUPER_ADMIN, enabled=True)


async def create_user(
    session: AsyncSession,
    config: AppConfig,
    *,
    username: str,
    password: str,
    role: str = "viewer",
    display_name: str | None = None,
    is_builtin: bool = False,
    must_change_password: bool = True,
) -> User:
    """创建账号（内置管理员与子管理员共用）。"""
    name = validate_username(username)
    if role not in ROLE_RANK:
        raise ValidationFailedError(f"角色必须是 {'/'.join(ROLE_RANK)} 之一")
    validate_password(password, min_length=config.security.password_min_length)
    if await get_user_by_username(session, name) is not None:
        raise UserExistsError()

    user = User(
        username=name,
        password_hash=hash_password(password),
        role=role,
        display_name=(display_name or "").strip() or None,
        is_builtin=is_builtin,
        must_change_password=must_change_password,
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def authenticate(
    session: AsyncSession,
    *,
    username: str,
    password: str,
) -> tuple[User | None, str | None]:
    """校验登录凭据。

    返回 `(user, None)` 表示通过；`(None, reason)` 表示失败，reason 用于写登录历史。
    对外响应文案统一为"用户名或密码错误"，避免通过响应差异枚举用户名。
    """
    user = await get_user_by_username(session, username)
    if user is None:
        dummy_verify()
        return None, "用户名或密码错误"
    if not verify_password(password or "", user.password_hash):
        return None, "用户名或密码错误"
    if not user.enabled:
        return None, "账号已被停用"
    return user, None


async def change_own_password(
    session: AsyncSession,
    config: AppConfig,
    *,
    user: User,
    current_password: str,
    new_password: str,
) -> User:
    """修改本人密码；内置管理员的密码在服务器 .env 中维护。"""
    if user.is_builtin:
        raise BuiltinPasswordEnvError()
    if not verify_password(current_password or "", user.password_hash):
        raise InvalidCredentialsError("当前密码错误")
    validate_password(new_password, min_length=config.security.password_min_length)

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    await session.commit()
    await session.refresh(user)
    return user


async def update_user(
    session: AsyncSession,
    config: AppConfig,
    *,
    actor_id: int | None,
    user_id: int,
    role: str | None = None,
    enabled: bool | None = None,
    password: str | None = None,
    display_name: str | None = None,
) -> User:
    """修改子管理员：角色、启停、重置密码、显示名。"""
    target = await get_user(session, user_id)
    if target is None:
        raise NotFoundError("账号不存在")

    if role is not None and role not in ROLE_RANK:
        raise ValidationFailedError(f"角色必须是 {'/'.join(ROLE_RANK)} 之一")

    if actor_id is not None and target.id == actor_id:
        if enabled is False:
            raise SelfOperationError("不能停用自己的账号")
        if role is not None and role != target.role:
            raise SelfOperationError("不能修改自己的角色")

    if await _would_remove_last_super_admin(session, target, role=role, enabled=enabled):
        raise LastSuperAdminError()

    if password:
        if target.is_builtin:
            raise BuiltinPasswordEnvError()
        validate_password(password, min_length=config.security.password_min_length)
        target.password_hash = hash_password(password)
        target.must_change_password = True

    if role is not None:
        target.role = role
    if enabled is not None:
        target.enabled = enabled
    if display_name is not None:
        target.display_name = display_name.strip() or None

    await session.commit()
    await session.refresh(target)
    return target


async def delete_user(
    session: AsyncSession,
    *,
    actor_id: int | None,
    user_id: int,
) -> User:
    """删除子管理员，保留最后一个可用的超级管理员。"""
    target = await get_user(session, user_id)
    if target is None:
        raise NotFoundError("账号不存在")
    if target.is_builtin:
        raise ConflictError("内置管理员不可删除")
    if actor_id is not None and target.id == actor_id:
        raise SelfOperationError()
    if await _would_remove_last_super_admin(session, target, delete=True):
        raise LastSuperAdminError()

    await session.delete(target)
    await session.commit()
    return target


async def _would_remove_last_super_admin(
    session: AsyncSession,
    target: User,
    *,
    role: str | None = None,
    enabled: bool | None = None,
    delete: bool = False,
) -> bool:
    """判断本次操作是否会让系统失去最后一个启用的超级管理员。"""
    if target.role != ROLE_SUPER_ADMIN or not target.enabled:
        return False
    still_super = (role is None or role == ROLE_SUPER_ADMIN) and not delete
    still_enabled = (enabled is None or enabled) and not delete
    if still_super and still_enabled:
        return False
    return await count_active_super_admins(session) <= 1
