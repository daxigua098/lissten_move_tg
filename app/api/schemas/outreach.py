"""冷触达请求模型：租户策略与话术模板。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TemplateKindLiteral = Literal["first_contact", "follow_up", "auto_reply"]
ReplyModeLiteral = Literal["human", "auto"]


class OutreachSettingsUpdate(BaseModel):
    """租户级冷触达策略（字段均可选）。"""

    default_cooldown_seconds: int | None = Field(default=None, ge=0, le=24 * 3600)
    cross_account_lock_days: int | None = Field(default=None, ge=0, le=3650)
    strict_permanent_lock: bool | None = None
    follow_up_days: int | None = Field(default=None, ge=0, le=365)
    follow_up_max: int | None = Field(default=None, ge=0, le=5)
    reply_mode: ReplyModeLiteral | None = None
    auto_reply_enabled: bool | None = None
    auto_reply_max_rounds: int | None = Field(default=None, ge=0, le=20)
    auto_reply_template_id: int | None = Field(default=None, ge=1)
    handoff_bot_enabled: bool | None = None
    handoff_bot_id: int | None = Field(default=None, ge=1)
    working_hours: list[str] | None = None
    daily_pool_cap: int | None = Field(default=None, ge=1, le=100000)
    kill_switch: bool | None = None
    delete_session_on_account_delete: bool | None = None


class OutreachTemplateCreate(BaseModel):
    """新建会员自己的话术模板。"""

    name: str = Field(min_length=1, max_length=64)
    kind: TemplateKindLiteral
    text: str = Field(min_length=1, max_length=4000)
    variables: list[str] = Field(default_factory=list)


class OutreachTemplateUpdate(BaseModel):
    """修改会员自己的话术模板。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    kind: TemplateKindLiteral | None = None
    text: str | None = Field(default=None, min_length=1, max_length=4000)
    enabled: bool | None = None
    variables: list[str] | None = None


class HandoffConsumeRequest(BaseModel):
    """用户按下 Bot 的 Start 后回传的令牌。"""

    token: str = Field(min_length=8, max_length=64)
    tg_user_id: int | None = Field(default=None, gt=0)
