"""控制 Bot 接口测试。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, auth_header

VALID_TOKEN = "valid-123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def _payload(**overrides) -> dict:
    payload = {
        "name": "机器人A",
        "token": VALID_TOKEN,
        "admin_ids": [123456789],
        "note": "短剧类线路用",
    }
    payload.update(overrides)
    return payload


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def test_create_bot_validates_token_and_hides_it(bot_client) -> None:
    response = await bot_client.post("/api/bots", headers=_headers(), json=_payload())

    body = response.json()
    assert response.status_code == 201
    assert body["name"] == "机器人A"
    assert body["bot_username"].endswith("_bot")
    assert body["bot_telegram_id"] > 0
    assert body["has_token"] is True
    assert body["is_default"] is False
    assert body["enabled"] is True
    assert body["admin_ids"] == [123456789]
    assert "token" not in body
    assert VALID_TOKEN not in response.text


async def test_invalid_token_is_rejected(bot_client) -> None:
    response = await bot_client.post(
        "/api/bots",
        headers=_headers(),
        json=_payload(token="bad-123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ"),
    )

    assert response.status_code == 400
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "校验失败" in response.json()["detail"]

    listing = await bot_client.get("/api/bots", headers=_headers())
    assert listing.json()["total"] == 0


async def test_malformed_token_is_rejected_without_calling_telegram(bot_client) -> None:
    response = await bot_client.post(
        "/api/bots",
        headers=_headers(),
        json=_payload(token="this-token-has-no-colon-at-all-1234"),
    )

    assert response.status_code == 400
    assert "格式不正确" in response.json()["detail"]


async def test_update_bot_can_rotate_token_and_flip_default(bot_client) -> None:
    first = await bot_client.post("/api/bots", headers=_headers(), json=_payload(is_default=True))
    second = await bot_client.post(
        "/api/bots",
        headers=_headers(),
        json=_payload(name="机器人B", token="valid-987654:ZYXWVUTSRQPONMLKJIHGFEDCBA"),
    )
    first_id = first.json()["id"]
    second_id = second.json()["id"]

    rotated = await bot_client.patch(
        f"/api/bots/{second_id}",
        headers=_headers(),
        json={
            "token": "valid-555555:AAAAAAAAAAAAAAAAAAAAAAAA",
            "enabled": False,
            "admin_ids": [111, 222],
            "is_default": True,
        },
    )
    body = rotated.json()
    assert rotated.status_code == 200
    assert body["enabled"] is False
    assert body["is_default"] is True
    assert body["admin_ids"] == [111, 222]

    listing = await bot_client.get("/api/bots", headers=_headers())
    items = {item["id"]: item for item in listing.json()["items"]}
    assert items[first_id]["is_default"] is False
    assert items[second_id]["is_default"] is True

    deleted = await bot_client.delete(f"/api/bots/{first_id}", headers=_headers())
    assert deleted.status_code == 200


async def test_duplicate_bot_name_is_rejected(bot_client) -> None:
    await bot_client.post("/api/bots", headers=_headers(), json=_payload())

    duplicate = await bot_client.post("/api/bots", headers=_headers(), json=_payload())

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "USER_EXISTS"
