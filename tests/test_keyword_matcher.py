"""关键词匹配：包含、别名、模糊与排除词。"""

from __future__ import annotations

from app.core.keyword_matcher import (
    KeywordEntry,
    best_fuzzy_score,
    match_text,
    normalize,
    parse_aliases,
)


def sport_entry() -> KeywordEntry:
    return KeywordEntry(
        id=1,
        group_id=1,
        word="体育",
        aliases=("篮球", "足球", "乒乓球"),
    )


def test_alias_covers_related_words() -> None:
    """体育 挂了篮球等别名，说篮球也算命中体育。"""
    hits = match_text("今晚有篮球赛吗，求推荐", [sport_entry()])

    assert len(hits) == 1
    assert hits[0].keyword == "体育"
    assert hits[0].matched == "篮球"
    assert hits[0].mode == "contains"
    assert hits[0].score == 1.0


def test_plain_word_still_matches() -> None:
    hits = match_text("聊聊体育吧", [sport_entry()])

    assert hits and hits[0].matched == "体育"


def test_unrelated_message_does_not_match() -> None:
    """别名表不是模型：没列过的词不会被"猜"出来，这正是可控的地方。"""
    assert match_text("求推荐一部电影", [sport_entry()]) == []


def test_exclude_keywords_drop_whole_message() -> None:
    entries = [sport_entry()]

    hits = match_text("我是客服机器人，聊聊体育", entries, exclude=("客服",))

    assert hits == []


def test_typo_matches_only_in_loose_mode() -> None:
    """四个字错一个字（相似度 0.75）：宽松与标准都能兜，严格不行。"""
    entries = [KeywordEntry(id=2, group_id=None, word="篮球比赛")]

    loose = match_text(
        "今晚篮球比塞谁看",
        entries,
        sensitivity="loose",
        match_contains=False,
        match_fuzzy=True,
    )
    strict = match_text(
        "今晚篮球比塞谁看",
        entries,
        sensitivity="strict",
        match_contains=False,
        match_fuzzy=True,
    )

    assert loose and loose[0].mode == "fuzzy"
    assert strict == []


def test_fuzzy_can_be_disabled() -> None:
    entries = [KeywordEntry(id=3, group_id=None, word="篮球")]

    assert match_text("篮球", entries, match_contains=True, match_fuzzy=False)
    assert match_text("蓝球", entries, match_contains=True, match_fuzzy=False) == []


def test_hits_sorted_by_score() -> None:
    entries = [
        KeywordEntry(id=4, group_id=None, word="篮球"),
        KeywordEntry(id=5, group_id=None, word="球"),
    ]

    hits = match_text("篮球", entries)

    assert [item.keyword for item in hits] == ["篮球", "球"]
    assert hits[0].score >= hits[1].score


def test_parse_aliases_and_normalize() -> None:
    assert parse_aliases("篮球, 足球、乒乓球\n羽毛球") == ("篮球", "足球", "乒乓球", "羽毛球")
    assert parse_aliases("") == ()
    assert normalize(" Nba  赛事\u200b ") == "nba赛事"


def test_best_fuzzy_score_handles_short_words() -> None:
    # 两个中文字一个不同 → 0.5，低于宽松档阈值，所以两字词要靠别名
    assert best_fuzzy_score("篮球", "蓝球") == 0.5
    assert best_fuzzy_score("篮球", "完全无关") < 0.5


def test_short_words_need_alias_instead_of_fuzzy() -> None:
    """「羽毛球 / 篮球」都只共用一个"球"字，模糊匹配不该把它们当同一件事。"""
    entries = [KeywordEntry(id=6, group_id=None, word="篮球")]

    assert best_fuzzy_score("篮球", "羽毛球") == 0.5
    assert (
        match_text(
            "买了副羽毛球拍",
            entries,
            sensitivity="loose",
            match_contains=False,
            match_fuzzy=True,
        )
        == []
    )
