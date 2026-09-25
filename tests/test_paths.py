"""路径与原子写工具测试。"""

from __future__ import annotations

import json
from pathlib import Path

from app.core.paths import (
    data_dir,
    ensure_dir,
    project_root,
    read_json,
    resolve_path,
    write_json_atomic,
)


def test_project_root_defaults_to_code_directory() -> None:
    root = project_root()
    assert (root / "app" / "core" / "paths.py").is_file()


def test_project_root_accepts_override(tmp_path: Path) -> None:
    assert project_root(tmp_path) == tmp_path.resolve()


def test_resolve_path_handles_relative_and_absolute(tmp_path: Path) -> None:
    assert resolve_path("data/app.db", tmp_path) == tmp_path / "data" / "app.db"
    absolute = tmp_path / "absolute.db"
    assert resolve_path(absolute, tmp_path) == absolute


def test_ensure_dir_creates_nested_directories(tmp_path: Path) -> None:
    target = ensure_dir(tmp_path / "a" / "b")
    assert target.is_dir()
    assert data_dir(tmp_path) == tmp_path / "data"


def test_write_json_atomic_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "status.json"
    write_json_atomic(target, {"pid": 8456, "状态": "运行中"})

    assert read_json(target) == {"pid": 8456, "状态": "运行中"}
    assert json.loads(target.read_text(encoding="utf-8"))["pid"] == 8456


def test_write_json_atomic_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "runtime_status.json"
    write_json_atomic(target, {"a": 1})
    write_json_atomic(target, {"a": 2})

    leftovers = [path.name for path in tmp_path.iterdir() if path.name != target.name]
    assert leftovers == []
    assert read_json(target) == {"a": 2}


def test_read_json_returns_none_for_missing_or_invalid(tmp_path: Path) -> None:
    assert read_json(tmp_path / "missing.json") is None

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert read_json(broken) is None

    array = tmp_path / "array.json"
    array.write_text("[1, 2]", encoding="utf-8")
    assert read_json(array) is None
