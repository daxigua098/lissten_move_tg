"""发信息账号池：用途隔离、归属确认与运营态快照。"""

from __future__ import annotations

import pytest
from conftest import ADMIN_API_TOKEN, auth_header


def _payload(**overrides) -> dict:
    payload = {
        "name": "监听主号",
        "phone": "+8613800001111",
        "api_id": 123456,
        "api_hash": "abcdef0123456789abcdef0123456789",
    }
    payload.update(overrides)
    return payload


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def test_purpose_filter_keeps_pools_separate(admin_client) -> None:
    listen = await admin_client.post(
        "/api/accounts",
        headers=_headers(),
        json=_payload(is_default=True),
    )
    outreach = await admin_client.post(
        "/api/accounts",
        headers=_headers(),
        json=_payload(
            name="冷聊号A",
            phone="+8613900002222",
            purpose="outreach",
            owner_confirmed=True,
            is_default=True,
        ),
    )
    assert listen.status_code == outreach.status_code == 201

    listen_items = (
        await admin_client.get("/api/accounts", headers=_headers(), params={"purpose": "listen"})
    ).json()["items"]
    outreach_items = (
        await admin_client.get("/api/accounts", headers=_headers(), params={"purpose": "outreach"})
    ).json()["items"]

    assert [item["name"] for item in listen_items] == ["监听主号"]
    assert [item["name"] for item in outreach_items] == ["冷聊号A"]
    # 执行账号与发信息账号各有一个默认账号，互不影响
    assert [item["is_default"] for item in listen_items] == [True]
    assert [item["is_default"] for item in outreach_items] == [True]

    from app.db.session import session_scope
    from app.services import tg_account_service

    async with session_scope() as session:
        default_listen = await tg_account_service.get_default_account(session)
        assert default_listen is not None and default_listen.name == "监听主号"
        default_outreach = await tg_account_service.get_default_account(
            session,
            purpose="outreach",
        )
        assert default_outreach is not None and default_outreach.name == "冷聊号A"


async def test_outreach_account_requires_owner_confirmation(admin_client) -> None:
    response = await admin_client.post(
        "/api/accounts",
        headers=_headers(),
        json=_payload(name="冷聊号B", purpose="outreach"),
    )

    assert response.status_code == 400
    assert "确认" in response.json()["detail"]


async def test_outreach_snapshot_and_state_update(admin_client) -> None:
    created = await admin_client.post(
        "/api/accounts",
        headers=_headers(),
        json=_payload(name="冷聊号C", purpose="outreach", owner_confirmed=True),
    )

    body = created.json()
    assert created.status_code == 201
    assert body["purpose"] == "outreach"
    assert body["purpose_label"] == "发信息账号"
    assert body["owner_confirmed_at"] is not None
    assert body["owner_confirmed_by"]
    snapshot = body["outreach"]
    assert snapshot["tier"] == "NEW"
    assert snapshot["daily_cap"] == 3
    assert snapshot["remaining"] == 3
    assert snapshot["state"] == "NEW"

    account_id = body["id"]
    updated = await admin_client.patch(
        f"/api/accounts/{account_id}",
        headers=_headers(),
        json={"outreach_tier": "STANDARD", "outreach_state": "READY"},
    )
    assert updated.status_code == 200
    assert updated.json()["outreach"]["tier"] == "STANDARD"
    assert updated.json()["outreach"]["daily_cap"] == 10
    assert updated.json()["outreach"]["state_label"] == "可用"


async def test_outreach_state_only_for_outreach_accounts(admin_client) -> None:
    listen = await admin_client.post("/api/accounts", headers=_headers(), json=_payload())

    response = await admin_client.patch(
        f"/api/accounts/{listen.json()['id']}",
        headers=_headers(),
        json={"outreach_state": "READY"},
    )

    assert response.status_code == 400
    assert "发信息账号" in response.json()["detail"]


async def test_route_owner_refs_reject_outreach_account(admin_client) -> None:
    created = await admin_client.post(
        "/api/accounts",
        headers=_headers(),
        json=_payload(name="冷聊号D", purpose="outreach", owner_confirmed=True),
    )
    account_id = created.json()["id"]

    from app.core.errors import ValidationFailedError
    from app.db.session import session_scope
    from app.services import route_service

    async with session_scope() as session:
        with pytest.raises(ValidationFailedError):
            await route_service._validate_owner_refs(session, account_id, None)
