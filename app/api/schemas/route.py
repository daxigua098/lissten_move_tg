"""线路与广告素材的请求模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

BusinessLiteral = Literal["A", "B"]
SenderModeLiteral = Literal["account", "bot"]


class RouteCreateRequest(BaseModel):
    """创建线路。"""

    name: str = Field(min_length=1, max_length=128)
    source_chat_id: int | None = Field(default=None, gt=0)
    # 多选监听源：给这个就按「一个源一条线路」批量建，共用 bundle
    source_chat_ids: list[int] | None = None
    business_type: BusinessLiteral
    target_chat_ids: list[int] = Field(min_length=1)
    exec_account_id: int | None = None
    notify_bot_id: int | None = None
    sender_mode: SenderModeLiteral | None = None
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
    sender_mode: SenderModeLiteral | None = None
    delay_seconds: float = Field(default=1.0, ge=0.2, le=600)
    a_config: dict[str, Any] | None = None
    b_config: dict[str, Any] | None = None


class RouteUpdateRequest(BaseModel):
    """更新线路（字段均可选）。"""

    name: str | None = Field(default=None, min_length=1, max_length=128)
    source_chat_ids: list[int] | None = None
    # 列表页按「线路」操作时置 true：同一组的其他行一起改（多源线路共用一套配置）
    apply_to_bundle: bool = False
    business_type: BusinessLiteral | None = None
    exec_account_id: int | None = None
    notify_bot_id: int | None = None
    sender_mode: SenderModeLiteral | None = None
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
