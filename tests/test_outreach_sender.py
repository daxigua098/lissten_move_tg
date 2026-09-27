"""冷触达 P2：发送执行、错误映射、回复归属与免打扰。"""

from __future__ import annotations

from types import SimpleNamespace

from conftest import ADMIN_API_TOKEN, auth_header
from sqlalchemy import func, select


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


class FakeOutreachClient:
    """Telethon 替身：只实现冷触达用到的 get_entity / send_message。"""

    def __init__(self, *, fail: BaseException | None = None) -> None:
        self.fail = fail
        self.sent: list[tuple[object, str]] = []
        self.disconnected = False

    async def get_entity(self, identifier):
        return SimpleNamespace(id=int(identifier) if not isinstance(identifier, str) else 555)

    async def send_message(self, entity, text, **kwargs):
        if self.fail is not None:
            raise self.fail
        self.sent.append((entity, text))
        return SimpleNamespace(id=1000 + len(self.sent))

    async def disconnect(self) -> None:
        self.disconnected = True


class FloodWaitError(Exception):
    seconds = 120


class PeerFloodError(Exception):
    pass


async def _prepare(session, *, tg_user_id: int = 880001):
    """建一套可发送的账号 + 档位 + 话术 + 联系人 + 任务。"""
    from app.db.base import utc_now
    from app.db.models import (
        OutreachAccountState,
        OutreachContact,
        OutreachTask,
        OutreachTemplate,
        TgAccount,
    )

    account = TgAccount(
        name="冷聊号",
        phone_masked="+86***1111",
        phone_enc="x",
        api_id_enc="x",
        api_hash_enc="x",
        session_name="cold",
        status="active",
        purpose="outreach",
    )
    session.add(account)
    await session.flush()
    session.add(
        OutreachAccountState(
            account_id=account.id,
            tenant_id=account.tenant_id,
            tier="STANDARD",
            state="READY",
        )
    )
    session.add(
        OutreachTemplate(
            tenant_id=account.tenant_id,
            name="首条确认",
            kind="first_contact",
            text="你好 {称呼}，想确认一下你是否愿意了解。",
        )
    )
    contact = OutreachContact(
        tg_user_id=tg_user_id,
        username="seller01",
        display_name="卖家一号",
        identity_confirmed=True,
    )
    session.add(contact)
    await session.flush()
    task = OutreachTask(
        contact_id=contact.id,
        kind="first_contact",
        status="QUEUED",
        dedupe_key=f"first:{contact.id}",
        scheduled_at=utc_now(),
    )
    task.tenant_id = account.tenant_id
    session.add(task)
    await session.commit()
    await session.refresh(account)
    await session.refresh(contact)
    await session.refresh(task)
    return account, contact, task


def test_detect_refusal_and_classify_error() -> None:
    from app.services import outreach_sender_service as sender

    assert sender.detect_refusal("不要再联系我了") is True
    assert sender.detect_refusal("stop please") is True
    assert sender.detect_refusal("你好呀") is False
    assert sender.classify_error(FloodWaitError()) == "flood"
    assert sender.classify_error(PeerFloodError()) == "peer_flood"
    assert sender.classify_error(TimeoutError("timed out")) == "uncertain"
    assert sender.classify_error(ValueError("boom")) == "unknown"


async def test_send_task_updates_contact_account_and_logs(admin_client) -> None:
    from app.db.base import utc_now
    from app.db.models import OutreachAccountDaily, OutreachMessage
    from app.db.session import session_scope
    from app.services import outreach_sender_service as sender

    async with session_scope() as session:
        account, contact, task = await _prepare(session)
        client = FakeOutreachClient()

        result = await sender.send_task(
            session,
            client=client,
            account=account,
            task=task,
            contact=contact,
            now=utc_now(),
        )

        assert result["status"] == "SENT"
        assert client.sent and client.sent[0][1].startswith("你好 卖家一号")

        await session.refresh(task)
        await session.refresh(contact)
        assert task.status == "SENT"
        assert task.target_message_id == 1001
        assert contact.contact_state == "CONTACTED"
        assert contact.first_contact_at is not None
        assert contact.global_lock_until is not None

        daily = await session.scalar(
            select(OutreachAccountDaily).where(OutreachAccountDaily.account_id == account.id)
        )
        assert daily is not None and daily.first_contact_sent == 1

        outbound = await session.scalar(
            select(func.count())
            .select_from(OutreachMessage)
            .where(OutreachMessage.direction == "out")
        )
        assert outbound == 1


