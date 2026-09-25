"""线路与广告素材的请求模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

BusinessLiteral = Literal["A", "B"]


class RouteCreateRequest(BaseModel):
    """创建线路。"""

    name: str = Field(min_length=1, max_length=128)
    source_chat_id: int = Field(gt=0)
    business_type: BusinessLiteral
    target_chat_ids: list[int] = Field(min_length=1)
    exec_account_id: int | None = None
    notify_bot_id: int | None = None
    priority: int = Field(default=100, ge=0, le=10000)
    delay_seconds: float = Field(default=1.0, ge=0.2, le=600)
    hourly_limit: int | None = Field(default=None, ge=1)
    daily_limit: int | None = Field(default=None, ge=1)
    enabled: bool = True
    a_config: dict[str, Any] | None = None
    b_config: dict[str, Any] | None = None


class RouteMatrixRequest(BaseModel):
    """矩阵建线：多个源 × 多个目标。"""

    source_chat_ids: list[int] = Field(min_length=1)
    target_chat_ids: list[int] = Field(min_length=1)
    business_type: BusinessLiteral = "A"
    exec_account_id: int | None = None
    notify_bot_id: int | None = None
    delay_seconds: float = Field(default=1.0, ge=0.2, le=600)
    a_config: dict[str, Any] | None = None
    b_config: dict[str, Any] | None = None


class RouteUpdateRequest(BaseModel):
    """更新线路（字段均可选）。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    business_type: BusinessLiteral | None = None
    exec_account_id: int | None = None
    notify_bot_id: int | None = None
    priority: int | None = Field(default=None, ge=0, le=10000)
    delay_seconds: float | None = Field(default=None, ge=0.2, le=600)
    hourly_limit: int | None = Field(default=None, ge=1)
    daily_limit: int | None = Field(default=None, ge=1)
    enabled: bool | None = None
    a_config: dict[str, Any] | None = None
    b_config: dict[str, Any] | None = None


class RouteTargetAddRequest(BaseModel):
    """给线路追加接收目标。"""

    chat_ids: list[int] = Field(min_length=1)


class EnabledUpdate(BaseModel):
    """启停开关。"""

    enabled: bool


class ResetProgressRequest(BaseModel):
    """重置目标进度需要显式确认。"""

    confirm: str = Field(min_length=1, max_length=16)


class AdAssetCreateRequest(BaseModel):
    """新建广告素材。"""

    name: str = Field(min_length=1, max_length=64)
    text: str = Field(default="", max_length=4096)
    image_path: str | None = Field(default=None, max_length=255)
    link_url: str | None = Field(default=None, max_length=512)
    link_text: str | None = Field(default=None, max_length=64)
    enabled: bool = True


class AdAssetUpdateRequest(BaseModel):
    """更新广告素材。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    text: str | None = Field(default=None, max_length=4096)
    image_path: str | None = Field(default=None, max_length=255)
    link_url: str | None = Field(default=None, max_length=512)
    link_text: str | None = Field(default=None, max_length=64)
    enabled: bool | None = None
