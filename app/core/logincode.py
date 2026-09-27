"""接码平台（logincode）客户端：读登录验证码与二级密码。

协议与参考项目 TG登录发信息 对齐：

1. 取码地址形如 ``https://host/?token=<uuid>``；
2. ``GET {origin}/config.js?t=188888888`` 里取出 ``VITE_API_BASE_URL``；
3. 接口基址为 ``{VITE_API_BASE_URL}/verification``；
4. ``GET {base}/getMsg/{token}`` 读当前结果，``GET {base}/getCode/{token}`` 请求刷新；
5. 结果里 ``data.loginCode`` 是登录验证码，``data.password`` 是二级密码。
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.core.errors import ValidationFailedError

CONFIG_URL = "{origin}/config.js?t=188888888"
API_BASE_PATTERN = re.compile(r"VITE_API_BASE_URL\s*[:=]\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
REQUEST_TIMEOUT_SECONDS = 20.0
REFRESH_TIMEOUT_SECONDS = 60.0
DEFAULT_WAIT_SECONDS = 300.0
DEFAULT_POLL_SECONDS = 5.0
DEFAULT_FIRST_DELAY_SECONDS = 10.0

# (url, *, timeout) -> (http_status, body)
HttpGet = Callable[..., Awaitable[tuple[int, str]]]


class WaitCancelled(RuntimeError):
    """等待过程被人工取消。"""


class LogincodeCooldown(ValidationFailedError):
    """接码平台提示：30 分钟内没有新的登录验证码（该账号先挂起）。"""

    code = "LOGINCODE_COOLDOWN"


@dataclass(frozen=True)
class LogincodeService:
    """一个取码地址解析出来的接口信息。"""

    token: str
    api_base: str


async def default_http_get(
    url: str, *, timeout: float = REQUEST_TIMEOUT_SECONDS
) -> tuple[int, str]:
    """默认用 httpx 取内容；测试可注入替身。"""
    import httpx

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(
            url,
            headers={"Content-Type": "application/json;charset=UTF-8"},
        )
        return response.status_code, response.text


def parse_login_url(login_url: str) -> tuple[str, str]:
    """解析取码地址，返回 ``(origin, token)``。"""
    parsed = urlparse((login_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValidationFailedError("接码地址必须是 http/https 链接")
    token = (parse_qs(parsed.query).get("token") or [""])[0].strip()
    if not token:
        raise ValidationFailedError("接码地址缺少 token 参数")
    return f"{parsed.scheme}://{parsed.netloc}", token


async def resolve_service(
    login_url: str,
    *,
    http_get: HttpGet | None = None,
) -> LogincodeService:
    """按取码地址读出真正的接口基址。"""
    fetch = http_get or default_http_get
    origin, token = parse_login_url(login_url)
    status, text = await fetch(CONFIG_URL.format(origin=origin), timeout=REQUEST_TIMEOUT_SECONDS)
    match = API_BASE_PATTERN.search(text or "")
    if status >= 400 or not match:
        raise ValidationFailedError("无法从接码地址读取接口配置（config.js）")
    return LogincodeService(token=token, api_base=f"{match.group(1).rstrip('/')}/verification")


def normalize(payload: Any) -> dict[str, Any]:
    """把接码接口返回整理成固定字段。"""
    root = payload if isinstance(payload, dict) else {}
    data = root.get("data") if isinstance(root.get("data"), dict) else root

    def text(key: str) -> str:
        return str(data.get(key) or "").strip()

    return {
        "status": str(root.get("status") or "").lower(),
        "login_code": text("loginCode"),
        "password": text("password"),
        "account": text("account").replace("_", ""),
        "success_times": int(data.get("successTimes") or 0),
        "message": str(root.get("message") or "").strip(),
        "limit_seconds": int(root.get("limitTm") or 0),
    }


async def _call(
    service: LogincodeService,
    path: str,
    *,
    http_get: HttpGet | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    fetch = http_get or default_http_get
    url = f"{service.api_base}/{path}/{service.token}"
    status, text = await fetch(url, timeout=timeout)
    try:
        payload = json.loads(text or "null")
    except json.JSONDecodeError as exc:
        raise ValidationFailedError(f"接码接口返回了非 JSON 内容（HTTP {status}）") from exc
    if status >= 400 or not isinstance(payload, dict) or int(payload.get("code") or 0) != 200:
        message = str(payload.get("msg") or "") if isinstance(payload, dict) else ""
        raise ValidationFailedError(message or f"接码接口请求失败（HTTP {status}）")
    return payload


async def snapshot(service: LogincodeService, *, http_get: HttpGet | None = None) -> dict[str, Any]:
    """读一次当前接码结果。"""
    return normalize(await _call(service, "getMsg", http_get=http_get))


async def request_refresh(service: LogincodeService, *, http_get: HttpGet | None = None) -> None:
    """请求接码平台刷新一次（有些实现会阻塞到拿到短信）。"""
    await _call(
        service,
        "getCode",
        http_get=http_get,
        timeout=REFRESH_TIMEOUT_SECONDS,
    )


async def wait_for_code(
    service: LogincodeService,
    *,
    http_get: HttpGet | None = None,
    timeout: float = DEFAULT_WAIT_SECONDS,
    poll: float = DEFAULT_POLL_SECONDS,
    first_delay: float = DEFAULT_FIRST_DELAY_SECONDS,
    should_stop: Callable[[], bool] | None = None,
    on_step: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """等接码平台返回新的登录验证码（顺带拿到二级密码）。"""

    async def pause(seconds: float) -> None:
        if should_stop is not None and should_stop():
            raise WaitCancelled("任务已停止")
        await asyncio.sleep(seconds)

    if on_step is not None:
        on_step("Telegram 已请求验证码，稍候读取接码平台")
    await pause(first_delay)
    try:
        await request_refresh(service, http_get=http_get)
    except ValidationFailedError:
        # getCode 可能本身就在等短信，失败也继续读结果
        pass

    waited = 0.0
    while waited < timeout:
        if should_stop is not None and should_stop():
            raise WaitCancelled("任务已停止")
        result = await snapshot(service, http_get=http_get)
        if result["login_code"]:
            return result
        if result["status"] == "invalid":
            raise ValidationFailedError("接码链接已失效（账号可能已被封禁）")
        if result["status"] == "failed":
            raise LogincodeCooldown("接码平台提示：30 分钟内没有新的登录验证码，请稍后重试")
        if on_step is not None and int(waited) % 15 == 0:
            on_step(f"等待接码平台返回验证码（已等待 {int(waited)} 秒）")
        await pause(poll)
        waited += poll
    raise ValidationFailedError("等待接码平台超时，没有拿到登录验证码")
