"""源与接收组的请求模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TargetRoleLiteral = Literal["content", "lead"]
TagModeLiteral = Literal["add", "replace", "remove"]


class SourceAddRequest(BaseModel):
    """添加监听源：勾选池内群组 + 手动粘贴链接。"""

    chat_ids: list[int] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    join: bool = False
    account_id: int | None = None


class TargetAddRequest(BaseModel):
    """添加接收组。"""

    chat_ids: list[int] = Field(default_factory=list)
    inputs: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    role: TargetRoleLiteral = "content"
    join: bool = False
    account_id: int | None = None
    check_access: bool = True


class SourceUpdateRequest(BaseModel):
    """更新监听源。"""

    display_name: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None
    tags: list[str] | None = None
    note: str | None = Field(default=None, max_length=255)


class TargetUpdateRequest(BaseModel):
    """更新接收组。"""

    display_name: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None
    role: TargetRoleLiteral | None = None
    tags: list[str] | None = None
    note: str | None = Field(default=None, max_length=255)


class TagBatchRequest(BaseModel):
    """批量打标签。"""

    chat_ids: list[int] = Field(min_length=1)
    tags: list[str] = Field(min_length=1)
    mode: TagModeLiteral = "add"
