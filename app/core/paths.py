"""项目路径常量与原子文件写入工具。"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TEMP_SUFFIX = ".tmp"
_ENCODING = "utf-8"


def project_root(root: str | Path | None = None) -> Path:
    """返回项目根目录；传入 root 时以传入值为准（测试与多实例部署用）。"""
    if root is None:
        return PROJECT_ROOT
    return Path(root).expanduser().resolve()


def resolve_path(value: str | Path, root: str | Path | None = None) -> Path:
    """解析路径：绝对路径原样返回，相对路径基于项目根目录。"""
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return project_root(root) / candidate


def ensure_dir(path: str | Path) -> Path:
    """确保目录存在并返回该目录。"""
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    return target


def data_dir(root: str | Path | None = None) -> Path:
    """运行时数据目录。"""
    return ensure_dir(project_root(root) / "data")


def logs_dir(root: str | Path | None = None) -> Path:
    """日志目录。"""
    return ensure_dir(project_root(root) / "logs")


def backups_dir(root: str | Path | None = None) -> Path:
    """备份目录。"""
    return ensure_dir(project_root(root) / "backups")


def uploads_dir(root: str | Path | None = None) -> Path:
    """上传素材目录。"""
    return ensure_dir(project_root(root) / "assets" / "uploads")


def write_json_atomic(path: str | Path, payload: Any) -> None:
    """原子写入 JSON：先写同目录临时文件再 os.replace，避免读到半截内容。"""
    target = Path(path)
    ensure_dir(target.parent)
    handle_fd, temp_name = tempfile.mkstemp(
        prefix=f"{target.name}.",
        suffix=_TEMP_SUFFIX,
        dir=str(target.parent),
    )
    try:
        with os.fdopen(handle_fd, "w", encoding=_ENCODING) as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp_name)
        raise


def read_json(path: str | Path) -> dict[str, Any] | None:
    """读取 JSON 文件；文件不存在或内容非法时返回 None。"""
    target = Path(path)
    try:
        with target.open("r", encoding=_ENCODING) as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None
