"""账号管理请求模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

RoleLiteral = Literal["viewer", "sub_admin", "super_admin"]


class UserCreateRequest(BaseModel):
    """新增子管理员。"""

    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=256)
    role: RoleLiteral = "viewer"
    display_name: str | None = Field(default=None, max_length=64)


class UserUpdateRequest(BaseModel):
    """修改子管理员（字段均可选）。"""

    role: RoleLiteral | None = None
    enabled: bool | None = None
    password: str | None = Field(default=None, max_length=256)
    display_name: str | None = Field(default=None, max_length=64)
