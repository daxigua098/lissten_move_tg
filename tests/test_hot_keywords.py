"""热门关键词：采词规则、累计排名与加入词库。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN

from app.core.hot_words import (
    drop_overlapping_duplicates,
    drop_substring_duplicates,
    extract_tokens,
)


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {ADMIN_API_TOKEN}"}


def test_extract_tokens_keeps_meaningful_words() -> None:
    tokens = extract_tokens("今晚有篮球赛吗，想找人一起看 求推荐")

    assert "篮球" in tokens
    assert "篮球赛" in tokens
    # 停用词与口水话不进来
    assert "今晚" not in tokens
    assert "一起" not in tokens
    assert all(len(token) >= 2 for token in tokens)
    # 同一条消息里重复出现的词只算一次
    assert extract_tokens("加我微信 加我微信").count("加我微信") == 1


def test_extract_tokens_drops_links_contacts_and_numbers() -> None:
    text = "加我 https://ad.example.com 微信 abc12345 电话 13800138000 @seller_ok 12345"

    tokens = extract_tokens(text)

    assert all("http" not in token for token in tokens)
    assert all("@" not in token for token in tokens)
    assert "13800138000" not in tokens
    assert "12345" not in tokens


def test_extract_tokens_keeps_latin_words() -> None:
    tokens = extract_tokens("求 NBA 直播地址 nba 集锦")

    assert "nba" in tokens


def test_drop_substring_duplicates_prefers_longer_word() -> None:
    kept = drop_substring_duplicates({"抖音": 3, "抖音号": 3, "日本": 5})

    assert "抖音号" in kept
    assert "抖音" not in kept
    assert "日本" in kept


def test_drop_overlapping_duplicates_merges_sliding_fragments() -> None:
    """n-gram 切出来的重叠碎片只留一个。"""
    kept = drop_overlapping_duplicates(
        {"喜欢交朋": 3, "欢交朋友": 3, "快手": 2},
    )

    assert len([token for token in kept if token in {"喜欢交朋", "欢交朋友"}]) == 1
    assert "快手" in kept


def test_merge_variants_groups_similar_words() -> None:
    """同类说法归到一个名字下：微信 = 加我微信 + 微信同号。"""
    from app.services.hot_keyword_service import merge_variants

    merged = merge_variants(
        {"加我微信": 8, "微信同号": 4, "快手": 3},
        [("微信", ["加我微信", "微信同号", "vx"])],
    )

    assert merged["微信"]["count"] == 12
    assert merged["微信"]["merged"] is True
    assert merged["微信"]["variants"] == {"加我微信": 8, "微信同号": 4}
    # 没被规则覆盖的词保持原样
    assert merged["快手"]["count"] == 3
    assert merged["快手"]["merged"] is False


def test_merge_variants_keeps_rule_name_without_exact_match() -> None:
    """只说"微信同号"、没人直接说"微信"时，也要归到"微信"这一条。"""
    from app.services.hot_keyword_service import merge_variants

    merged = merge_variants({"微信同号": 5}, [("微信", ["微信同号"])])

    assert merged["微信"]["count"] == 5


async def test_hot_keywords_are_counted_and_ranked(admin_client, api_config) -> None:
    from app.db.session import session_scope
    from app.services import hot_keyword_service

    async with session_scope() as session:
        await hot_keyword_service.collect_message(
            session,
            text="求抖音号 抖音号 有没有",
            source_chat_id=1,
            source_title="搜索群",
        )
        await hot_keyword_service.collect_message(
            session,
            text="抖音号 还有吗",
            source_chat_id=1,
            source_title="搜索群",
        )

    listing = await admin_client.get("/api/hot-keywords", headers=_headers())
    body = listing.json()
    assert listing.status_code == 200
    top = body["items"][0]
    assert top["rank"] == 1
    assert top["token"] == "抖音号"
    assert top["count"] >= 2
    assert "搜索群" in top["sources"]
    # 长词优先：短词「抖音」被同频次的长词合并掉
    assert all(item["token"] != "抖音" for item in body["items"])

    stats = (await admin_client.get("/api/hot-keywords/stats", headers=_headers())).json()
    assert stats["tokens"] >= 1
    assert stats["occurrences"] >= 2


async def test_hot_keyword_can_be_promoted_into_library(admin_client, api_config) -> None:
    from app.db.session import session_scope
    from app.services import hot_keyword_service

    async with session_scope() as session:
        await hot_keyword_service.collect_message(
            session,
            text="求个篮球赛推荐",
            source_chat_id=1,
            source_title="搜索群",
        )

    await admin_client.post(
        "/api/keyword-groups",
        headers=_headers(),
        json={"name": "测试关键词组", "kind": "keyword"},
    )
    groups = (await admin_client.get("/api/keyword-groups?kind=keyword", headers=_headers())).json()
    group_id = groups["items"][0]["id"]
    token = (await admin_client.get("/api/hot-keywords", headers=_headers())).json()["items"][0][
        "token"
    ]

    promoted = await admin_client.post(
        "/api/hot-keywords/promote",
        headers=_headers(),
        json={"token": token, "group_id": group_id},
    )
    assert promoted.status_code == 201
    assert promoted.json()["added"] is True

    # 再加一次：已经存在，不报错
    again = await admin_client.post(
        "/api/hot-keywords/promote",
        headers=_headers(),
        json={"token": token, "group_id": group_id},
    )
    assert again.json()["added"] is False
