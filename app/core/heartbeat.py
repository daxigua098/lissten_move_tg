"""运行时心跳：状态文件的读写。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.paths import read_json, write_json_atomic


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
