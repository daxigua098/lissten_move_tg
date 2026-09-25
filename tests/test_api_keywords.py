"""关键词组接口测试：预置别名库、增删改与试跑。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, auth_header


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def test_seed_then_crud_and_match(admin_client) -> None:
    seeded = await admin_client.post("/api/keyword-groups/seed", headers=_headers())
    body = seeded.json()
    assert seeded.status_code == 200
    assert body["created"] >= 3
    names = [item["name"] for item in body["items"]]
    assert "体育赛事" in names

    sport = next(item for item in body["items"] if item["name"] == "体育赛事")
    keyword = next(item for item in sport["keywords"] if item["word"] == "体育")
    assert "篮球" in keyword["alias_list"]

    # 再点一次不会重复导入
    again = await admin_client.post("/api/keyword-groups/seed", headers=_headers())
    assert again.json()["created"] == 0

    created = await admin_client.post(
        "/api/keyword-groups",
        headers=_headers(),
        json={"name": "自定义组"},
    )
    assert created.status_code == 201
    group_id = created.json()["id"]

    duplicate = await admin_client.post(
        "/api/keyword-groups",
        headers=_headers(),
        json={"name": "自定义组"},
    )
    assert duplicate.status_code == 409

    added = await admin_client.post(
        f"/api/keyword-groups/{group_id}/keywords",
        headers=_headers(),
        json={"group_id": group_id, "word": "求片", "aliases": "求资源,谁有"},
    )
    assert added.status_code == 201
    keyword_id = added.json()["id"]
    assert added.json()["alias_list"] == ["求资源", "谁有"]

    patched = await admin_client.patch(
        f"/api/keywords/{keyword_id}",
        headers=_headers(),
        json={"aliases": "求资源"},
    )
    assert patched.json()["alias_list"] == ["求资源"]

    match = await admin_client.post(
        "/api/keyword-groups/match",
        headers=_headers(),
        json={"text": "今晚有篮球赛吗，求推荐", "group_ids": []},
    )
    hits = match.json()["hits"]
    assert any(item["keyword"] == "体育" and item["matched"] == "篮球" for item in hits)

    removed = await admin_client.delete(f"/api/keyword-groups/{group_id}", headers=_headers())
    assert removed.json()["deleted"] is True
    remaining = (await admin_client.get("/api/keyword-groups", headers=_headers())).json()["items"]
    assert all(item["id"] != group_id for item in remaining)


async def test_missing_group_returns_404(admin_client) -> None:
    response = await admin_client.patch(
        "/api/keyword-groups/9999",
        headers=_headers(),
        json={"name": "改名"},
    )

    assert response.status_code == 404


async def test_merge_rule_group_and_hot_keyword_ranking(admin_client) -> None:
    """归并规则：热门词按同类说法合并，并把变体一起写进词库别名。"""
    from app.db.session import session_scope
    from app.services import hot_keyword_service

    group = await admin_client.post(
        "/api/keyword-groups",
        headers=_headers(),
        json={"name": "同类词归并", "kind": "merge"},
    )
    assert group.status_code == 201
    group_id = group.json()["id"]
    await admin_client.post(
        f"/api/keyword-groups/{group_id}/keywords",
        headers=_headers(),
        json={"group_id": group_id, "word": "微信", "aliases": "加我微信,微信同号"},
    )

    async with session_scope() as session:
        # 同一条消息里重复出现只算一次（按消息计），跨消息才累加
        await hot_keyword_service.collect_message(
            session,
            text="加我微信 加我微信 微信同号",
            source_chat_id=1,
            source_title="搜索群",
        )
        await hot_keyword_service.collect_message(
            session,
            text="加我微信 再说一次",
            source_chat_id=1,
            source_title="搜索群",
        )

    listing = (await admin_client.get("/api/hot-keywords", headers=_headers())).json()
    top = listing["items"][0]
    assert top["token"] == "微信"
    assert top["merged"] is True
    assert top["count"] == 3
    assert {item["token"] for item in top["variants"]} == {"加我微信", "微信同号"}

    # 加进关键词组：归类名当主词，变体写成别名
    keyword_group = await admin_client.post(
        "/api/keyword-groups",
        headers=_headers(),
        json={"name": "联系方式", "kind": "keyword"},
    )
    promoted = await admin_client.post(
        "/api/hot-keywords/promote",
        headers=_headers(),
        json={
            "token": top["token"],
            "group_id": keyword_group.json()["id"],
            "aliases": [item["token"] for item in top["variants"]],
        },
    )
    assert promoted.status_code == 201
    assert promoted.json()["added"] is True

    detail = (
        await admin_client.get(
            "/api/keyword-groups?kind=keyword",
            headers=_headers(),
        )
    ).json()["items"]
    created = next(item for item in detail if item["name"] == "联系方式")
    keyword = next(item for item in created["keywords"] if item["word"] == "微信")
    assert set(keyword["alias_list"]) == {"加我微信", "微信同号"}


async def test_exclude_groups_are_separate_kind(admin_client) -> None:
    """排除词组与关键词组分开放，互不混入匹配。"""
    await admin_client.post("/api/keyword-groups/seed", headers=_headers())

    keyword_only = (
        await admin_client.get("/api/keyword-groups?kind=keyword", headers=_headers())
    ).json()["items"]
    exclude_only = (
        await admin_client.get("/api/keyword-groups?kind=exclude", headers=_headers())
    ).json()["items"]

    assert all(item["kind"] == "keyword" for item in keyword_only)
    assert exclude_only and all(item["kind"] == "exclude" for item in exclude_only)
    names = [item["name"] for item in exclude_only]
    assert "通用噪声（排除）" in names

    created = await admin_client.post(
        "/api/keyword-groups",
        headers=_headers(),
        json={"name": "手机号贩子（排除）", "kind": "exclude"},
    )
    assert created.status_code == 201
    assert created.json()["kind"] == "exclude"
    group_id = created.json()["id"]
    await admin_client.post(
        f"/api/keyword-groups/{group_id}/keywords",
        headers=_headers(),
        json={"group_id": group_id, "word": "卡商", "aliases": "号商,卖号"},
    )

    # 排除词组里的词只用于"挡"，不会当成关键词去命中
    match = await admin_client.post(
        "/api/keyword-groups/match",
        headers=_headers(),
        json={"text": "卡商 号商 卖号", "group_ids": []},
    )
    assert all(hit["keyword"] != "卡商" for hit in match.json()["hits"])

    # 但作为排除词传入时，整条会被忽略
    blocked = await admin_client.post(
        "/api/keyword-groups/match",
        headers=_headers(),
        json={"text": "卡商出货 顺便聊聊篮球", "exclude_group_ids": [group_id]},
    )
    assert blocked.json()["exclude_total"] >= 3
    assert blocked.json()["hits"] == []