async def test_flood_wait_cools_down_account(admin_client) -> None:
    from app.db.base import as_utc, utc_now
    from app.db.models import OutreachAccountState
    from app.db.session import session_scope
    from app.services import outreach_sender_service as sender

    async with session_scope() as session:
        account, contact, task = await _prepare(session, tg_user_id=880002)
        await sender.send_task(
            session,
            client=FakeOutreachClient(fail=FloodWaitError()),
            account=account,
            task=task,
            contact=contact,
        )

        state = await session.get(OutreachAccountState, account.id)
        await session.refresh(task)
        assert state is not None and state.state == "COOLING"
        assert state.flood_wait_until is not None
        assert as_utc(state.flood_wait_until) > utc_now()
        assert task.status == "QUEUED"
        assert task.next_retry_at is not None


async def test_peer_flood_limits_account_and_freezes_conversations(admin_client) -> None:
    from app.db.models import OutreachAccountState
    from app.db.session import session_scope
    from app.services import outreach_sender_service as sender

    async with session_scope() as session:
        account, contact, task = await _prepare(session, tg_user_id=880003)
        # 模拟这个人已经在和这个账号聊天
        contact.owner_account_id = account.id
        contact.contact_state = "REPLIED"
        await session.commit()

        await sender.send_task(
            session,
            client=FakeOutreachClient(fail=PeerFloodError()),
            account=account,
            task=task,
            contact=contact,
        )

        state = await session.get(OutreachAccountState, account.id)
        await session.refresh(contact)
        await session.refresh(task)
        assert state is not None and state.state == "LIMITED"
        assert state.limited_reason == "peer_flood"
        assert contact.contact_state == "FROZEN"
        assert task.status == "FAILED"


async def test_incoming_reply_locks_owner(admin_client) -> None:
    from app.db.models import OutreachMessage
    from app.db.session import session_scope
    from app.services import outreach_sender_service as sender

    async with session_scope() as session:
        account, contact, task = await _prepare(session, tg_user_id=880004)

        result = await sender.handle_incoming(
            session,
            tenant_id=contact.tenant_id,
            account_id=account.id,
            sender_tg_id=contact.tg_user_id,
            text="你是谁？",
            tg_message_id=7001,
        )

        assert result is not None
        await session.refresh(contact)
        await session.refresh(task)
        assert contact.contact_state == "REPLIED"
        assert contact.owner_account_id == account.id
        assert contact.reply_state == "REPLIED"
        assert task.status == "CANCELLED"

        inbound = await session.scalar(
            select(func.count())
            .select_from(OutreachMessage)
            .where(OutreachMessage.direction == "in")
        )
        assert inbound == 1


async def test_refusal_adds_permanent_suppression(admin_client) -> None:
    from app.db.models import ContactSuppression
    from app.db.session import session_scope
    from app.services import outreach_sender_service as sender

    async with session_scope() as session:
        account, contact, _task = await _prepare(session, tg_user_id=880005)

        await sender.handle_incoming(
            session,
            tenant_id=contact.tenant_id,
            account_id=account.id,
            sender_tg_id=contact.tg_user_id,
            text="不要再联系我了",
            tg_message_id=7002,
        )

        await session.refresh(contact)
        assert contact.contact_state == "REFUSED"
        assert contact.do_not_contact is True
        suppressed = await session.scalar(
            select(ContactSuppression.id).where(ContactSuppression.tg_user_id == contact.tg_user_id)
        )
        assert suppressed is not None


