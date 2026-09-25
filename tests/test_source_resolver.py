"""Telegram 链接与标识解析测试。"""

from __future__ import annotations

import pytest

from app.core.source_resolver import resolve_many, resolve_target


@pytest.mark.parametrize(
    ("raw", "kind", "value"),
    [
        ("t.me/duanju_material", "username", "duanju_material"),
        ("https://t.me/duanju_material", "username", "duanju_material"),
        ("http://telegram.me/duanju_material", "username", "duanju_material"),
        ("@duanju_material", "username", "duanju_material"),
        ("duanju_material", "username", "duanju_material"),
        ("  @Duanju_Material  ", "username", "Duanju_Material"),
        ("-1001234567890", "tg_id", "-1001234567890"),
        ("1234567890", "tg_id", "1234567890"),
        ("+8613800001111", "phone", "+8613800001111"),
    ],
)
def test_resolve_common_forms(raw: str, kind: str, value: str) -> None:
    target = resolve_target(raw)

    assert target.kind == kind
    assert target.value == value
    assert target.raw_input == raw


@pytest.mark.parametrize(
    "raw",
    [
        "https://t.me/+AbCdEfGh123456",
        "t.me/+AbCdEfGh123456",
        "https://t.me/joinchat/AbCdEfGh123456",
    ],
)
def test_resolve_invite_links(raw: str) -> None:
    target = resolve_target(raw)

    assert target.kind == "invite"
    assert target.value == "AbCdEfGh123456"
    assert target.is_private is True
    assert target.requires_join is True


def test_public_target_does_not_require_join() -> None:
    target = resolve_target("t.me/duanju_material")

    assert target.requires_join is False
    assert target.is_private is False


def test_normalized_key_groups_equivalent_forms() -> None:
    first = resolve_target("https://t.me/Duanju_Material")
    second = resolve_target("@duanju_material")

    assert first.normalized_key == second.normalized_key == "username:duanju_material"


@pytest.mark.parametrize("raw", ["", "   ", "t.me/+bad", "!!!", "@a"])
def test_resolve_rejects_invalid_input(raw: str) -> None:
    with pytest.raises(ValueError):
        resolve_target(raw)


def test_resolve_many_deduplicates_and_keeps_order() -> None:
    targets = resolve_many(
        [
            "t.me/alpha_group",
            "@alpha_group",
            "https://t.me/beta_group",
            "t.me/alpha_group",
        ]
    )

    assert [item.value for item in targets] == ["alpha_group", "beta_group"]
