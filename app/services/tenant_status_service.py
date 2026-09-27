"""租户有效状态判定（P4-01）。

真源是 ``expires_at`` 与 ``status`` 两个字段的**组合**，而不是单个 ``status``：

1. ``status='suspended'`` → ``suspended``（停用是处罚，压过一切）；
2. ``expires_at`` 非空且已过切点 → ``expired``（**不看** ``status``：心跳可能还没跑到，
   时间已经过了就必须按过期处理）；
3. ``status='expired'`` → ``expired``（"到期善后已执行"的标记，保证强停只做一次）；
4. 其余 → ``active``。

判定一律"随请求现算"，不缓存结果本身；运行时投递路径另有一份 30 秒 TTL 的
前置缓存（见 :mod:`app.services.tenant_runtime_service`），那里缓存的也是
``runtime_enabled`` 与 ``expires_at`` 这两个**原始值**，过期比较仍然现算。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.core.expiry import is_expired, local_today, resolve_timezone
from app.db.base import as_utc
from app.db.models import (
    TENANT_STATUS_ACTIVE,
    TENANT_STATUS_EXPIRED,
    TENANT_STATUS_SUSPENDED,
    Tenant,
)

# 状态显示名（界面与错误文案共用）
STATUS_LABELS = {
    TENANT_STATUS_ACTIVE: "正常",
    TENANT_STATUS_EXPIRED: "已过期",
    TENANT_STATUS_SUSPENDED: "已停用",
}

# 非 active 时统一的对外文案：会员看得懂，也知道该找谁
BLOCKED_MESSAGE = "账号已过期或已停用，功能已停止，请联系你的上级续费或解停"


@dataclass(frozen=True)
class TenantState:
    """一次判定的完整结果，给接口与界面用。"""

    tenant_id: int
    status: str
    expires_at: datetime | None
    days_left: int | None
    runtime_enabled: bool
    runtime_stop_reason: str | None

    @property
    def active(self) -> bool:
        return self.status == TENANT_STATUS_ACTIVE

    @property
    def label(self) -> str:
        return STATUS_LABELS.get(self.status, self.status)


def effective_status(tenant: Tenant, *, now: datetime | None = None) -> str:
    """按 P4-01 的顺序判定租户有效状态。"""
    if tenant.status == TENANT_STATUS_SUSPENDED:
        return TENANT_STATUS_SUSPENDED
    if is_expired(tenant.expires_at, now=now):
        return TENANT_STATUS_EXPIRED
    if tenant.status == TENANT_STATUS_EXPIRED:
        return TENANT_STATUS_EXPIRED
    return TENANT_STATUS_ACTIVE


def is_active(tenant: Tenant, *, now: datetime | None = None) -> bool:
    """租户当前是否可用（未过期且未停用）。"""
    return effective_status(tenant, now=now) == TENANT_STATUS_ACTIVE


def days_left(
    expires_at: datetime | None,
    *,
    now: datetime | None = None,
    tz_name: str | None = None,
) -> int | None:
    """剩余自然日：到期日（本地）减今天（本地）。

    永不过期返回 ``None``；已过期返回负数（界面按 0 天展示）。
    """
    if expires_at is None:
        return None
    tz = resolve_timezone(tz_name)
    today = local_today(tz_name=tz_name, now=now)
    target = as_utc(expires_at).astimezone(tz).date()
    return (target - today).days


def evaluate(
    tenant: Tenant,
    *,
    now: datetime | None = None,
    tz_name: str | None = None,
) -> TenantState:
    """一次性算出状态、剩余天数与运行开关。"""
    return TenantState(
        tenant_id=tenant.id,
        status=effective_status(tenant, now=now),
        expires_at=as_utc(tenant.expires_at),
        days_left=days_left(tenant.expires_at, now=now, tz_name=tz_name),
        runtime_enabled=bool(tenant.runtime_enabled),
        runtime_stop_reason=tenant.runtime_stop_reason,
    )


def state_payload(state: TenantState) -> dict[str, Any]:
    """状态结果转成接口 / 身份字典可用的结构。"""
    return {
        "status": state.status,
        "label": state.label,
        "expires_at": state.expires_at.isoformat() if state.expires_at else None,
        "days_left": state.days_left,
        "runtime_enabled": state.runtime_enabled,
        "runtime_stop_reason": state.runtime_stop_reason,
    }


def stop_reason_label(reason: str | None) -> str:
    """运行停止原因的中文说法。"""
    return {
        "manual": "已手动停止",
        "expired": "账号已过期，功能已停止",
        "suspended": "账号已停用，功能已停止",
    }.get(str(reason or ""), "")