async def test_pick_account_respects_cooldown_and_cap(admin_client) -> None:
    from app.db.base import utc_now
    from app.db.models import OutreachAccountState
    from app.db.session import session_scope
    from app.services import outreach_account_service
    from app.services import outreach_sender_service as sender

    async with session_scope() as session:
        account, _contact, _task = await _prepare(session, tg_user_id=880006)
        tenant_id = account.tenant_id

        assert await sender.pick_account(session, tenant_id) is not None

        state = await session.get(OutreachAccountState, account.id)
        state.last_cold_at = utc_now()
        await session.commit()
        assert await sender.pick_account(session, tenant_id) is None

        state.last_cold_at = None
        await session.commit()
        daily = await outreach_account_service.get_or_create_daily(session, account)
        daily.first_contact_sent = 10
        await session.commit()
        assert await sender.pick_account(session, tenant_id) is None


async def test_send_window_enforces_hours_and_pool_cap(admin_client) -> None:
    from datetime import UTC, datetime

    from app.db.session import session_scope
    from app.services import outreach_account_service, outreach_settings_service
    from app.services import outreach_sender_service as sender

    async with session_scope() as session:
        account, contact, task = await _prepare(session, tg_user_id=880020)
        tenant_id = account.tenant_id
        noon = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)

        await outreach_settings_service.update_settings(
            session,
            tenant_id,
            timezone="UTC",
            working_hours=["09:00", "18:00"],
            daily_pool_cap=1,
        )
        allowed, reason = await sender.enforce_send_window(session, tenant_id, now=noon)
        assert allowed is True and reason == ""

        daily = await outreach_account_service.get_or_create_daily(
            session,
            account,
            day=outreach_account_service.local_day(noon, tz_name="UTC"),
            tz_name="UTC",
        )
        daily.first_contact_sent = 1
        await session.commit()
        allowed, reason = await sender.enforce_send_window(session, tenant_id, now=noon)
        assert allowed is False and reason == "DAILY_POOL_CAP"
        sent = await sender.send_task(
            session,
            client=FakeOutreachClient(),
            account=account,
            task=task,
            contact=contact,
            now=noon,
        )
        assert sent["status"] == "QUEUED"
        assert sent["error"] == "DAILY_POOL_CAP"

        await outreach_settings_service.update_settings(
            session,
            tenant_id,
            working_hours=["09:00", "10:00"],
        )
        allowed, reason = await sender.enforce_send_window(session, tenant_id, now=noon)
        assert allowed is False and reason == "OUTSIDE_WORKING_HOURS"


