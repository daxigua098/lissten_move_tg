"""代理工作台请求模型（P3-06）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

QuotaType = Literal["member", "agent", "trial"]


class MemberOpenRequest(BaseModel):
    """开正式会员。"""

    username: str = Field(min_length=3, max_length=64)
    days: int = Field(ge=1, le=3650)
    password: str | None = Field(default=None, max_length=256)
    display_name: str | None = Field(default=None, max_length=64)
    modules: list[str] | None = None
    template_code: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=255)


class TrialOpenRequest(BaseModel):
    """开 1 天试用账号（天数固定，不收）。"""

    username: str = Field(min_length=3, max_length=64)
    template_code: str | None = Field(default=None, max_length=32)
    password: str | None = Field(default=None, max_length=256)
    display_name: str | None = Field(default=None, max_length=64)
    note: str | None = Field(default=None, max_length=255)


class AgentOpenRequest(BaseModel):
    """开下级代理（可顺手划拨额度）。"""

    username: str = Field(min_length=3, max_length=64)
    password: str | None = Field(default=None, max_length=256)
    display_name: str | None = Field(default=None, max_length=64)
    allocate: dict[QuotaType, int] | None = None
    note: str | None = Field(default=None, max_length=255)


class AllocateRequest(BaseModel):
    """划拨额度给直属下级。"""

    target_user_id: int
    quota_type: QuotaType
    count: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=255)


class ReclaimRequest(BaseModel):
    """回收直属下级的未使用额度。"""

    target_user_id: int
    quota_type: QuotaType
    count: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=255)


class RenewRequest(BaseModel):
    """续期。"""

    days: int = Field(ge=1, le=3650)
    modules: list[str] | None = None
    template_code: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=255)


class UpgradeTrialRequest(BaseModel):
    """试用转正式。"""

    days: int = Field(ge=1, le=3650)
    modules: list[str] | None = None
    template_code: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=255)


class EnableRequest(BaseModel):
    """停用 / 解停直属下级。"""

    enabled: bool
    reason: str | None = Field(default=None, max_length=255)


class AdjustRequest(BaseModel):
    """平台手工调账（必须填备注）。"""

    quota_type: QuotaType
    delta: int
    note: str = Field(min_length=1, max_length=255)
