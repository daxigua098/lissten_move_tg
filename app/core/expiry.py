"""有效期计算：按自然日计时，到期切点是最后一天 23:59:59（本地时区）。

约定（八项决议第 9 条）：

- **按天**，不按时刻精细计费；
- 到期日当天 23:59:59（配置里的 ``app.timezone``，默认 Asia/Shanghai）即断，无宽限期；
- "开通 N 天"= 开通当天起算 N 个自然日，所以 1 天试用就是**当天** 23:59:59 结束；
- 库里统一存 UTC，展示时再换回本地时区。

P3 用它算开号/续期的到期时间，P4 的到期巡检用它判断是否过期。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.errors import ValidationFailedError

DEFAULT_TIMEZONE = "Asia/Shanghai"
END_OF_DAY = time(23, 59, 59)

# 没装 tzdata 的机器（Windows 默认就没有）取不到 IANA 时区库，
# 这里给几个常见时区兜一个固定偏移，保证开号不会因为时区库缺失而挂掉。
_FALLBACK_OFFSETS: dict[str, timedelta] = {
    "Asia/Shanghai": timedelta(hours=8),
    "Asia/Hong_Kong": timedelta(hours=8),
    "Asia/Taipei": timedelta(hours=8),
    "Asia/Singapore": timedelta(hours=8),
    "UTC": timedelta(0),
}

MIN_DAYS = 1
MAX_DAYS = 3650


def resolve_timezone(name: str | None) -> tzinfo:
    """按配置取时区；时区库缺失或名字不合法时退回固定偏移（默认东八区）。

    绝不能让"机器没装 tzdata"这种环境问题把开号链路拖垮。
    """
    key = (name or DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    try:
        return ZoneInfo(key)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return timezone(_FALLBACK_OFFSETS.get(key, timedelta(hours=8)))


def local_day_end(day: date, tz: tzinfo) -> datetime:
    """本地某一天的 23:59:59，转成 UTC。"""
    return datetime.combine(day, END_OF_DAY, tzinfo=tz).astimezone(UTC)


def normalize_days(days: int) -> int:
    """校验开通天数。"""
    try:
        value = int(days)
    except (TypeError, ValueError) as exc:
        raise ValidationFailedError("开通天数必须是整数") from exc
    if value < MIN_DAYS or value > MAX_DAYS:
        raise ValidationFailedError(f"开通天数必须在 {MIN_DAYS}–{MAX_DAYS} 天之间")
    return value


def expiry_for_days(
    days: int,
    *,
    tz_name: str | None = None,
    start: date | None = None,
) -> datetime:
    """从 ``start``（默认今天）起算 N 个自然日的到期时间（UTC）。

    1 天 = 当天 23:59:59 结束；30 天 = 第 30 天 23:59:59 结束。
    """
    value = normalize_days(days)
    tz = resolve_timezone(tz_name)
    first_day = start or datetime.now(tz).date()
    return local_day_end(first_day + timedelta(days=value - 1), tz)


def local_today(*, tz_name: str | None = None, now: datetime | None = None) -> date:
    """本地"今天"，用于按天比对。"""
    tz = resolve_timezone(tz_name)
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tz).date()


def is_expired(
    expires_at: datetime | None,
    *,
    tz_name: str | None = None,
    now: datetime | None = None,
) -> bool:
    """是否已过到期切点；``None`` 表示永不过期。"""
    if expires_at is None:
        return False
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    target = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
    return moment >= target
