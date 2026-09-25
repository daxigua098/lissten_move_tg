"""密码哈希、会话令牌与敏感字段加解密。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

from app.core.config import AppConfig

_hasher = PasswordHasher(time_cost=2, memory_cost=65536, parallelism=2)

# 账号不存在时也执行一次哈希校验，避免通过响应时间判断用户名是否存在
_DUMMY_PASSWORD = "dummy-password-for-timing-equalisation"
_DUMMY_HASH = _hasher.hash(_DUMMY_PASSWORD)

SESSION_TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    """生成 argon2id 密码哈希。"""
    return _hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    """校验密码，任何异常都视为不匹配。"""
    try:
        return _hasher.verify(encoded, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError, ValueError):
        return False


def dummy_verify() -> None:
    """在账号不存在时调用，保持与其他失败分支一致的耗时。"""
    verify_password(_DUMMY_PASSWORD, _DUMMY_HASH)


def needs_rehash(encoded: str) -> bool:
    """判断哈希参数是否需要升级。"""
    try:
        return _hasher.check_needs_rehash(encoded)
    except (InvalidHashError, ValueError):
        return True


def create_session_token() -> str:
    """生成明文会话令牌（只交给客户端，数据库只存哈希）。"""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """会话令牌哈希（sha256 十六进制）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry(config: AppConfig, *, now: datetime | None = None) -> datetime:
    """按配置计算会话过期时间。"""
    base = now or datetime.now(UTC)
    return base + timedelta(hours=config.security.session_hours)


def compare_token(supplied: str, expected: str) -> bool:
    """常量时间比较，用于 API Token 校验。"""
    return hmac.compare_digest(supplied, expected)


def mask_phone(phone: str) -> str:
    """手机号掩码：带区号时保留区号与后 4 位，其余打码。"""
    text = (phone or "").strip()
    digits = [char for char in text if char.isdigit()]
    if len(digits) < 7:
        return "*" * len(text) if text else ""
    tail = "".join(digits[-4:])
    if text.startswith("+"):
        return f"+{''.join(digits[:2])}***{tail}"
    return f"{'*' * (len(digits) - 4)}{tail}"


class FieldCipher:
    """敏感字段加解密（Bot Token、API Hash 等）。"""

    def __init__(self, secret_key: str) -> None:
        key = (secret_key or "").strip()
        if not key:
            raise ValueError("SECRET_KEY 未配置，无法加密敏感字段")
        self._fernet = Fernet(key.encode("utf-8"))

    @classmethod
    def from_config(cls, config: AppConfig) -> FieldCipher:
        """按配置构造。"""
        return cls(config.secrets.secret_key)

    def encrypt(self, plain: str) -> str:
        """加密明文。"""
        return self._fernet.encrypt(plain.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str) -> str:
        """解密密文；密钥不匹配时抛出 ValueError。"""
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except InvalidToken as exc:
            raise ValueError("密文无法解密：SECRET_KEY 可能已更换") from exc
