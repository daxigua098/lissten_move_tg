"""平台后台请求模型（P5）。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

QuotaType = Literal["member", "agent", "trial"]


class PlatformMemberOpenRequest(BaseModel):
    """平台开会员：可以指定这个号挂在哪个代理名下。"""

    username: str = Field(min_length=3, max_length=64)
    days: int = Field(ge=1, le=3650)
    password: str | None = Field(default=None, max_length=256)
    display_name: str | None = Field(default=None, max_length=64)
    modules: list[str] | None = None
    template_code: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=255)
    # 归属代理：填了就从他账上扣 1 个会员额度；不填就是平台直开，不占额度
    owner_agent_id: int | None = None


class PlatformRenewRequest(BaseModel):
    """平台续期 / 改功能包：只改到期日与授权，不自动恢复线路运行。"""

    days: int = Field(ge=1, le=3650)
    modules: list[str] | None = None
    template_code: str | None = Field(default=None, max_length=32)
    note: str | None = Field(default=None, max_length=255)


class PlatformPlanRequest(BaseModel):
    """改功能包：手勾功能块或选模板，立即生效。"""

    modules: list[str] | None = None
    template_code: str | None = Field(default=None, max_length=32)


class PlatformEnableRequest(BaseModel):
    """停用 / 解停任意账号。"""

    enabled: bool
    reason: str | None = Field(default=None, max_length=255)


class PlatformAdjustRequest(BaseModel):
    """平台手工调账（可正可负，必须填备注）。"""

    quota_type: QuotaType
    delta: int
    note: str = Field(min_length=1, max_length=255)


class PlatformAccountUpdateRequest(BaseModel):
    """平台编辑代理 / 会员：改显示名、重置密码（二选一或同时）。

    ``reset_password=True`` 表示下发统一初始密码 ``a123456``；
    ``password`` 是平台手填的新密码（按正常强度校验）。
    """

    display_name: str | None = Field(default=None, max_length=64)
    password: str | None = Field(default=None, max_length=256)
    reset_password: bool = False
