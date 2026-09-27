"""发信息账号自动登录：从接码平台取验证码与二级密码，自动完成登录。

流程：解析取码地址 → 让 Telegram 发验证码 → 等接码平台返回 loginCode（含 2FA 密码）
→ 提交验证码 → 需要时提交二级密码。

一个账号一个后台任务，状态放内存供界面轮询；某个账号失败不影响其他账号。
"""

from __future__ import annotations

import asyncio
from typing import Any

from loguru import logger

from app.core import logincode
from app.core.config import AppConfig
from app.core.errors import ValidationFailedError
from app.core.logincode import WaitCancelled, resolve_service, wait_for_code
from app.db.base import as_utc, utc_now
from app.db.models import TgAccount
from app.db.session import session_scope
from app.services import tg_account_service, tg_login_service

# 等接码平台的节奏（测试可改小）
WAIT_TIMEOUT_SECONDS = logincode.DEFAULT_WAIT_SECONDS
FIRST_DELAY_SECONDS = logincode.DEFAULT_FIRST_DELAY_SECONDS
POLL_SECONDS = logincode.DEFAULT_POLL_SECONDS

STAGE_LABELS: dict[str, str] = {
    "resolving": "解析接码地址",
    "sending": "发送验证码",
    "waiting_code": "等待接码平台",
    "verifying": "提交验证码",
    "success": "登录成功",
    "failed": "失败",
    "stopped": "已停止",
}

_STATES: dict[int, dict[str, Any]] = {}
_TASKS: dict[int, asyncio.Task] = {}
_STOP: set[int] = set()


def snapshot() -> list[dict[str, Any]]:
    """当前所有自动登录任务的状态。"""
    return list(_STATES.values())


def is_running(account_id: int) -> bool:
    task = _TASKS.get(account_id)
    return task is not None and not task.done()


def _set(
    account_id: int,
    *,
    stage: str,
    message: str = "",
    status: str = "pending",
    name: str = "",
    phone_masked: str = "",
) -> None:
    previous = _STATES.get(account_id, {})
    _STATES[account_id] = {
        "account_id": account_id,
        "name": name or previous.get("name", ""),
        "phone_masked": phone_masked or previous.get("phone_masked", ""),
        "stage": stage,
        "stage_label": STAGE_LABELS.get(stage, stage),
        "message": message,
        "status": status,
        "updated_at": utc_now(),
    }


async def start(
    config: AppConfig,
    account_ids: list[int],
    *,
    http_get: Any = None,
    client_factory: Any = None,
) -> dict[str, Any]:
    """为这些账号启动自动登录任务。"""
    started: list[int] = []
    skipped: list[int] = []
    for account_id in account_ids:
        if is_running(account_id):
            skipped.append(account_id)
            continue
        _STOP.discard(account_id)
        _set(account_id, stage="resolving", message="准备开始")
        _TASKS[account_id] = asyncio.create_task(
            _run(config, account_id, http_get=http_get, client_factory=client_factory)
        )
        started.append(account_id)
    return {"started": started, "skipped": skipped}


def stop(account_ids: list[int] | None = None) -> int:
    """请求停止（协作式 + 取消任务）。"""
    targets = list(_STATES) if not account_ids else list(account_ids)
    stopped = 0
    for account_id in targets:
        _STOP.add(account_id)
        task = _TASKS.get(account_id)
        if task is not None and not task.done():
            task.cancel()
            stopped += 1
    return stopped


async def _run(
    config: AppConfig,
    account_id: int,
    *,
    http_get: Any,
    client_factory: Any,
) -> None:
    name = ""
    phone_masked = ""
    try:
        async with session_scope() as session:
            account = await session.get(TgAccount, account_id)
            if account is None:
                raise ValidationFailedError("账号不存在")
            name, phone_masked = account.name, account.phone_masked
            code_url = tg_account_service.decrypt_code_url(config, account)
        _set(account_id, stage="resolving", name=name, phone_masked=phone_masked)
        if not code_url:
            raise ValidationFailedError("该账号没有接码地址，请用「手机号 + 接码地址」导入")

        service = await resolve_service(code_url, http_get=http_get)

        _set(account_id, stage="sending", message="正在让 Telegram 发送验证码")
        async with session_scope() as session:
            account = await session.get(TgAccount, account_id)
            await tg_login_service.start_login(
                config,
                account,
                client_factory=client_factory,
            )

        _set(account_id, stage="waiting_code", message="等待接码平台返回验证码")
        verification = await wait_for_code(
            service,
            http_get=http_get,
            timeout=WAIT_TIMEOUT_SECONDS,
            poll=POLL_SECONDS,
            first_delay=FIRST_DELAY_SECONDS,
            should_stop=lambda: account_id in _STOP,
            on_step=lambda text: _set(account_id, stage="waiting_code", message=text),
        )

        _set(account_id, stage="verifying", message="正在提交验证码")
        async with session_scope() as session:
            account = await session.get(TgAccount, account_id)
            result = await tg_login_service.verify_code(
                session,
                config,
                account,
                code=verification["login_code"],
            )

        if result.get("status") == "password_required":
            password = verification["password"]
            if not password:
                raise ValidationFailedError("接码地址没有返回二级密码，请手动登录补输密码")
            _set(account_id, stage="verifying", message="正在提交二级密码")
            async with session_scope() as session:
                account = await session.get(TgAccount, account_id)
                result = await tg_login_service.submit_password(
                    session,
                    config,
                    account,
                    password=password,
                )

        _set(
            account_id,
            stage="success",
            status="success",
            message=f"登录成功（@{result.get('username') or '-'}）",
        )
        logger.info("自动登录成功：{}", name)
    except asyncio.CancelledError:
        await _cleanup(account_id)
        _set(account_id, stage="stopped", status="stopped", message="已停止")
        raise
    except WaitCancelled as exc:
        await _cleanup(account_id)
        _set(account_id, stage="stopped", status="stopped", message=str(exc))
    except Exception as exc:  # noqa: BLE001 - 失败原因要显示给操作者
        await _cleanup(account_id)
        _set(account_id, stage="failed", status="failed", message=str(exc)[:300])
        logger.warning("自动登录失败（{}）：{}", name or account_id, exc)
    finally:
        _STOP.discard(account_id)


async def _cleanup(account_id: int) -> None:
    """取消未完成的登录并断开连接，避免留下悬挂客户端。"""
    try:
        await tg_login_service.cancel_login(account_id)
    except Exception:  # noqa: BLE001 - 清理失败不影响结果上报
        logger.debug("清理登录会话失败：{}", account_id)


def to_payload() -> list[dict[str, Any]]:
    """给接口用的状态（时间转 ISO）。"""
    return [{**item, "updated_at": as_utc(item.get("updated_at"))} for item in _STATES.values()]
