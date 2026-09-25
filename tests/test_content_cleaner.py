"""内容净化与类型筛选测试（纯逻辑）。"""

from __future__ import annotations

import pytest

from app.core.content_cleaner import (
    KIND_OTHER,
    KIND_PHOTO,
    KIND_SERVICE,
    KIND_TEXT,
    KIND_VIDEO,
    CleanRules,
    MessageView,
    allows_kind,
    clean_text,
    filter_reason,
    is_empty_text,
    resolve_caption,
)


def test_strip_urls_and_links() -> None:
    text = "看这里 https://example.com/a 还有 t.me/xxx 以及 www.demo.com 结束"

    cleaned = clean_text(text, CleanRules())

    assert "https://example.com" not in cleaned
    assert "t.me/xxx" not in cleaned
    assert "www.demo.com" not in cleaned
    assert "看这里" in cleaned and "结束" in cleaned


def test_strip_mentions_keeps_email_like_text() -> None:
    text = "联系 @seller_or_bot 或 admin@example.com"

    cleaned = clean_text(text, CleanRules())

    assert "@seller_or_bot" not in cleaned
    assert "admin@example.com" in cleaned


def test_phone_stripped_only_when_enabled() -> None:
    text = "电话 138 0000 1111 找我"

    kept = clean_text(text, CleanRules(strip_phone=False))
    stripped = clean_text(text, CleanRules(strip_phone=True))

    assert "138 0000 1111" in kept
    assert "138" not in stripped
    assert "找我" in stripped


def test_promo_blacklist_drops_whole_line() -> None:
    text = "正文第一行\n加我微信 领取福利\n正文第二行"

    cleaned = clean_text(text, CleanRules(promo_blacklist=("加我微信",)))

    assert "加我微信" not in cleaned
    assert cleaned == "正文第一行\n正文第二行"


def test_promo_regex_and_invalid_regex_fallback() -> None:
    text = "正常内容\n官方入口：abc123\n推广码 8888"
    rules = CleanRules(promo_regex=(r"官方入口[:：]", "推广码 [0-9]+"))

    cleaned = clean_text(text, rules)

    assert cleaned == "正常内容"

    # 正则写错时跳过该条规则，不误删正常内容
    broken = clean_text("推广码 8888", CleanRules(promo_regex=("[",)))
    assert broken == "推广码 8888"


def test_blank_lines_are_collapsed() -> None:
    text = "第一行\n\n\n\n第二行"

    cleaned = clean_text(text, CleanRules())

    assert cleaned == "第一行\n\n第二行"


def test_resolve_caption_policies() -> None:
    assert resolve_caption("正文", has_media=True) == ("正文", True)
    assert resolve_caption("   ", empty_text_policy="keep_media", has_media=True) == ("", True)
    assert resolve_caption("", empty_text_policy="drop", has_media=True) == ("", False)
    assert resolve_caption("", empty_text_policy="keep_media", has_media=False) == ("", False)


def test_is_empty_text() -> None:
    assert is_empty_text("   \n ") is True
    assert is_empty_text("abc") is False


def test_allows_kind() -> None:
    assert allows_kind(KIND_VIDEO, ["photo", "video"]) is True
    assert allows_kind(KIND_TEXT, ["photo", "video"]) is False


@pytest.mark.parametrize(
    ("view", "expected"),
    [
        (MessageView(message_id=1, kind=KIND_SERVICE), "系统消息"),
        (MessageView(message_id=2, kind=KIND_OTHER), "不支持的消息类型"),
        (MessageView(message_id=3, kind=KIND_TEXT), "内容类型不在白名单：text"),
        (MessageView(message_id=4, kind=KIND_PHOTO), None),
    ],
)
def test_filter_reason(view: MessageView, expected: str | None) -> None:
    reason = filter_reason(view, content_types=["photo", "video"])

    if expected is None:
        assert reason is None
    else:
        assert reason is not None and expected.split("：")[0] in reason


def test_rules_from_config_defaults() -> None:
    rules = CleanRules.from_config({})

    assert rules.strip_url is True
    assert rules.strip_mention is True
    assert rules.strip_phone is False
    assert rules.promo_blacklist == ()
