"""冷触达 P5：7 日指标与自动降档、账号退役护栏、冷触达额度。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, auth_header


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def _account(
    session, *, name: str = "冷聊号", tier: str = "MATURE", tg_user_id_prefix: int = 91
):
    from app.db.models import OutreachAccountState, TgAccount

    account = TgAccount(
        name=name,
        phone_masked="+86***1111",
        phone_enc="x",
        api_id_enc="x",
        api_hash_enc="x",
        session_name=name,
        status="active",
        purpose="outreach",
    )
    session.add(account)
    await session.flush()
    session.add(
        OutreachAccountState(
            account_id=account.id,
            tenant_id=account.tenant_id,
            tier=tier,
            state="READY",
        )
    )
    await session.commit()
    await session.refresh(account)
    return account


async def _contact(session, account, *, tg_user_id: int, replied: bool = False):
    from app.core.outreach_capture import OUTREACH_REPLIED
    from app.db.base import utc_now
    from app.db.models import OutreachContact

    contact = OutreachContact(
        tg_user_id=tg_user_id,
        identity_confirmed=True,
        first_contact_at=utc_now(),
        first_contact_account_id=account.id,
        last_inbound_at=utc_now() if replied else None,
        contact_state=OUTREACH_REPLIED if replied else "CONTACTED",
        owner_account_id=account.id,
    )
    session.add(contact)
    await session.commit()
    return contact


async def test_metrics_downgrade_needs_enough_samples(admin_client) -> None:
    from app.db.models import OutreachAccountState
    from app.db.session import session_scope
    from app.services import outreach_metrics_service as metrics

    async with session_scope() as session:
        account = await _account(session, name="少量样本", tier="MATURE")
        for index in range(3):
            await _contact(session, account, tg_user_id=910000 + index)

        result = await metrics.refresh(session, account)

        state = await session.get(OutreachAccountState, account.id)
        assert result["reply_rate_7d"] == 0.0
        assert result["downgraded_to"] is None
        assert state is not None and state.tier == "MATURE"


async def test_metrics_auto_downgrade_when_reply_rate_low(admin_client) -> None:
    from app.db.base import utc_now
    from app.db.models import OutreachAccountState, OutreachTask
    from app.db.session import session_scope
    from app.services import outreach_metrics_service as metrics

    async with session_scope() as session:
        account = await _account(session, name="低回复号", tier="MATURE")
        for index in range(12):
            await _contact(session, account, tg_user_id=920000 + index)
        # 该账号发过 2 条成功任务：成功率 100%
        for index in range(2):
            session.add(
                OutreachTask(
                    tenant_id=account.tenant_id,
                    contact_id=1 + index,
                    kind="first_contact",
                    status="SENT",
                    account_id=account.id,
                    dedupe_key=f"metric-{account.id}-{index}",
                    sent_at=utc_now(),
                )
            )
        await session.commit()

        result = await metrics.refresh(session, account)

        state = await session.get(OutreachAccountState, account.id)
        assert result["success_rate_7d"] == 1.0
        assert result["downgraded_to"] == "STANDARD"
        assert state is not None and state.tier == "STANDARD"
        assert state.active_conversation_count == 12


async def test_retire_freezes_conversations_and_recycles_tasks(admin_client) -> None:
    from app.db.models import OutreachTask
    from app.db.session import session_scope
    from app.services import outreach_account_service

    async with session_scope() as session:
        account = await _account(session, name="要退役的号")
        contact = await _contact(session, account, tg_user_id=930001, replied=True)
        task = OutreachTask(
            tenant_id=account.tenant_id,
            contact_id=contact.id,
            kind="follow_up",
            status="ASSIGNED",
            account_id=account.id,
            dedupe_key=f"retire-{account.id}",
        )
        session.add(task)
        await session.commit()

        summary = await outreach_account_service.retire(session, account, reason="limited")

        await session.refresh(contact)
        await session.refresh(task)
        await session.refresh(account)
        assert summary["frozen_contacts"] == 1
        assert summary["recycled_tasks"] == 1
        assert contact.contact_state == "FROZEN"
        assert task.status == "QUEUED" and task.account_id is None
        assert account.status == "disabled"
        assert account.retired_at is not None
        assert account.retire_reason == "limited"


async def test_hard_retire_moves_session_file(admin_client, tmp_path) -> None:
    from sqlalchemy import select

    from app.db.models import TgAccount
    from app.db.session import session_scope
    from app.services import outreach_account_service

    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    session_file = session_dir / "cold.session"
    session_file.write_text("x", encoding="utf-8")

    async with session_scope() as session:
        account = await _account(session, name="硬删号")
        account_id = account.id

        summary = await outreach_account_service.retire(
            session,
            account,
            reason="banned",
            hard=True,
            delete_session=True,
            session_path=session_file,
        )

        assert summary["hard"] is True
        assert summary["session_moved"] == "cold.session"
        assert not session_file.exists()
        assert (tmp_path / "session_trash" / "cold.session").exists()
        remaining = await session.scalar(select(TgAccount).where(TgAccount.id == account_id))
        assert remaining is None


async def test_batch_retire_api_skips_listen_accounts(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import tg_account_service

    outreach_ids = []
    for index in range(2):
        created = await admin_client.post(
            "/api/accounts",
            headers=_headers(),
            json={
                "name": f"批量冷聊{index}",
                "phone": f"+86138000011{index:02d}",
                "api_id": 123456,
                "api_hash": "abcdef0123456789abcdef0123456789",
                "purpose": "outreach",
                "owner_confirmed": True,
            },
        )
        assert created.status_code == 201
        outreach_ids.append(created.json()["id"])

    async with session_scope() as session:
        listen = await tg_account_service.create_account(
            session,
            admin_client._transport.app.state.config,
            name="监听号",
            phone="+8613900001111",
            api_id=123456,
            api_hash="abcdef0123456789abcdef0123456789",
        )
        listen_id = listen.id

    response = await admin_client.post(
        "/api/outreach/accounts/batch-retire",
        headers=_headers(),
        json={"account_ids": [*outreach_ids, listen_id], "reason": "batch_disable"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["hard"] is False

    listing = await admin_client.get(
        "/api/accounts",
        headers=_headers(),
        params={"purpose": "outreach"},
    )
    assert listing.json()["total"] == 0  # 退役账号不再出现在可用列表口径里


async def test_outreach_account_quota(admin_client) -> None:
    from app.db.models import TenantLimit
    from app.db.session import session_scope

    async with session_scope() as session:
        session.add(TenantLimit(tenant_id=1, max_outreach_accounts=1))
        await session.commit()

    payload = {
        "name": "额度冷聊1",
        "phone": "+8613800002222",
        "api_id": 123456,
        "api_hash": "abcdef0123456789abcdef0123456789",
        "purpose": "outreach",
        "owner_confirmed": True,
    }
    first = await admin_client.post("/api/accounts", headers=_headers(), json=payload)
    assert first.status_code == 201

    second = await admin_client.post(
        "/api/accounts",
        headers=_headers(),
        json={**payload, "name": "额度冷聊2", "phone": "+8613800003333"},
    )
    assert second.status_code == 400
    assert "上限" in second.json()["detail"]


def test_outreach_is_a_module() -> None:
    from app.db.models import MODULE_LABELS, MODULES

    assert "outreach" in MODULES
    assert MODULE_LABELS["outreach"] == "冷触达"
