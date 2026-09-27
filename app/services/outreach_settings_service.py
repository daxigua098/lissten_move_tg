"""冷触达租户策略：读取（缺省给默认值）与更新。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ValidationFailedError
from app.db.models import REPLY_MODE_HUMAN, REPLY_MODES, OutreachSettings

DEFAULT_COOLDOWN_SECONDS = 2 * 3600
DEFAULT_LOCK_DAYS = 30
# 跨账号重新联系的下限：要么关闭（0），要么至少 14 天
MIN_LOCK_DAYS = 14
DEFAULT_FOLLOW_UP_DAYS = 7
DEFAULT_FOLLOW_UP_MAX = 1
DEFAULT_AUTO_REPLY_ROUNDS = 3
# 严格模式下的"永久"锁定时间（用固定时间表达"永久"，避免 NULL 语义歧义）
LOCK_FOREVER = datetime(9999, 12, 31, tzinfo=UTC)

BOOL_FIELDS = (
    "strict_permanent_lock",
    "auto_reply_enabled",
    "kill_switch",
    "delete_session_on_account_delete",
)


def parse_working_hours(raw: str | None) -> list[str]:
    """读取工作时间 ``["09:00", "21:00"]``；损坏或空值按"不限制"处理。"""
    try:
        payload = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list) or len(payload) != 2:
        return []
    return [str(item) for item in payload]


def _dump(row: OutreachSettings) -> dict:
    return {
        "tenant_id": row.tenant_id,
        "default_cooldown_seconds": row.default_cooldown_seconds,
        "cross_account_lock_days": row.cross_account_lock_days,
        "strict_permanent_lock": bool(row.strict_permanent_lock),
        "follow_up_days": row.follow_up_days,
        "follow_up_max": row.follow_up_max,
        "reply_mode": row.reply_mode,
        "auto_reply_enabled": bool(row.auto_reply_enabled),
        "auto_reply_max_rounds": row.auto_reply_max_rounds,
        "auto_reply_template_id": row.auto_reply_template_id,
        "working_hours": parse_working_hours(row.working_hours),
        "daily_pool_cap": row.daily_pool_cap,
        "kill_switch": bool(row.kill_switch),
        "delete_session_on_account_delete": bool(row.delete_session_on_account_delete),
    }


def defaults() -> dict:
    """没有配置行时的默认策略。"""
    return {
        "tenant_id": None,
        "default_cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
        "cross_account_lock_days": DEFAULT_LOCK_DAYS,
        "strict_permanent_lock": False,
        "follow_up_days": DEFAULT_FOLLOW_UP_DAYS,
        "follow_up_max": DEFAULT_FOLLOW_UP_MAX,
        "reply_mode": REPLY_MODE_HUMAN,
        "auto_reply_enabled": False,
        "auto_reply_max_rounds": DEFAULT_AUTO_REPLY_ROUNDS,
        "auto_reply_template_id": None,
        "working_hours": [],
        "daily_pool_cap": None,
        "kill_switch": False,
        "delete_session_on_account_delete": False,
    }


async def read_settings(session: AsyncSession, tenant_id: int) -> dict:
    """读取策略；没有配置行时返回默认值（只读，不建行）。"""
    row = await session.get(OutreachSettings, tenant_id)
    return defaults() if row is None else _dump(row)


async def update_settings(session: AsyncSession, tenant_id: int, **fields) -> dict:
    """更新策略；首次修改时才建行。"""
    row = await session.get(OutreachSettings, tenant_id)
    if row is None:
        row = OutreachSettings(tenant_id=tenant_id)
        session.add(row)

    for name in BOOL_FIELDS:
        if name in fields:
            setattr(row, name, bool(fields[name]))

    if "default_cooldown_seconds" in fields:
        value = int(fields["default_cooldown_seconds"] or 0)
        if not 0 <= value <= 24 * 3600:
            raise ValidationFailedError("冷却时间必须在 0～24 小时之间")
        row.default_cooldown_seconds = value

    if "cross_account_lock_days" in fields:
        days = int(fields["cross_account_lock_days"] or 0)
        if days != 0 and days < MIN_LOCK_DAYS:
            raise ValidationFailedError(f"跨账号重新联系要么关闭（0），要么至少 {MIN_LOCK_DAYS} 天")
        row.cross_account_lock_days = days

    if "follow_up_days" in fields:
        value = int(fields["follow_up_days"] or 0)
        if not 0 <= value <= 365:
            raise ValidationFailedError("跟进间隔必须在 0～365 天之间")
        row.follow_up_days = value

    if "follow_up_max" in fields:
        value = int(fields["follow_up_max"] or 0)
        if not 0 <= value <= 5:
            raise ValidationFailedError("跟进次数只能在 0～5 次之间")
        row.follow_up_max = value

    if "daily_pool_cap" in fields:
        raw_cap = fields["daily_pool_cap"]
        if raw_cap is None:
            row.daily_pool_cap = None
        else:
            value = int(raw_cap)
            if value < 1:
                raise ValidationFailedError("租户日总量至少为 1")
            row.daily_pool_cap = value

    if "working_hours" in fields:
        row.working_hours = _validate_hours(fields["working_hours"])

    if "reply_mode" in fields:
        mode = (fields["reply_mode"] or REPLY_MODE_HUMAN).strip()
        if mode not in REPLY_MODES:
            raise ValidationFailedError(f"回复模式必须是 {'/'.join(REPLY_MODES)} 之一")
        row.reply_mode = mode

    if "auto_reply_max_rounds" in fields:
        value = int(fields["auto_reply_max_rounds"] or 0)
        if not 0 <= value <= 20:
            raise ValidationFailedError("自动回复轮次上限必须在 0～20 之间")
        row.auto_reply_max_rounds = value

    if "auto_reply_template_id" in fields:
        raw_template = fields["auto_reply_template_id"]
        row.auto_reply_template_id = int(raw_template) if raw_template else None

    await session.commit()
    await session.refresh(row)
    return _dump(row)


def _validate_hours(value: object) -> str:
    if value in (None, [], ""):
        return "[]"
    if not isinstance(value, list) or len(value) != 2:
        raise ValidationFailedError("工作时间需要填写开始与结束两个时间点")
    for item in value:
        text = str(item)
        parts = text.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValidationFailedError("时间格式应为 HH:MM")
        hour, minute = int(parts[0]), int(parts[1])
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValidationFailedError("时间超出范围")
    return json.dumps([str(item) for item in value])


def lock_until(settings: dict, now: datetime) -> datetime | None:
    """本次首触后，跨账号重新联系的最早时间。"""
    if settings.get("strict_permanent_lock"):
        return LOCK_FOREVER
    days = int(settings.get("cross_account_lock_days") or 0)
    if days <= 0:
        return None
    return now + timedelta(days=days)
