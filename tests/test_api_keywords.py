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
