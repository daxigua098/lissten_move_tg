"""密码哈希、会话令牌与字段加解密测试。"""

from __future__ import annotations

import pytest

from app.core.config import load_config
from app.core.security import (
    FieldCipher,
    compare_token,
    create_session_token,
    dummy_verify,
    hash_password,
    hash_session_token,
    needs_rehash,
    session_expiry,
    verify_password,
)


def test_password_hash_roundtrip() -> None:
    encoded = hash_password("admin123")

    assert encoded != "admin123"
    assert verify_password("admin123", encoded) is True
    assert verify_password("wrong-password", encoded) is False
    assert needs_rehash(encoded) is False


def test_verify_password_tolerates_broken_hash() -> None:
    assert verify_password("admin123", "not-a-hash") is False
    assert needs_rehash("not-a-hash") is True


def test_dummy_verify_does_not_raise() -> None:
    dummy_verify()


def test_session_token_is_random_and_hashed_deterministically() -> None:
    first = create_session_token()
    second = create_session_token()

    assert first != second
    assert len(first) > 20
    assert hash_session_token(first) == hash_session_token(first)
    assert hash_session_token(first) != first
    assert len(hash_session_token(first)) == 64


def test_session_expiry_follows_config(project_root, valid_secret_key) -> None:
    config = load_config(
        project_root=project_root,
        environ={"SECRET_KEY": valid_secret_key, "ADMIN_PASSWORD": "custom-pass1"},
    )

    from datetime import UTC, datetime

    expires_at = session_expiry(config)
    hours = (expires_at - datetime.now(UTC)).total_seconds() / 3600

    assert config.security.session_hours - 1 < hours <= config.security.session_hours


def test_compare_token() -> None:
    assert compare_token("abc", "abc") is True
    assert compare_token("abc", "abd") is False


def test_field_cipher_roundtrip(project_root, valid_secret_key) -> None:
    config = load_config(project_root=project_root, environ={"SECRET_KEY": valid_secret_key})
    cipher = FieldCipher.from_config(config)

    token = cipher.encrypt("123456:bot-token")

    assert token != "123456:bot-token"
    assert cipher.decrypt(token) == "123456:bot-token"


def test_field_cipher_rejects_wrong_key(project_root, valid_secret_key) -> None:
    config = load_config(project_root=project_root, environ={"SECRET_KEY": valid_secret_key})
    token = FieldCipher.from_config(config).encrypt("secret")

    other = FieldCipher("B" * 43 + "=")

    with pytest.raises(ValueError):
        other.decrypt(token)


def test_field_cipher_requires_secret_key() -> None:
    with pytest.raises(ValueError):
        FieldCipher("")
