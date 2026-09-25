"""执行账号登录：在后台网页里分步完成（发送验证码 → 验证码 → 两步验证密码）。

未完成的登录会话保存在内存里（含尚未授权的 Telethon 客户端），超时自动断开。
"""

from __future__ import annotations

import contextlib
import time
from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.errors import ValidationFailedError
from app.core.paths import ensure_dir
from app.core.security import mask_phone
from app.core.telegram_client import (
    build_user_client,
    fetch_account_profile,
    session_file_path,
)
from app.db.models import TgAccount
from app.services import tg_account_service

LOGIN_TTL_SECONDS = 600


@dataclass
class PendingLogin:
    """一次未完成的登录。"""

    account_id: int
    phone: str
    client: Any
    phone_code_hash: str | None = None
    stage: str = "code_sent"
    created_at: float = field(default_factory=time.monotonic)

    @property
    def expired(self) -> bool:
        """是否已超时。"""
        return (time.monotonic() - self.created_at) > LOGIN_TTL_SECONDS


_PENDING: dict[int, PendingLogin] = {}


def pending_status(account_id: int) -> dict[str, Any]:
    """查询该账号是否有未完成的登录。"""
    _sweep()
    item = _PENDING.get(account_id)
    if item is None:
        return {"pending": False, "stage": "idle"}
    return {
        "pending": True,
        "stage": item.stage,
        "phone_masked": mask_phone(item.phone),
        "age_seconds": int(time.monotonic() - item.created_at),
        "expires_in": max(0, LOGIN_TTL_SECONDS - int(time.monotonic() - item.created_at)),
    }


async def start_login(
    config: AppConfig,
    account: TgAccount,
    *,
    client_factory: Any = None,
    force_sms: bool = False,
) -> dict[str, Any]:
    """第一步：连接 Telegram 并请求发送验证码。"""
    await cancel_login(account.id)

    phone, api_id, api_hash = tg_account_service.decrypt_credentials(config, account)
    session_path = session_file_path(config, account.session_name)
    ensure_dir(session_path.parent)

    client = await _open_client(
        config,
        api_id=api_id,
        api_hash=api_hash,
        session_path=session_path,
        client_factory=client_factory,
    )
    try:
        result = await client.send_code_request(phone, force_sms=force_sms)
    except Exception as exc:  # noqa: BLE001 - 需要把 Telegram 的提示原样带回界面
        with contextlib.suppress(Exception):
            await client.disconnect()
        raise ValidationFailedError(_friendly_error(exc)) from exc

    code_hash = getattr(result, "phone_code_hash", None)
    _PENDING[account.id] = PendingLogin(
        account_id=account.id,
        phone=phone,
        client=client,
        phone_code_hash=code_hash,
    )
    logger.info("已为账号 {} 发送登录验证码", account.name)
    return {
        "status": "code_sent",
        "phone_masked": account.phone_masked,
        "timeout": getattr(result, "timeout", None),
        "hint": "验证码会发到你的 Telegram App（不是短信），若没收到可点重新发送；"
        "开了两步验证的账号下一步还会要求输入密码。",
    }


async def verify_code(
    session: AsyncSession,
    config: AppConfig,
    account: TgAccount,
    *,
    code: str,
    client_factory: Any = None,
) -> dict[str, Any]:
    """第二步：提交验证码；需要两步验证时返回 password_required。"""
    item = _require_pending(account.id)
    text = (code or "").strip().replace(" ", "").replace("-", "")
    if not text:
        raise ValidationFailedError("请输入验证码")

    try:
        await item.client.sign_in(
            phone=item.phone,
            code=text,
            phone_code_hash=item.phone_code_hash,
        )
    except Exception as exc:  # noqa: BLE001
        if _is_password_needed(exc):
            item.stage = "password_required"
            return {
                "status": "password_required",
                "hint": "该账号开启了两步验证，请输入你的两步验证密码。",
            }
        raise ValidationFailedError(_friendly_error(exc)) from exc

    return await _finish(session, account, item)


