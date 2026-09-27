"""P4-01：租户有效状态的判定顺序、边界与剩余天数。"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from app.core.expiry import local_day_end, resolve_timezone
from app.db.models import (
    STOP_REASON_EXPIRED,
    STOP_REASON_MANUAL,
    TENANT_STATUS_ACTIVE,
    TENANT_STATUS_EXPIRED,
    TENANT_STATUS_SUSPENDED,
    Tenant,
)
from app.services import tenant_status_service


def _tenant(**kwargs) -> Tenant:
    """内存里的租户对象：判定只看字段，不必落库。"""
    data: dict = {
        "id": 7,
        "name": "甲",
        "kind": "member",
        "status": TENANT_STATUS_ACTIVE,
        "expires_at": None,
        "quota_type": "none",
        "quota_held": False,
        "runtime_enabled": True,
        "runtime_stop_reason": None,
    }
    data.update(kwargs)
    return Tenant(**data)


def test_never_expires_is_active() -> None:
    """自营租户 expires_at 为空 → 永不过期。"""
    tenant = _tenant(status=TENANT_STATUS_ACTIVE, expires_at=None)

    assert tenant_status_service.effective_status(tenant) == TENANT_STATUS_ACTIVE
    assert tenant_status_service.is_active(tenant) is True


def test_expires_at_wins_even_when_status_field_says_active() -> None:
    """判定第 2 条不看 status：时间过了就是过期（心跳可能还没跑到）。"""
    now = datetime(2026, 9, 27, 12, 0, tzinfo=UTC)
    tenant = _tenant(status=TENANT_STATUS_ACTIVE, expires_at=now - timedelta(seconds=1))

    assert tenant_status_service.effective_status(tenant, now=now) == TENANT_STATUS_EXPIRED


def test_status_expired_without_expires_at_still_expired() -> None:
    """判定第 3 条：status 字段是"到期善后已执行"的标记。"""
    tenant = _tenant(status=TENANT_STATUS_EXPIRED, expires_at=None)

    assert tenant_status_service.effective_status(tenant) == TENANT_STATUS_EXPIRED


def test_suspended_beats_expired() -> None:
    """判定第 1 条最优先：既过期又停用时按停用算（停用不释放额度）。"""
    past = datetime(2026, 1, 1, tzinfo=UTC)
    tenant = _tenant(status=TENANT_STATUS_SUSPENDED, expires_at=past)

    assert tenant_status_service.effective_status(tenant) == TENANT_STATUS_SUSPENDED


def test_boundary_is_last_second_of_local_day() -> None:
    """到期切点是**当天 23:59:59（Asia/Shanghai）**：到点即断，无宽限。"""
    tz = resolve_timezone("Asia/Shanghai")
    expires = local_day_end(date(2026, 9, 27), tz)
    tenant = _tenant(status=TENANT_STATUS_ACTIVE, expires_at=expires)

    one_second_before = expires - timedelta(seconds=1)
    assert tenant_status_service.is_active(tenant, now=one_second_before) is True
    # 到点这一秒就已经不算可用
    assert tenant_status_service.is_active(tenant, now=expires) is False
    assert tenant_status_service.is_active(tenant, now=expires + timedelta(minutes=1)) is False


def test_days_left_counts_local_days() -> None:
    """剩余天数按本地自然日算：今天到期 = 0 天，还有 6 天 = 6。"""
    tz = resolve_timezone("Asia/Shanghai")
    now = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)  # 上海时间 11:00
    today = now.astimezone(tz).date()

    assert tenant_status_service.days_left(None, now=now) is None
    assert tenant_status_service.days_left(local_day_end(today, tz), now=now) == 0
    assert tenant_status_service.days_left(local_day_end(today + timedelta(days=6), tz), now=now) == 6


def test_evaluate_payload_shape() -> None:
    """状态块给接口/界面用，字段固定。"""
    tz = resolve_timezone("Asia/Shanghai")
    now = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)
    today = now.astimezone(tz).date()
    tenant = _tenant(
        expires_at=local_day_end(today + timedelta(days=2), tz),
        runtime_enabled=False,
        runtime_stop_reason=STOP_REASON_MANUAL,
    )

    payload = tenant_status_service.state_payload(tenant_status_service.evaluate(tenant, now=now))

    assert payload["status"] == TENANT_STATUS_ACTIVE
    assert payload["label"] == "正常"
    assert payload["days_left"] == 2
    assert payload["runtime_enabled"] is False
    assert payload["runtime_stop_reason"] == STOP_REASON_MANUAL
    assert payload["expires_at"].endswith("+00:00")


def test_stop_reason_labels() -> None:
    """停止原因有中文说法，界面直接显示。"""
    assert tenant_status_service.stop_reason_label(STOP_REASON_EXPIRED) == "账号已过期，功能已停止"
    assert tenant_status_service.stop_reason_label(STOP_REASON_MANUAL) == "已手动停止"
    assert tenant_status_service.stop_reason_label(None) == ""