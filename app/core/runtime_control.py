"""运行时控制：暂停与停止开关。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.paths import read_json, write_json_atomic


def read_control(path: str | Path) -> dict[str, Any]:
    """读取控制文件（缺失时返回默认值）。"""
    payload = read_json(path) or {}
    return {
        "paused": bool(payload.get("paused", False)),
        "stop_requested": bool(payload.get("stop_requested", False)),
        "updated_at": payload.get("updated_at"),
    }


def set_paused(path: str | Path, paused: bool) -> dict[str, Any]:
    """设置暂停状态。"""
    payload = read_control(path)
    payload["paused"] = bool(paused)
    return _write(path, payload)


def set_stop_requested(path: str | Path, stop: bool) -> dict[str, Any]:
    """请求停止运行时进程。"""
    payload = read_control(path)
    payload["stop_requested"] = bool(stop)
    return _write(path, payload)


def is_paused(path: str | Path) -> bool:
    """是否处于暂停状态。"""
    return read_control(path)["paused"]


def is_stop_requested(path: str | Path) -> bool:
    """是否已请求停止。"""
    return read_control(path)["stop_requested"]


def _write(path: str | Path, payload: dict[str, Any]) -> dict[str, Any]:
    body = {
        **payload,
        "updated_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    write_json_atomic(path, body)
    return body