async def submit_password(
    session: AsyncSession,
    config: AppConfig,
    account: TgAccount,
    *,
    password: str,
) -> dict[str, Any]:
    """第三步：提交两步验证密码。"""
    item = _require_pending(account.id)
    if not password:
        raise ValidationFailedError("请输入两步验证密码")
    try:
        await item.client.sign_in(password=password)
    except Exception as exc:  # noqa: BLE001
        raise ValidationFailedError(_friendly_error(exc)) from exc
    return await _finish(session, account, item)


async def cancel_login(account_id: int) -> bool:
    """取消未完成的登录并断开连接。"""
    item = _PENDING.pop(account_id, None)
    if item is None:
        return False
    with contextlib.suppress(Exception):
        await item.client.disconnect()
    return True


def _require_pending(account_id: int) -> PendingLogin:
    _sweep()
    item = _PENDING.get(account_id)
    if item is None:
        raise ValidationFailedError("登录会话已过期，请重新点击「发送验证码」")
    return item


async def _finish(
    session: AsyncSession,
    account: TgAccount,
    item: PendingLogin,
) -> dict[str, Any]:
    """登录成功：写入资料、更新状态并清理会话。"""
    try:
        profile = await fetch_account_profile(item.client)
    except Exception as exc:  # noqa: BLE001
        raise ValidationFailedError(f"读取账号资料失败：{_friendly_error(exc)}") from exc
    finally:
        _PENDING.pop(account.id, None)
        with contextlib.suppress(Exception):
            await item.client.disconnect()

    await tg_account_service.mark_login_success(session, account, profile=profile)
    logger.info("账号 {} 登录成功（@{}）", account.name, profile.username or "-")
    return {
        "status": "active",
        "username": profile.username,
        "tg_user_id": profile.tg_user_id,
        "display_name": profile.display_name,
    }


async def _open_client(
    config: AppConfig,
    *,
    api_id: int,
    api_hash: str,
    session_path: Any,
    client_factory: Any = None,
) -> Any:
    if client_factory is not None:
        client = await client_factory(
            config,
            api_id=api_id,
            api_hash=api_hash,
            session_path=session_path,
        )
        with contextlib.suppress(Exception):
            await client.connect()
        return client

    client = build_user_client(
        config,
        api_id=api_id,
        api_hash=api_hash,
        session_path=session_path,
    )
    await client.connect()
    return client


def _sweep() -> None:
    """清理超时的登录会话（只断开连接，不阻塞调用方）。"""
    for account_id in [key for key, item in _PENDING.items() if item.expired]:
        item = _PENDING.pop(account_id, None)
        if item is None:
            continue
        logger.warning("账号 {} 的登录会话已超时，已断开连接", item.account_id)
        import asyncio

        with contextlib.suppress(RuntimeError, Exception):
            asyncio.get_running_loop().create_task(item.client.disconnect())


def _is_password_needed(exc: BaseException) -> bool:
    """判断是否"需要两步验证密码"（测试替身可用同名异常）。"""
    try:
        from telethon.errors import SessionPasswordNeededError

        if isinstance(exc, SessionPasswordNeededError):
            return True
    except ImportError:  # pragma: no cover - telethon 缺失时退化为按类名判断
        pass
    return exc.__class__.__name__ == "SessionPasswordNeededError"


def _friendly_error(exc: BaseException) -> str:
    """把 Telegram 的异常翻译成能直接展示的中文提示。"""
    name = exc.__class__.__name__
    mapping = {
        "PhoneCodeInvalidError": "验证码不正确，请重新输入",
        "PhoneCodeExpiredError": "验证码已过期，请重新发送",
        "PhoneCodeEmptyError": "请输入验证码",
        "PhoneNumberInvalidError": "手机号格式不正确，请检查区号",
        "PhoneNumberBannedError": "该手机号已被 Telegram 封禁",
        "PasswordHashInvalidError": "两步验证密码错误",
        "SessionPasswordNeededError": "该账号开启了两步验证，请输入密码",
        "AuthKeyUnregisteredError": "登录态已失效，请重新登录",
        "ApiIdInvalidError": "API ID / API Hash 无效，请检查 .env 配置",
        "FloodWaitError": "请求过于频繁，请稍后再试",
    }
    if name in mapping:
        wait = getattr(exc, "seconds", None)
        if name == "FloodWaitError" and wait:
            return f"请求过于频繁，请等待 {wait} 秒后再试"
        return mapping[name]
    return f"登录失败：{exc}"
