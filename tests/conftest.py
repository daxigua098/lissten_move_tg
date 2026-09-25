"""pytest 公共夹具。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def valid_secret_key() -> str:
    """形态合法的 Fernet 密钥（43 位 urlsafe base64 + 等号）。"""
    return "A" * 43 + "="


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """临时的项目根目录，含 configs 目录。"""
    root = tmp_path / "project"
    (root / "configs").mkdir(parents=True)
    return root


@pytest.fixture
def write_config(project_root: Path):
    """写入 configs/config.yaml 的辅助函数。"""

    def _write(text: str) -> Path:
        path = project_root / "configs" / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def write_env(project_root: Path):
    """写入 .env 的辅助函数。"""

    def _write(lines: list[str]) -> Path:
        path = project_root / ".env"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    return _write
