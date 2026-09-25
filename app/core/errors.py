"""领域异常：带 HTTP 状态码与机器可读错误码。

放在 core 层，services 可以放心抛出，不必依赖 HTTP 层。
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """业务异常基类。"""

    status_code = 500
    code = "INTERNAL_ERROR"
    default_detail = "服务内部错误"

    def __init__(
        self,
        detail: str | None = None,
        *,
        code: str | None = None,
        status_code: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.detail = detail or self.default_detail
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.extra = dict(extra or {})
        super().__init__(self.detail)

    def payload(self) -> dict[str, Any]:
        """构造错误响应体。"""
        body: dict[str, Any] = {"detail": self.detail, "code": self.code}
        body.update(self.extra)
        return body


class ValidationFailedError(AppError):
    """请求参数不合法。"""

    status_code = 400
    code = "VALIDATION_ERROR"
    default_detail = "请求参数不合法"


class PasswordWeakError(AppError):
    """新密码不满足强度要求。"""

    status_code = 400
    code = "AUTH_PASSWORD_WEAK"
    default_detail = "新密码强度不足"


class BuiltinPasswordEnvError(AppError):
    """内置管理员密码只能在服务器 .env 修改。"""

    status_code = 400
    code = "AUTH_BUILTIN_PASSWORD_ENV"
    default_detail = "内置管理员密码需在服务器 .env 中修改"


class AuthRequiredError(AppError):
    """未认证或令牌已失效。"""

    status_code = 401
    code = "AUTH_REQUIRED"
    default_detail = "未认证或登录已过期"


class InvalidCredentialsError(AppError):
    """用户名或密码错误。"""

    status_code = 401
    code = "AUTH_INVALID_CREDENTIALS"
    default_detail = "用户名或密码错误"


class PasswordChangeRequiredError(AppError):
    """必须先修改默认密码。"""

    status_code = 403
    code = "AUTH_PASSWORD_CHANGE_REQUIRED"
    default_detail = "请先修改默认密码"


class PermissionDeniedError(AppError):
    """角色权限不足。"""

    status_code = 403
    code = "PERMISSION_DENIED"
    default_detail = "当前角色无此操作权限"


class NotFoundError(AppError):
    """资源不存在。"""

    status_code = 404
    code = "NOT_FOUND"
    default_detail = "资源不存在"


class ConflictError(AppError):
    """资源状态冲突。"""

    status_code = 409
    code = "CONFLICT"
    default_detail = "资源状态冲突"


class UserExistsError(ConflictError):
    """用户名已存在。"""

    code = "USER_EXISTS"
    default_detail = "用户名已存在"


class LastSuperAdminError(ConflictError):
    """不能删除或降级最后一个超级管理员。"""

    code = "USER_LAST_SUPER_ADMIN"
    default_detail = "系统必须保留至少一个启用的超级管理员"


class SelfOperationError(ConflictError):
    """不能对自己执行该操作。"""

    code = "USER_SELF_DELETE"
    default_detail = "不能对自己执行该操作"


class AccountLockedError(AppError):
    """账号因失败次数过多被锁定。"""

    status_code = 423
    code = "AUTH_LOCKED"
    default_detail = "账号已被锁定，请稍后再试"


class RateLimitedError(AppError):
    """触发限流。"""

    status_code = 429
    code = "RATE_LIMITED"
    default_detail = "请求过于频繁，请稍后再试"
