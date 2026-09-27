"""冷触达发送记录与容量明细测试。"""

from __future__ import annotations

from datetime import UTC, datetime

from conftest import ADMIN_API_TOKEN, auth_header


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def _account(session, name: str, *, status: str = "active"):
    from app.db.models import TgAccount

    account = TgAccount(
        name=name,
        phone_masked="+86***0000",
        phone_enc="x",
        api_id_enc="x",
        api_hash_enc="x",
        session_name=f"record-{name}",
        status=status,
        purpose="outreach",
    )
    session.add(account)
    await session.flush()
    return account


async def test_records_include_content_and_recipient(admin_client) -> None:
    from app.db.models import OutreachContact, OutreachMessage, OutreachTask
    from app.db.session import session_scope

    async with session_scope() as session:
        account = await _account(session, "记录号")
        contact = OutreachContact(
            tg_user_id=880101,
            username="recipient_name",
            display_name="接收人",
            phone="13800001111",
            identity_confirmed=True,
        )
        session.add(contact)
        await session.flush()
        task = OutreachTask(
            contact_id=contact.id,
            kind="first_contact",
            status="SENT",
            account_id=account.id,
            trigger_type="manual",
            triggered_by="admin",
            rendered_text="你好，这是发送内容",
            target_message_id=991,
            dedupe_key=f"record-{contact.id}",
            sent_at=datetime(2026, 9, 28, 1, 0, tzinfo=UTC),
        )
        session.add(task)
        await session.flush()
        session.add(
            OutreachMessage(
                task_id=task.id,
                contact_id=contact.id,
                account_id=account.id,
                direction="out",
                message_kind="first_contact",
                tg_message_id=991,
                text="你好，这是发送内容",
                recipient_username="recipient_name",
                recipient_display_name="接收人",
                recipient_tg_user_id=880101,
                sent_at=datetime(2026, 9, 28, 1, 0, tzinfo=UTC),
            )
        )
        await session.commit()

    response = await admin_client.get(
        "/api/outreach/records",
        headers=_headers(),
        params={"status": "SENT"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    item = body["items"][0]
    assert item["content"] == "你好，这是发送内容"
    assert item["recipient_display_name"] == "接收人"
    assert item["recipient_username"] == "recipient_name"
    assert item["trigger_type"] == "manual"
    assert item["message_kind_label"] == "首触消息"

    exported = await admin_client.get(
        "/api/outreach/records.csv",
        headers=_headers(),
        params={"status": "SENT"},
    )
    assert exported.status_code == 200
    assert "你好，这是发送内容" in exported.text
    assert "recipient_name" in exported.text


async def test_records_can_filter_failed_task(admin_client) -> None:
    from app.db.models import OutreachContact, OutreachTask
    from app.db.session import session_scope

    async with session_scope() as session:
        account = await _account(session, "失败号")
        contact = OutreachContact(tg_user_id=880102, username="failed_target")
        session.add(contact)
        await session.flush()
        session.add(
            OutreachTask(
                contact_id=contact.id,
                kind="first_contact",
                status="FAILED",
                account_id=account.id,
                rendered_text="失败尝试内容",
                last_error="network timeout",
                dedupe_key=f"failed-{contact.id}",
            )
        )
        await session.commit()

    response = await admin_client.get(
        "/api/outreach/records",
        headers=_headers(),
        params={"status": "FAILED"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["content"] == "失败尝试内容"


async def test_capacity_excludes_pending_login_account(admin_client) -> None:
    from app.db.models import OutreachAccountDaily, OutreachAccountState
    from app.db.session import session_scope
    from app.services import outreach_account_service, outreach_queue_service

    async with session_scope() as session:
        active = await _account(session, "可用号")
        pending = await _account(session, "未登录号", status="pending_login")
        session.add(
            OutreachAccountState(
                account_id=active.id,
                tenant_id=active.tenant_id,
                tier="NEW",
                state="READY",
            )
        )
        session.add(
            OutreachAccountDaily(
                account_id=active.id,
                tenant_id=active.tenant_id,
                day=outreach_account_service.local_day(tz_name="Asia/Shanghai"),
                first_contact_sent=1,
            )
        )
        await session.commit()
        active_id = active.id
        pending_id = pending.id

        capacity = await outreach_queue_service.capacity(session, tenant_id=1)

    assert capacity["today_available"] == 2
    assert [item["id"] for item in capacity["account_details"]] == [active_id]
    assert pending_id in [item["id"] for item in capacity["excluded_accounts"]]
