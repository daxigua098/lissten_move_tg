"""HTTP 层的错误响应：领域异常在此统一注册，并对外重新导出。"""

from __future__ import annotations

from typing import Any

from loguru import logger

from app.core.errors import (
    AccountLockedError,
    AppError,
    AuthRequiredError,
    BuiltinPasswordEnvError,
    ConflictError,
    InvalidCredentialsError,
    LastSuperAdminError,
    NotFoundError,
    PasswordChangeRequiredError,
    PasswordWeakError,
    PermissionDeniedError,
    RateLimitedError,
    SelfOperationError,
    UserExistsError,
    ValidationFailedError,
)

__all__ = [
    "AccountLockedError",
    "AppError",
    "AuthRequiredError",
    "BuiltinPasswordEnvError",
    "ConflictError",
    "InvalidCredentialsError",
    "LastSuperAdminError",
    "NotFoundError",
    "PasswordChangeRequiredError",
    "PasswordWeakError",
    "PermissionDeniedError",
    "RateLimitedError",
    "SelfOperationError",
    "UserExistsError",
    "ValidationFailedError",
    "register_exception_handlers",
]

# 请求体字段名 → 界面上的中文说法，用于把 FastAPI 的原生英文报错翻译成人话
FIELD_LABELS = {
    "name": "名称",
    "username": "账号",
    "password": "密码",
    "new_password": "新密码",
    "current_password": "当前密码",
    "source_chat_id": "监听源",
    "source_chat_ids": "监听源",
    "target_chat_ids": "接收目标",
    "chat_ids": "群组",
    "inputs": "输入的链接",
    "business_type": "业务类型",
    "role": "用途",
    "enabled": "启用状态",
    "delay_seconds": "投递间隔",
    "priority": "优先级",
    "hourly_limit": "每小时上限",
    "daily_limit": "每日上限",
    "history_limit": "历史补齐条数",
    "ad_policy": "广告策略",
    "ad_asset_id": "广告素材",
    "content_types": "搬运内容类型",
    "keyword_group_ids": "关键词组",
    "title": "标题",
    "token": "Bot Token",
    "api_id": "API ID",
    "api_hash": "API Hash",
}

# pydantic 的原生英文提示 → 中文说法（前缀匹配）
MESSAGE_LABELS = (
    ("String should have at least 1 character", "不能为空"),
    ("String should have at most", "过长"),
    ("Field required", "必填"),
    ("Input should be a valid integer", "必须是整数"),
    ("Input should be a valid number", "必须是数字"),
    ("Input should be a valid string", "必须是文本"),
    ("Input should be a valid boolean", "必须是「是 / 否」"),
    ("Input should be 'A' or 'B'", "只能是 A 或 B"),
    ("Input should be greater than 0", "必须大于 0"),
    ("Input should be greater than or equal to", "取值太小"),
    ("Input should be less than or equal to", "取值太大"),
    ("List should have at least 1 item", "至少要选 1 项"),
)


def _field_label(location: str) -> str:
    """把 ``body.name`` 之类的定位转成界面上的字段名。"""
    parts = [part for part in location.split(".") if part]
    if not parts:
        return "请求体"
    field = parts[-1]
    label = FIELD_LABELS.get(field, field)
    if parts[0] == "body" and len(parts) == 2 and field not in FIELD_LABELS:
        return f"字段 {field}"
    return label


def _humanize_issue(location: str, message: str) -> str:
    """把校验错误拼成「线路名不能为空」这种人话。"""
    label = _field_label(location)
    for prefix, text in MESSAGE_LABELS:
        if message.startswith(prefix):
            return f"{label}{text}"
    return f"{label}：{message}"


def register_exception_handlers(app: Any) -> None:
    """把领域异常注册为统一 JSON 错误响应。"""
    from fastapi import Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse

    @app.exception_handler(AppError)
    async def _handle_app_error(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.payload())

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        """把 FastAPI 原生校验错误也统一成 {detail, code} 结构。"""
        issues = exc.errors()
        first = issues[0] if issues else {}
        location = ".".join(str(part) for part in first.get("loc", ())) or "请求体"
        message = _humanize_issue(location, str(first.get("msg", "取值非法")))
        return JSONResponse(
            status_code=422,
            content={
                "detail": f"参数校验失败：{message}".strip(),
                "code": "VALIDATION_ERROR",
                "issues": [
                    {
                        "location": ".".join(str(part) for part in item.get("loc", ())),
                        "message": str(item.get("msg", "")),
                    }
                    for item in issues
                ],
            },
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_request: Request, exc: Exception) -> JSONResponse:
        logger.exception("未处理的异常：{}", exc)
        return JSONResponse(
            status_code=500,
            content={"detail": "服务内部错误", "code": "INTERNAL_ERROR"},
        )
