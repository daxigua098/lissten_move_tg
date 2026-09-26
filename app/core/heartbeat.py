"""运行时心跳：状态文件的读写。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.paths import read_json, write_json_atomic

# 心跳超过这个秒数就认为运行时已经不在了（运行时每 30 秒左右写一次）
STALE_AFTER_SECONDS = 90


def write_status(path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    """写入运行状态（原子写，避免读到半截内容）。"""
    body = {
        "status": "running",
        "heartbeat_at": datetime.now(UTC).isoformat(timespec="seconds"),
        **payload,
    }
    write_json_atomic(path, body)
    return body


def read_status(path: str | Path) -> dict[str, Any] | None:
    """读取运行状态。"""
    return read_json(path)


def heartbeat_age_seconds(status: dict[str, Any] | None) -> float | None:
    """心跳距今多少秒；无法判断返回 None。"""
    if not status:
        return None
    raw = status.get("heartbeat_at")
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (datetime.now(UTC) - moment).total_seconds()


def is_running(status: dict[str, Any] | None) -> bool:
    """运行时真的在跑吗：状态是 running，而且心跳还新鲜。

    进程被杀之后状态文件会停在 ``running``，只看那个字符串会让界面一直显示
    「运行中」——而加群、探测、投递其实全停了。
    """
    if not status or status.get("status") != "running":
        return False
    age = heartbeat_age_seconds(status)
    return age is not None and age <= STALE_AFTER_SECONDS
