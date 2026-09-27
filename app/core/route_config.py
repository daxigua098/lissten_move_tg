"""线路规则配置模型：A 线（搬运帖子）与 B 线（监听会员）。

配置以 JSON 存在 routes.a_config / b_config，读写都经过这里校验，
界面只提交关心的字段，其余走默认值。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

from app.core.errors import ValidationFailedError
from app.db.models import BUSINESS_CARRY, BUSINESS_TYPES

ContentType = Literal["text", "photo", "video", "document", "poll"]


class ACarryConfig(BaseModel):
    """A 线：帖子搬运与内容净化。"""

    content_types: list[ContentType] = Field(default_factory=lambda: ["text", "photo", "video"])
    strip_url: bool = True
    strip_mention: bool = True
    strip_buttons: bool = True
    strip_phone: bool = False
    promo_blacklist: list[str] = Field(default_factory=list)
    promo_regex: list[str] = Field(default_factory=list)
    transfer_mode: Literal["copy", "forward"] = "copy"
    # 文案处理：keep=保持原文直接转发（快）；clean=净化后重新上传（慢，但真正去掉对方广告）
    text_mode: Literal["keep", "clean"] = "keep"
    album_aggregate: bool = True
    empty_text_policy: Literal["keep_media", "drop"] = "keep_media"
    ad_policy: Literal["none", "every", "nth"] = "nth"
    ad_nth: int = Field(default=3, ge=1, le=1000)
    ad_asset_id: int | None = Field(default=None, ge=1)
    history_backfill: bool = True
    history_limit: int = Field(default=500, ge=1, le=10000)
    history_since_date: datetime | None = None
    backfill_rate_seconds: float = Field(default=2.0, ge=0.2, le=60)
    skip_pinned: bool = True

    @field_validator("content_types")
    @classmethod
    def _require_content_type(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("至少选择一种内容类型")
        return value


class BMonitorConfig(BaseModel):
    """B 线：会员监听与线索生成。"""

    listen_mode: Literal["all", "keyword"] = "all"
    # cold：关键词命中 + 潜在可触达路径即可成为冷私聊候选；
    # strict：还必须存在明确邀请、历史回复或会员授权。
    capture_mode: Literal["cold", "strict"] = "cold"
    keyword_group_ids: list[int] = Field(default_factory=list)
    sensitivity: Literal["loose", "standard", "strict"] = "loose"
    match_contains: bool = True
    match_fuzzy: bool = True
    match_semantic: bool = False
    exclude_keywords: list[str] = Field(default_factory=list)
    # 共享排除词库：多选引用多个排除词组，与本线路自定义排除词取并集
    exclude_group_ids: list[int] = Field(default_factory=list)
    sender_whitelist: list[int] = Field(default_factory=list)
    sender_blacklist: list[int] = Field(default_factory=list)
    skip_bots: bool = True
    skip_admins: bool = True
    min_text_length: int = Field(default=4, ge=0, le=200)
    capture_phone: bool = True
    capture_contact: bool = True
    lead_template: str = ""
    hit_cooldown_minutes: int = Field(default=10, ge=0, le=1440)
    push_card_on_all: bool = False


def load_a_config(raw: str | None) -> ACarryConfig:
    """读取 A 线配置；内容损坏时退回默认值，保证列表接口不整体失败。"""
    payload = _parse(raw)
    try:
        return ACarryConfig.model_validate(payload)
    except ValidationError:
        return ACarryConfig()


def load_b_config(raw: str | None) -> BMonitorConfig:
    """读取 B 线配置；内容损坏时退回默认值。"""
    payload = _parse(raw)
    try:
        return BMonitorConfig.model_validate(payload)
    except ValidationError:
        return BMonitorConfig()


def parse_a_config_payload(payload: dict[str, Any] | None) -> ACarryConfig:
    """校验界面提交的 A 线配置。"""
    try:
        return ACarryConfig.model_validate(payload or {})
    except ValidationError as exc:
        raise ValidationFailedError(_format(exc, "A 线配置")) from exc


def parse_b_config_payload(payload: dict[str, Any] | None) -> BMonitorConfig:
    """校验界面提交的 B 线配置。"""
    try:
        return BMonitorConfig.model_validate(payload or {})
    except ValidationError as exc:
        raise ValidationFailedError(_format(exc, "B 线配置")) from exc


def dump_config(model: BaseModel) -> str:
    """序列化为 JSON 字符串（保存前剔除未使用的另一业务线字段）。"""
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False)


def validate_route_config(
    business_type: str,
    a_config: ACarryConfig,
    b_config: BMonitorConfig,
) -> None:
    """跨字段校验：避免出现"选了但没内容"的配置。"""
    if business_type not in BUSINESS_TYPES:
        raise ValidationFailedError(f"业务类型必须是 {'/'.join(BUSINESS_TYPES)} 之一")

    if business_type == BUSINESS_CARRY:
        if a_config.ad_policy != "none" and a_config.ad_asset_id is None:
            raise ValidationFailedError("已选择按频率挂广告，请先选择广告素材")
    elif b_config.listen_mode == "keyword" and not b_config.keyword_group_ids:
        raise ValidationFailedError("关键词模式必须先选择或新建关键词组")


def _parse(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _format(error: ValidationError, label: str) -> str:
    parts = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        parts.append(f"{location}：{item.get('msg', '取值非法')}")
    return f"{label}校验失败：" + "；".join(parts)
