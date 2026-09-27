"""执行账号与控制 Bot 的请求模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AccountStatusLiteral = Literal["pending_login", "active", "restricted", "disabled"]
AccountPurposeLiteral = Literal["listen", "outreach"]
OutreachTierLiteral = Literal["NEW", "WARMING", "STANDARD", "MATURE"]
OutreachStateLiteral = Literal[
    "NEW",
    "READY",
    "COOLING",
    "CAPPED",
    "LIMITED",
    "PAUSED",
    "DISABLED",
]


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
    # listen=执行账号（采集/搬运/监听），outreach=发信息账号（冷触达/对话）
    purpose: AccountPurposeLiteral = "listen"
    # 发信息账号必须由会员确认「账号归我所有并已获授权用于发送消息」
    owner_confirmed: bool = False


class AccountUpdateRequest(BaseModel):
    """更新执行账号（字段均可选）。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    phone: str | None = Field(default=None, max_length=32)
    api_id: int | None = Field(default=None, gt=0)
    api_hash: str | None = Field(default=None, max_length=128)
    status: AccountStatusLiteral | None = None
    is_default: bool | None = None
    note: str | None = Field(default=None, max_length=255)
    # 发信息账号的运营态 / 档位（人工调整）
    outreach_tier: OutreachTierLiteral | None = None
    outreach_state: OutreachStateLiteral | None = None


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


class LoginStartRequest(BaseModel):
    """开始登录（是否强制走短信）。"""

    force_sms: bool = False


class LoginCodeRequest(BaseModel):
    """提交登录验证码。"""

    code: str = Field(min_length=1, max_length=16)


class LoginPasswordRequest(BaseModel):
    """提交两步验证密码。"""

    password: str = Field(min_length=1, max_length=256)


class AccountImportRequest(BaseModel):
    """粘贴「+手机号 接码地址」批量导入发信息账号。"""

    text: str = Field(min_length=1, max_length=20000)
    owner_confirmed: bool = False


class AutoLoginRequest(BaseModel):
    """批量自动登录的账号列表。"""

    account_ids: list[int] = Field(default_factory=list, max_length=200)


class AutoLoginStopRequest(BaseModel):
    """停止自动登录（留空表示全部）。"""

    account_ids: list[int] = Field(default_factory=list, max_length=200)
