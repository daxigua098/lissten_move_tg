"""执行账号与控制 Bot 的请求模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AccountStatusLiteral = Literal["pending_login", "active", "restricted", "disabled"]


class AccountCreateRequest(BaseModel):
    """登记执行账号。"""

    name: str = Field(min_length=1, max_length=64)
    phone: str = Field(min_length=6, max_length=32)
    # 留空则使用 .env 里的 TG_API_ID / TG_API_HASH
    api_id: int | None = Field(default=None, gt=0)
    api_hash: str | None = Field(default=None, max_length=128)
    session_name: str | None = Field(default=None, max_length=128)
    is_default: bool = False
    note: str | None = Field(default=None, max_length=255)


class AccountUpdateRequest(BaseModel):
    """更新执行账号（字段均可选）。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    phone: str | None = Field(default=None, max_length=32)
    api_id: int | None = Field(default=None, gt=0)
    api_hash: str | None = Field(default=None, max_length=128)
    status: AccountStatusLiteral | None = None
    is_default: bool | None = None
    note: str | None = Field(default=None, max_length=255)


class BotCreateRequest(BaseModel):
    """绑定控制 Bot。"""

    name: str = Field(min_length=1, max_length=64)
    token: str = Field(min_length=20, max_length=128)
    admin_ids: list[int] = Field(default_factory=list)
    is_default: bool = False
    note: str | None = Field(default=None, max_length=255)


class BotUpdateRequest(BaseModel):
    """更新控制 Bot（字段均可选）。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    token: str | None = Field(default=None, min_length=20, max_length=128)
    admin_ids: list[int] | None = None
    is_default: bool | None = None
    enabled: bool | None = None
    note: str | None = Field(default=None, max_length=255)
