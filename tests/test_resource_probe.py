"""资源探测的口径计算（F-R06 / F-R07）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core.content_cleaner import MessageView
from app.core.resource_probe import (
    ProbeThresholds,
    compute_metrics,
    detect_categories,
    detect_country,
    detect_language,
    looks_like_ad,
)

BASE_TIME = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def message(
    index: int,
    text: str,
    *,
    sender: int = 100,
    bot: bool = False,
    minutes_ago: int = 0,
) -> MessageView:
    """构造一条采样消息。"""
    return MessageView(
        message_id=index,
        text=text,
        sender_id=sender,
        sender_is_bot=bot,
        date=BASE_TIME - timedelta(minutes=minutes_ago),
    )


class TestAdDetection:
    def test_keyword_selling_is_ad(self) -> None:
        assert looks_like_ad("专业代发推广，加微信详聊") is True

    def test_bare_link_plus_contact_is_ad(self) -> None:
        assert looks_like_ad("t.me/somechannel 微信 abc123") is True
        assert looks_like_ad("@some_channel 加我 whatsapp") is True

    def test_normal_chat_is_not_ad(self) -> None:
        assert looks_like_ad("有人在吗，想问问这个怎么弄") is False
        assert looks_like_ad("分享一个不错的链接 https://t.me/other 大家可以看看") is False
        assert looks_like_ad(None) is False


class TestLanguageAndCountry:
    def test_chinese_detected(self) -> None:
        assert detect_language("今天群里聊什么") == "zh"

    def test_english_fallback(self) -> None:
        assert detect_language("hello everyone") == "en"

    def test_empty_returns_none(self) -> None:
        assert detect_language("", None) is None
        assert detect_language("123 456") is None

    def test_country_from_phone_prefix(self) -> None:
        assert detect_country("联系 +8613800138000") == "CN"
        assert detect_country("call +14155550100") == "US"

    def test_country_prefers_longest_prefix(self) -> None:
        """+852 是香港，不能被读成 +8 或 +85。"""
        assert detect_country("香港号码 +85261234567") == "HK"

    def test_country_without_phone_is_none(self) -> None:
        assert detect_country("没有电话的群简介") is None


def test_detect_categories_uses_dictionary() -> None:
    found = detect_categories("这是一个搜群导航，找群就来这里", "兼职招聘日结")

    assert "搜索导航" in found
    assert "求职招聘" in found


class TestMetrics:
    def test_human_ratio_excludes_bots_and_ads(self) -> None:
        samples = [
            message(1, "大家好啊", sender=1),
            message(2, "今天聊什么", sender=2),
            message(3, "专业推广加微信", sender=3),
            message(4, "我是机器人播报", sender=9, bot=True),
        ]
        metrics = compute_metrics(samples)

        assert metrics.sample_size == 4
        assert metrics.human_ratio == pytest.approx(0.5)
        assert metrics.bot_ratio == pytest.approx(0.25)
        assert metrics.unique_senders == 3

    def test_link_density_counts_messages_with_links(self) -> None:
        samples = [
            message(1, "t.me/aaa_group"),
            message(2, "https://t.me/bbb_group 看看"),
            message(3, "普通聊天"),
            message(4, "又一条普通聊天"),
        ]
        metrics = compute_metrics(samples)

        assert metrics.link_density == pytest.approx(50.0)

    def test_posts_per_day_uses_sample_span(self) -> None:
        # 10 条消息跨 5 天 → 2 条/天
        samples = [message(i, "聊天", minutes_ago=i * 720) for i in range(10)]
        metrics = compute_metrics(samples)

        assert metrics.posts_per_day == pytest.approx(2.1, abs=0.3)

    def test_short_span_does_not_inflate_posts_per_day(self) -> None:
        """半小时 100 条不能被算成一天几千条。"""
        samples = [message(i, "聊天", minutes_ago=i) for i in range(100)]
        metrics = compute_metrics(samples)

        assert metrics.posts_per_day == 100.0

    def test_last_active_is_newest_message(self) -> None:
        samples = [
            message(1, "早", minutes_ago=100),
            message(2, "晚", minutes_ago=5),
        ]
        metrics = compute_metrics(samples)

        assert metrics.last_active_at == BASE_TIME - timedelta(minutes=5)

    def test_lead_potential_uses_keyword_library(self) -> None:
        from app.core.keyword_matcher import KeywordEntry

        entries = [KeywordEntry(id=1, group_id=1, word="篮球")]
        samples = [message(i, "有人打篮球吗" if i < 5 else "今天天气不错") for i in range(20)]
        metrics = compute_metrics(samples, keyword_entries=entries)

        # 5/20 命中 → 25% × 5 = 125 → 封顶 100
        assert metrics.lead_potential == 100.0

    def test_lead_potential_is_zero_without_hits(self) -> None:
        from app.core.keyword_matcher import KeywordEntry

        entries = [KeywordEntry(id=1, group_id=1, word="篮球")]
        samples = [message(i, "今天天气不错") for i in range(10)]

        assert compute_metrics(samples, keyword_entries=entries).lead_potential == 0.0

    def test_empty_sample_only_infers_from_profile(self) -> None:
        metrics = compute_metrics([], title="中文搜索导航", about="找群就这里")

        assert metrics.sample_size == 0
        assert metrics.language == "zh"
        assert "搜索导航" in metrics.categories
        assert metrics.activity_score == 0.0


class TestIndexGroup:
    def test_three_features_marks_index_group(self) -> None:
        samples = [
            message(i, f"t.me/group{i}", sender=1000 + i, bot=True, minutes_ago=i * 30)
            for i in range(12)
        ]
        metrics = compute_metrics(
            samples,
            title="超级搜索",
            member_count=8000,
        )

        # 标题 + 短链接为主 + 机器人占比 + 成员数 + 活跃度
        assert metrics.index_score >= 3
        assert metrics.is_index_group is True
        assert "标题含搜索/导航类词" in metrics.index_reasons

    def test_normal_group_is_not_index_group(self) -> None:
        samples = [
            message(i, "大家晚上好啊", sender=200 + i, minutes_ago=i * 600) for i in range(5)
        ]
        metrics = compute_metrics(samples, title="老王家唠嗑群", member_count=80)

        assert metrics.is_index_group is False
        assert metrics.index_score < 3

    def test_thresholds_are_configurable(self) -> None:
        samples = [
            message(i, f"t.me/group{i}", sender=1000 + i, bot=True, minutes_ago=i * 30)
            for i in range(12)
        ]
        strict = ProbeThresholds(index_feature_threshold=5, index_member_threshold=999999)
        metrics = compute_metrics(samples, title="超级搜索", member_count=8000, thresholds=strict)

        # 成员数不达标、阈值抬到 5 项，就不再算索引型
        assert metrics.is_index_group is False
