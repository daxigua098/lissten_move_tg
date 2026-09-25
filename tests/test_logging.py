"""日志初始化与脱敏测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from loguru import logger

from app.core.config import load_config
from app.core.logging import log_file_path, sanitize, setup_logging


@pytest.fixture
def configured(project_root, valid_secret_key):
    config = load_config(
        project_root=project_root,
        environ={"SECRET_KEY": valid_secret_key, "ADMIN_PASSWORD": "custom-pass"},
    )
    path = setup_logging(config)
    try:
        yield config, path
    finally:
        logger.remove()


def test_setup_logging_writes_file(configured) -> None:
    _config, path = configured

    logger.info("服务启动完成")

    content = path.read_text(encoding="utf-8")
    assert "服务启动完成" in content
    assert path.parent.is_dir()


def test_log_file_path_follows_config(project_root, valid_secret_key) -> None:
    config = load_config(
        project_root=project_root,
        environ={"SECRET_KEY": valid_secret_key},
    )

    assert log_file_path(config) == Path(config.project_root) / "logs" / "app.log"


def test_sanitize_masks_key_value_secrets() -> None:
    text = "login failed password=admin123 token: abcdef SECRET_KEY=XYZ"

    masked = sanitize(text)

    assert "admin123" not in masked
    assert "abcdef" not in masked
    assert "XYZ" not in masked
    assert masked.count("***") == 3


def test_sanitize_masks_phone_numbers() -> None:
    assert "13800001111" not in sanitize("联系人 13800001111")
    assert "+86***1111" in sanitize("联系人 +8613800001111")


def test_sanitize_keeps_short_ids() -> None:
    text = "Delivered source=1 messages=(133106,) target=2 as message=9845"

    assert sanitize(text) == text


def test_logged_secret_is_masked_in_file(configured) -> None:
    _config, path = configured

    logger.info("管理员登录 password={}", "admin123")

    content = path.read_text(encoding="utf-8")
    assert "admin123" not in content
