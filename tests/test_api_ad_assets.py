"""广告素材库接口测试。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, auth_header


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def test_create_list_and_update_asset(admin_client) -> None:
    created = await admin_client.post(
        "/api/ad-assets",
        headers=_headers(),
        json={
            "name": "渠道A文案",
            "text": "{源名} · 每日更新",
            "link_url": "https://t.me/xxx?start=a",
            "link_text": "立即查看",
        },
    )
    body = created.json()
    assert created.status_code == 201
    assert body["reference_count"] == 0
    assert body["references"] == []
    assert body["enabled"] is True

    listing = await admin_client.get("/api/ad-assets", headers=_headers())
    assert listing.json()["total"] == 1
    assert listing.json()["items"][0]["link_text"] == "立即查看"

    updated = await admin_client.patch(
        f"/api/ad-assets/{body['id']}",
        headers=_headers(),
        json={"text": "新的文案", "enabled": False},
    )
    assert updated.status_code == 200
    assert updated.json()["text"] == "新的文案"
    assert updated.json()["enabled"] is False

    duplicate = await admin_client.post(
        "/api/ad-assets",
        headers=_headers(),
        json={"name": "渠道A文案", "text": "x"},
    )
    assert duplicate.status_code == 409


async def test_asset_requires_some_content(admin_client) -> None:
    empty = await admin_client.post(
        "/api/ad-assets",
        headers=_headers(),
        json={"name": "空素材"},
    )
    assert empty.status_code == 400
    assert "至少要填一项" in empty.json()["detail"]

    no_label = await admin_client.post(
        "/api/ad-assets",
        headers=_headers(),
        json={"name": "缺按钮文字", "link_url": "https://t.me/xxx"},
    )
    assert no_label.status_code == 400
    assert "按钮文字" in no_label.json()["detail"]

    image_only = await admin_client.post(
        "/api/ad-assets",
        headers=_headers(),
        json={"name": "纯图片素材", "image_path": "assets/uploads/a.jpg"},
    )
    assert image_only.status_code == 201


async def test_delete_asset_is_blocked_while_referenced(
    chat_client,
    fake_account_client,
) -> None:
    from conftest import make_entity

    source = make_entity(4001, "素材源", broadcast=True, username="ad_src")
    target = make_entity(4002, "主频道", broadcast=True, username="ad_main")
    fake_account_client.dialogs = [source, target]
    fake_account_client.entities = {"ad_src": source, "ad_main": target}
    await chat_client.post("/api/sources/sync", headers=_headers())

    pool = (await chat_client.get("/api/sources/available", headers=_headers())).json()["items"]
    source_id = next(item["id"] for item in pool if item["title"] == "素材源")
    await chat_client.post("/api/sources", headers=_headers(), json={"chat_ids": [source_id]})
    target_pool = (await chat_client.get("/api/targets/available", headers=_headers())).json()[
        "items"
    ]
    target_id = next(item["id"] for item in target_pool if item["title"] == "主频道")
    await chat_client.post("/api/targets", headers=_headers(), json={"chat_ids": [target_id]})

    asset = await chat_client.post(
        "/api/ad-assets",
        headers=_headers(),
        json={"name": "被引用的素材", "text": "广告词"},
    )
    asset_id = asset.json()["id"]

    route = await chat_client.post(
        "/api/routes",
        headers=_headers(),
        json={
            "name": "引用素材的线路",
            "source_chat_id": source_id,
            "business_type": "A",
            "target_chat_ids": [target_id],
            "a_config": {"ad_policy": "nth", "ad_asset_id": asset_id},
        },
    )
    assert route.status_code == 201

    detail = await chat_client.get(f"/api/ad-assets/{asset_id}", headers=_headers())
    assert detail.json()["reference_count"] == 1
    assert detail.json()["references"][0]["name"] == "引用素材的线路"

    blocked = await chat_client.delete(f"/api/ad-assets/{asset_id}", headers=_headers())
    assert blocked.status_code == 400
    assert "正在被 1 条线路引用" in blocked.json()["detail"]

    forced = await chat_client.delete(
        f"/api/ad-assets/{asset_id}?force=true",
        headers=_headers(),
    )
    assert forced.status_code == 200
    assert (await chat_client.get("/api/ad-assets", headers=_headers())).json()["total"] == 0