async def test_uncertain_delivery_requires_manual_confirmation(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_sender_service as sender

    async with session_scope() as session:
        account, contact, task = await _prepare(session, tg_user_id=880021)

        result = await sender.send_task(
            session,
            client=FakeOutreachClient(fail=TimeoutError("timed out")),
            account=account,
            task=task,
            contact=contact,
        )
        assert result["status"] == "UNKNOWN_DELIVERY"
        await session.refresh(task)
        assert task.account_id == account.id
        assert contact.first_contact_at is None

        result = await sender.confirm_unknown_delivery(session, task=task, delivered=False)
        assert result["status"] == "QUEUED"
        await session.refresh(task)
        assert task.account_id is None
        assert task.attempt_count == 0


async def test_confirm_unknown_delivery_as_sent_commits_lock(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_sender_service as sender

    async with session_scope() as session:
        account, contact, task = await _prepare(session, tg_user_id=880022)
        await sender.send_task(
            session,
            client=FakeOutreachClient(fail=TimeoutError("timed out")),
            account=account,
            task=task,
            contact=contact,
        )

        result = await sender.confirm_unknown_delivery(session, task=task, delivered=True)

        assert result["status"] == "SENT"
        await session.refresh(contact)
        assert contact.contact_state == "CONTACTED"
        assert contact.first_contact_at is not None
        assert contact.global_lock_until is not None


async def test_first_contact_account_limit_requeues_without_freezing(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_sender_service as sender

    async with session_scope() as session:
        account, contact, task = await _prepare(session, tg_user_id=880023)

        await sender.send_task(
            session,
            client=FakeOutreachClient(fail=PeerFloodError()),
            account=account,
            task=task,
            contact=contact,
        )

        await session.refresh(task)
        await session.refresh(contact)
        assert task.status == "QUEUED"
        assert task.account_id is None
        assert contact.contact_state == "QUEUED"


async def test_retry_failed_task_allows_temporary_failure(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_sender_service as sender

    async with session_scope() as session:
        _account, contact, task = await _prepare(session, tg_user_id=880024)
        task.status = "FAILED"
        task.account_id = None
        task.attempt_count = 3
        contact.contact_state = "WAITING_SENDER_ACCOUNT"
        await session.commit()

        result = await sender.retry_failed_task(session, task=task)

        assert result["status"] == "QUEUED"
        await session.refresh(task)
        await session.refresh(contact)
        assert task.attempt_count == 0
        assert task.account_id is None
        assert contact.contact_state == "QUEUED"


async def test_dispatch_endpoints_and_runtime_switch(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_settings_service

    status = await admin_client.get("/api/outreach/runtime/status", headers=_headers())
    assert status.status_code == 200
    assert status.json()["paused"] is False

    paused = await admin_client.post("/api/outreach/runtime/pause", headers=_headers())
    assert paused.status_code == 200
    assert (await admin_client.get("/api/outreach/runtime/status", headers=_headers())).json()[
        "paused"
    ] is True

    await admin_client.post("/api/outreach/runtime/resume", headers=_headers())
    assert (await admin_client.get("/api/outreach/runtime/status", headers=_headers())).json()[
        "paused"
    ] is False

    # 没有可用发信息账号时不会发送，但接口要正常返回
    empty = await admin_client.post("/api/outreach/queue/dispatch", headers=_headers())
    assert empty.status_code == 200
    assert empty.json() == {"sent": 0, "results": []}

    async with session_scope() as session:
        await outreach_settings_service.update_settings(session, 1, kill_switch=True)
    blocked = await admin_client.post("/api/outreach/queue/dispatch", headers=_headers())
    assert blocked.status_code == 400
    async with session_scope() as session:
        await outreach_settings_service.update_settings(session, 1, kill_switch=False)


async def test_contact_takeover_and_suppression(admin_client) -> None:
    from app.db.models import OutreachContact
    from app.db.session import session_scope
    from app.services import outreach_sender_service as sender

    async with session_scope() as session:
        _account, contact, _task = await _prepare(session, tg_user_id=880007)
        contact_id = contact.id
        tenant_id = contact.tenant_id
        await sender.handle_incoming(
            session,
            tenant_id=tenant_id,
            account_id=_account.id,
            sender_tg_id=contact.tg_user_id,
            text="你好",
            tg_message_id=7003,
        )

    takeover = await admin_client.post(
        f"/api/outreach/contacts/{contact_id}/takeover",
        headers=_headers(),
    )
    assert takeover.status_code == 200
    assert takeover.json()["reply_state"] == "HUMAN"

    blocked = await admin_client.post(
        f"/api/outreach/contacts/{contact_id}/suppress",
        headers=_headers(),
    )
    assert blocked.status_code == 200
    assert blocked.json()["do_not_contact"] is True

    released = await admin_client.delete(
        f"/api/outreach/contacts/{contact_id}/suppress",
        headers=_headers(),
    )
    assert released.status_code == 200
    assert released.json()["do_not_contact"] is False

    async with session_scope() as session:
        row = await session.get(OutreachContact, contact_id)
        assert row is not None and row.do_not_contact is False
