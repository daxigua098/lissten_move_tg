"""loguru 初始化与日志脱敏。"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from loguru import logger

from app.core.config import AppConfig
from app.core.paths import ensure_dir

CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
)
FILE_FORMAT = "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {message}"

MASK = "***"
# 长数字串至少 10 位才视为电话号码；短消息 ID（如 133106）保持原样便于排查
MIN_PHONE_DIGITS = 10

_SENSITIVE_KEYS = (
    "secret_key",
    "api_hash",
    "apihash",
    "authorization",
    "password",
    "passwd",
    "pwd",
    "token",
    "secret",
    "phone",
    "bearer",
    "session",
)
_KEY_VALUE_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(_SENSITIVE_KEYS) + r")\b\s*[:=]\s*(?!\{)(\"[^\"]*\"|'[^']*'|\S+)"
)
# 模板形如 password={} 时，占位符本身不能被打码（否则会破坏格式化），
# 改为把该条日志的字符串参数整体打码。
_SECRET_PLACEHOLDER_PATTERN = re.compile(
    r"(?i)\b(" + "|".join(_SENSITIVE_KEYS) + r")\b\s*[:=]\s*\{[^}]*\}"
)
_PHONE_CANDIDATE_PATTERN = re.compile(r"(?<!\d)(?:\+?\d{1,3}[\s-]?)?(?:\d[\s-]?){7,}\d(?!\d)")
_DIGIT_PATTERN = re.compile(r"\d")


def log_file_path(config: AppConfig) -> Path:
    """返回日志文件绝对路径。"""
    return config.path(config.logging.file)


def sanitize(text: str) -> str:
    """对日志文本脱敏。

    规则：
        1. 键值型机密字段（password / token / secret_key / phone 等）整体打码；
        2. 位数达到电话号码级别的长数字串保留末尾 4 位，其余打码；
           带国际区号的号码额外保留前两位区号（如 `+86***1111`），便于分辨归属地。

    短数字（消息 ID、账号 ID 等）保持原样，避免影响排查。
    """
    if not text:
        return text
    masked = _KEY_VALUE_PATTERN.sub(_redact_key_value, text)
    return _PHONE_CANDIDATE_PATTERN.sub(_redact_phone, masked)


def setup_logging(config: AppConfig, *, enqueue: bool = False) -> Path:
    """初始化日志输出（控制台 + 文件），返回日志文件路径。

    enqueue=False 时文件写入是同步的，便于测试与排查；高吞吐场景可改为 True。
    """
    logger.remove()
    logger.configure(patcher=_patcher)
    logger.add(
        sys.stderr,
        level=config.logging.level,
        format=CONSOLE_FORMAT,
        colorize=True,
        backtrace=False,
        diagnose=False,
    )
    path = log_file_path(config)
    ensure_dir(path.parent)
    logger.add(
        str(path),
        level=config.logging.level,
        format=FILE_FORMAT,
        rotation=f"{config.logging.rotation_mb} MB",
        retention=f"{config.logging.retention_days} days",
        encoding="utf-8",
        enqueue=enqueue,
        backtrace=False,
        diagnose=False,
    )
    return path


def _patcher(record: dict[str, Any]) -> None:
    message = str(record.get("message", ""))
    args = record.get("args")
    if args:
        secret_context = bool(_SECRET_PLACEHOLDER_PATTERN.search(message))
        record["args"] = tuple(
            (MASK if secret_context else sanitize(item)) if isinstance(item, str) else item
            for item in args
        )
    record["message"] = sanitize(message)


def _redact_key_value(match: re.Match[str]) -> str:
    return f"{match.group(1)}={MASK}"


def _redact_phone(match: re.Match[str]) -> str:
    raw = match.group(0)
    digits = "".join(_DIGIT_PATTERN.findall(raw))
    if len(digits) < MIN_PHONE_DIGITS:
        return raw
    if raw.lstrip().startswith("+"):
        return f"+{digits[:2]}{MASK}{digits[-4:]}"
    return f"{MASK}{digits[-4:]}"
