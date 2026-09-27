"""冷触达：勾选参与发送的发信息账号，且策略照旧生效。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, auth_header


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def _account(admin_client, api_config, *, name: str, phone: str, status: str = "active"):
    from app.db.session import session_scope
    from app.services import tg_account_service

    async with session_scope() as session:
        account = await tg_account_service.create_account(
            session,
            api_config,
            name=name,
            phone=phone,
            purpose="outreach",
            owner_confirmed=True,
        )
        account_id = account.id
        account.status = status
        await session.commit()
    return account_id


async def test_participation_toggle_sets_ready_and_paused(admin_client, api_config) -> None:
    from app.db.models import OutreachAccountState
    from app.db.session import session_scope

    account_id = await _account(admin_client, api_config, name="参与号", phone="+14135030718")

    on = await admin_client.post(
        "/api/outreach/accounts/participation",
        headers=_headers(),
        json={"account_ids": [account_id], "enabled": True},
    )
    assert on.status_code == 200
    assert on.json()["updated"] == [account_id]

    async with session_scope() as session:
        state = await session.get(OutreachAccountState, account_id)
        assert state is not None and state.state == "READY"

    off = await admin_client.post(
        "/api/outreach/accounts/participation",
        headers=_headers(),
        json={"account_ids": [account_id], "enabled": False},
    )
    assert off.status_code == 200

    async with session_scope() as session:
        state = await session.get(OutreachAccountState, account_id)
        assert state is not None and state.state == "PAUSED"


async def test_participation_rejects_unlogged_and_limited(admin_client, api_config) -> None:
    from app.db.session import session_scope
    from app.services import outreach_account_service, tg_account_service

    pending_id = await _account(
        admin_client,
        api_config,
        name="没登录",
        phone="+17347499052",
        status="pending_login",
    )
    limited_id = await _account(admin_client, api_config, name="被限制", phone="+12125550001")
    async with session_scope() as session:
        account = await tg_account_service.get_account(session, limited_id)
        state = await outreach_account_service.get_or_create_state(session, account)
        state.state = "LIMITED"
        await session.commit()

    response = await admin_client.post(
        "/api/outreach/accounts/participation",
        headers=_headers(),
        json={"account_ids": [pending_id, limited_id], "enabled": True},
    )

    body = response.json()
    assert body["updated"] == []
    reasons = " ".join(item["reason"] for item in body["skipped"])
    assert "还没登录" in reasons
    assert "不能参与" in reasons


async def test_only_participating_accounts_are_used(admin_client, api_config) -> None:
    """取消勾选 = 暂停：调度器不会再挑它；重新勾选后才可用。"""
    from app.db.session import session_scope
    from app.services import outreach_sender_service

    account_id = await _account(admin_client, api_config, name="开关号", phone="+14135030718")

    await admin_client.post(
        "/api/outreach/accounts/participation",
        headers=_headers(),
        json={"account_ids": [account_id], "enabled": True},
    )
    async with session_scope() as session:
        picked = await outreach_sender_service.pick_account(session, 1)
        assert picked is not None and picked.id == account_id

    await admin_client.post(
        "/api/outreach/accounts/participation",
        headers=_headers(),
        json={"account_ids": [account_id], "enabled": False},
    )
    async with session_scope() as session:
        assert await outreach_sender_service.pick_account(session, 1) is None


async def test_strategy_still_limits_participating_account(admin_client, api_config) -> None:
    """勾选参与不等于不受限制：额度用尽照样挑不到它。"""
    from app.db.session import session_scope
    from app.services import (
        outreach_account_service,
        outreach_sender_service,
        tg_account_service,
    )

    account_id = await _account(admin_client, api_config, name="额度号", phone="+17347499052")
    await admin_client.post(
        "/api/outreach/accounts/participation",
        headers=_headers(),
        json={"account_ids": [account_id], "enabled": True},
    )

    async with session_scope() as session:
        account = await tg_account_service.get_account(session, account_id)
        assert await outreach_sender_service.pick_account(session, 1) is not None
        # 新号档位每日 3 条：把今天发满
        daily = await outreach_account_service.get_or_create_daily(session, account)
        daily.first_contact_sent = 3
        await session.commit()

    async with session_scope() as session:
        assert await outreach_sender_service.pick_account(session, 1) is None
