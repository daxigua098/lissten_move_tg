"""冷触达 P1：入队闸门、联系锁、话术模板与容量。"""

from __future__ import annotations

from datetime import timedelta

from conftest import ADMIN_API_TOKEN, auth_header
from sqlalchemy import func, select


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


EMPTY_CONTACTS = '{"phones": [], "wechats": [], "usernames": []}'


def _lead(**overrides):
    from app.core.outreach_capture import ROUTE_USERNAME, reachable_routes_json
    from app.db.models import Lead

    data = {
        "message_id": 1,
        "sender_tg_id": 990001,
        "sender_username": "seller01",
        "sender_name": "卖家一号",
        "text": "有意的话私信我",
        "reachable_routes": reachable_routes_json((ROUTE_USERNAME,)),
        "outreach_status": "WAITING_SENDER_ACCOUNT",
        "contacts": EMPTY_CONTACTS,
    }
    data.update(overrides)
    return Lead(**data)


async def test_enqueue_creates_task_and_queues_contact(admin_client) -> None:
    from app.db.models import OutreachContact
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        lead = _lead()
        session.add(lead)
        await session.commit()
        await session.refresh(lead)

        task, reason = await outreach_queue_service.enqueue_lead(session, lead)

        assert reason == ""
        assert task is not None
        assert task.status == "QUEUED"
        assert task.kind == "first_contact"

        contact = await session.scalar(
            select(OutreachContact).where(OutreachContact.tg_user_id == 990001)
        )
        assert contact is not None
        assert contact.contact_state == "QUEUED"
        assert contact.first_lead_id == lead.id
        assert contact.identity_confirmed is True
        assert lead.outreach_status == "QUEUED"


async def test_same_user_is_enqueued_only_once(admin_client) -> None:
    from app.db.models import OutreachTask
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        first = _lead(sender_tg_id=990002, message_id=2)
        second = _lead(sender_tg_id=990002, message_id=3)
        session.add_all([first, second])
        await session.commit()
        await session.refresh(first)
        await session.refresh(second)

        task, reason = await outreach_queue_service.enqueue_lead(session, first)
        assert task is not None

        blocked_task, blocked_reason = await outreach_queue_service.enqueue_lead(session, second)
        assert blocked_task is None
        assert blocked_reason == outreach_queue_service.BLOCK_DUPLICATE

        total = len(list(await session.scalars(select(OutreachTask))))
        assert total == 1


async def test_peer_reference_only_lead_is_blocked(admin_client) -> None:
    """只有监听账号才认识的路径不算可触达。"""
    from app.core.outreach_capture import ROUTE_SHARED_GROUP, reachable_routes_json
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        lead = _lead(
            sender_tg_id=990003,
            sender_username=None,
            reachable_routes=reachable_routes_json((ROUTE_SHARED_GROUP,)),
        )
        session.add(lead)
        await session.commit()
        await session.refresh(lead)

        task, reason = await outreach_queue_service.enqueue_lead(session, lead)

        assert task is None
        assert reason == outreach_queue_service.BLOCK_NO_CONTACT


async def test_phone_only_lead_is_blocked(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        lead = _lead(
            sender_tg_id=990004,
            sender_username=None,
            reachable_routes="[]",
            contacts='{"phones": ["13800001111"], "wechats": [], "usernames": []}',
            phone="13800001111",
        )
        session.add(lead)
        await session.commit()
        await session.refresh(lead)

        task, reason = await outreach_queue_service.enqueue_lead(session, lead)

        assert task is None
        assert reason == outreach_queue_service.BLOCK_NO_CONTACT


async def test_message_username_counts_as_reachable(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        lead = _lead(
            sender_tg_id=990005,
            sender_username=None,
            reachable_routes="[]",
            contacts='{"phones": [], "wechats": [], "usernames": ["from_message"]}',
        )
        session.add(lead)
        await session.commit()
        await session.refresh(lead)

        task, reason = await outreach_queue_service.enqueue_lead(session, lead)

        assert reason == ""
        assert task is not None


async def test_global_lock_and_suppression_block(admin_client) -> None:
    from app.db.base import utc_now
    from app.db.models import ContactSuppression
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        locked = _lead(sender_tg_id=990005)
        session.add(locked)
        await session.commit()
        await session.refresh(locked)
        contact = await outreach_queue_service.get_or_create_contact(session, locked)
        contact.global_lock_until = utc_now() + timedelta(days=3)
        await session.commit()

        task, reason = await outreach_queue_service.enqueue_lead(session, locked)
        assert task is None
        assert reason == outreach_queue_service.BLOCK_GLOBAL_LOCK

        suppressed = _lead(sender_tg_id=990006)
        session.add(suppressed)
        session.add(ContactSuppression(tg_user_id=990006, reason="manual"))
        await session.commit()
        await session.refresh(suppressed)

        task, reason = await outreach_queue_service.enqueue_lead(session, suppressed)
        assert task is None
        assert reason == outreach_queue_service.BLOCK_DO_NOT_CONTACT


async def test_kill_switch_stops_enqueue(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_queue_service, outreach_settings_service

    async with session_scope() as session:
        await outreach_settings_service.update_settings(session, 1, kill_switch=True)
        lead = _lead(sender_tg_id=990007)
        session.add(lead)
        await session.commit()
        await session.refresh(lead)

        task, reason = await outreach_queue_service.enqueue_lead(session, lead)
        assert task is None
        assert reason == outreach_queue_service.BLOCK_KILL_SWITCH

        await outreach_settings_service.update_settings(session, 1, kill_switch=False)


async def test_plan_pending_and_capacity(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        session.add(_lead(sender_tg_id=990008, message_id=8))
        session.add(_lead(sender_tg_id=990009, message_id=9))
        await session.commit()

        result = await outreach_queue_service.plan_pending(session, tenant_id=1, limit=50)
        assert result["created"] == 2

        again = await outreach_queue_service.plan_pending(session, tenant_id=1, limit=50)
        assert again["created"] == 0

        data = await outreach_queue_service.capacity(session, tenant_id=1)
        assert data["queued"] == 2
        assert data["today_available"] == 0
        assert data["estimated_days"] is None


async def test_settings_reject_short_lock(admin_client) -> None:
    response = await admin_client.patch(
        "/api/outreach/settings",
        headers=_headers(),
        json={"cross_account_lock_days": 3},
    )

    assert response.status_code == 400
    assert "14" in response.json()["detail"]

    ok = await admin_client.patch(
        "/api/outreach/settings",
        headers=_headers(),
        json={"cross_account_lock_days": 30, "follow_up_days": 7},
    )
    assert ok.status_code == 200
    assert ok.json()["cross_account_lock_days"] == 30


async def test_templates_reject_link_and_support_platform_adopt(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_template_service

    linked = await admin_client.post(
        "/api/outreach/templates",
        headers=_headers(),
        json={"name": "带链接", "kind": "first_contact", "text": "你好 https://t.me/xxx"},
    )
    assert linked.status_code == 400

    created = await admin_client.post(
        "/api/outreach/templates",
        headers=_headers(),
        json={
            "name": "首条确认",
            "kind": "first_contact",
            "text": "你好，我是某某团队，想确认一下你是否愿意了解。",
        },
    )
    assert created.status_code == 201
    assert created.json()["scope"] == "tenant"

    async with session_scope() as session:
        platform = await outreach_template_service.create_template(
            session,
            1,
            name="平台首条",
            kind="first_contact",
            text="你好，想确认一下是否方便继续了解。",
            scope="platform",
        )
        platform_id = platform.id

    listing = await admin_client.get("/api/outreach/templates", headers=_headers())
    assert listing.status_code == 200
    assert listing.json()["total"] == 2

    adopted = await admin_client.post(
        f"/api/outreach/templates/{platform_id}/adopt",
        headers=_headers(),
    )
    assert adopted.status_code == 201
    assert adopted.json()["source_template_id"] == platform_id

    readonly = await admin_client.patch(
        f"/api/outreach/templates/{platform_id}",
        headers=_headers(),
        json={"text": "改一下"},
    )
    assert readonly.status_code == 400


async def test_clear_queue_resets_and_allows_replan(admin_client) -> None:
    """清空队列：删任务、把只排过队的线索退回，之后还能重新生成。"""
    from app.db.models import OutreachContact, OutreachTask
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        session.add(_lead(sender_tg_id=990100, message_id=101))
        session.add(_lead(sender_tg_id=990101, message_id=102))
        await session.commit()

        planned = await outreach_queue_service.plan_pending(session, tenant_id=1, limit=50)
        assert planned["created"] == 2

        cleared = await outreach_queue_service.clear_queue(session, tenant_id=1)
        assert cleared["deleted_tasks"] == 2
        assert cleared["reset_leads"] == 2

        remaining = await session.scalar(
            select(func.count()).select_from(OutreachTask).where(OutreachTask.tenant_id == 1)
        )
        assert remaining == 0

        contacts = list(
            await session.scalars(select(OutreachContact).where(OutreachContact.tenant_id == 1))
        )
        assert contacts and all(item.contact_state == "WAITING_SENDER_ACCOUNT" for item in contacts)

        # 线索退回「待生成」后可以重新排队
        again = await outreach_queue_service.plan_pending(session, tenant_id=1, limit=50)
        assert again["created"] == 2


async def test_clear_queue_api(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        session.add(_lead(sender_tg_id=990102, message_id=103))
        await session.commit()
        await outreach_queue_service.plan_pending(session, tenant_id=1, limit=50)

    response = await admin_client.post("/api/outreach/queue/clear", headers=_headers())
    assert response.status_code == 200
    assert response.json()["deleted_tasks"] == 1


async def test_task_detail_returns_lead_info(admin_client) -> None:
    """队列里点联系人：要能看到监听抓取到的来源线索。"""
    from app.db.models import OutreachContact, OutreachTask
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        lead = _lead(
            sender_tg_id=990200,
            message_id=201,
            text="想做体育项目，有意私信我",
            keyword="体育",
            source_title="测试来源群",
        )
        session.add(lead)
        await session.commit()
        await session.refresh(lead)
        await outreach_queue_service.plan_pending(session, tenant_id=1, limit=50)

        contact = await session.scalar(
            select(OutreachContact).where(OutreachContact.tg_user_id == 990200)
        )
        assert contact is not None
        task = await session.scalar(
            select(OutreachTask).where(OutreachTask.contact_id == contact.id)
        )
        assert task is not None
        task_id = task.id

    response = await admin_client.get(
        f"/api/outreach/tasks/{task_id}/detail",
        headers=_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert body["task"]["status"] == "QUEUED"
    assert body["contact"]["tg_user_id"] == 990200
    assert body["lead"]["text"] == "想做体育项目，有意私信我"
    assert body["lead"]["keyword"] == "体育"
    assert body["lead"]["source_title"] == "测试来源群"
    assert "USERNAME" in body["lead"]["reachable_routes"]


async def test_delete_single_task_resets_and_allows_replan(admin_client) -> None:
    """单条删除：只排过队的联系人与线索退回可再生成状态。"""
    from app.db.models import Lead, OutreachContact, OutreachTask
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        session.add(_lead(sender_tg_id=990300, message_id=301))
        await session.commit()
        await outreach_queue_service.plan_pending(session, tenant_id=1, limit=50)

        task = await session.scalar(select(OutreachTask))
        assert task is not None
        task_id, contact_id, lead_id = task.id, task.contact_id, task.lead_id

        result = await outreach_queue_service.delete_tasks(session, [task_id], tenant_id=1)
        assert result["deleted"] == 1

        contact = await session.get(OutreachContact, contact_id)
        lead = await session.get(Lead, lead_id)
        assert contact is not None and contact.contact_state == "WAITING_SENDER_ACCOUNT"
        assert lead is not None and lead.outreach_status == "WAITING_SENDER_ACCOUNT"
        left = await session.scalar(select(func.count()).select_from(OutreachTask))
        assert left == 0

        # 退回后还能重新生成
        again = await outreach_queue_service.plan_pending(session, tenant_id=1, limit=50)
        assert again["created"] == 1


async def test_delete_tasks_api_keeps_others(admin_client) -> None:
    from app.db.models import OutreachTask
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        session.add(_lead(sender_tg_id=990301, message_id=302))
        session.add(_lead(sender_tg_id=990302, message_id=303))
        await session.commit()
        await outreach_queue_service.plan_pending(session, tenant_id=1, limit=50)
        tasks = list(await session.scalars(select(OutreachTask).order_by(OutreachTask.id)))
        assert len(tasks) == 2
        keep_id, drop_id = tasks[1].id, tasks[0].id

    response = await admin_client.post(
        "/api/outreach/tasks/delete",
        headers=_headers(),
        json={"task_ids": [drop_id]},
    )

    assert response.status_code == 200
    assert response.json()["deleted"] == 1

    async with session_scope() as session:
        remaining = list(await session.scalars(select(OutreachTask)))
        assert [item.id for item in remaining] == [keep_id]


async def test_same_person_uses_highest_priority_lead(admin_client) -> None:
    """同一人多条 Lead：明确邀请 + 命中关键词优先。"""
    from app.core.outreach_capture import CONSENT_EXPLICIT_DM_INVITE
    from app.db.models import OutreachTask
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        normal = _lead(sender_tg_id=991001, message_id=401, sender_username="normal")
        strong = _lead(
            sender_tg_id=991001,
            message_id=402,
            sender_username="strong",
            keyword="体育",
            score=0.9,
            consent_type=CONSENT_EXPLICIT_DM_INVITE,
        )
        session.add_all([normal, strong])
        await session.commit()
        await session.refresh(normal)
        await session.refresh(strong)

        result = await outreach_queue_service.plan_pending(session, tenant_id=1, limit=20)
        assert result["created"] == 1
        assert result["scanned_contacts"] == 1
        task = await session.scalar(select(OutreachTask))
        assert task is not None
        assert task.lead_id == strong.id
        assert task.priority > 0
        assert "明确邀请" in (task.priority_reason or "")


async def test_same_person_falls_back_to_sendable_sibling(admin_client) -> None:
    """最高优先线索没有用户名时，继续寻找同一个人的可发送线索。"""
    from app.core.outreach_capture import CONSENT_EXPLICIT_DM_INVITE
    from app.db.models import OutreachTask
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        unsendable = _lead(
            sender_tg_id=991009,
            message_id=410,
            sender_username=None,
            consent_type=CONSENT_EXPLICIT_DM_INVITE,
            keyword="体育",
        )
        sendable = _lead(sender_tg_id=991009, message_id=411, sender_username="usable")
        session.add_all([unsendable, sendable])
        await session.commit()
        await session.refresh(unsendable)
        await session.refresh(sendable)

        result = await outreach_queue_service.plan_pending(session, tenant_id=1, limit=20)

        assert result["created"] == 1
        task = await session.scalar(select(OutreachTask))
        assert task is not None and task.lead_id == sendable.id


async def test_plan_filters_and_preview_do_not_mutate(admin_client) -> None:
    from app.db.models import Lead, OutreachTask
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        wanted = _lead(sender_tg_id=991002, message_id=403, keyword="体育")
        ignored = _lead(sender_tg_id=991003, message_id=404, keyword="财经")
        session.add_all([wanted, ignored])
        await session.commit()
        await session.refresh(wanted)
        await session.refresh(ignored)

        preview = await outreach_queue_service.plan_pending(
            session,
            tenant_id=1,
            limit=20,
            keyword="体育",
            dry_run=True,
        )
        assert preview["created"] == 1
        assert int(await session.scalar(select(func.count()).select_from(OutreachTask)) or 0) == 0
        left = await session.get(Lead, wanted.id)
        assert left is not None and left.outreach_status == "WAITING_SENDER_ACCOUNT"

        actual = await outreach_queue_service.plan_pending(
            session,
            tenant_id=1,
            limit=20,
            keyword="体育",
        )
        assert actual["created"] == 1
        task = await session.scalar(select(OutreachTask))
        assert task is not None and task.lead_id == wanted.id


async def test_blocked_lead_records_retry_time(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        lead = _lead(
            sender_tg_id=991004,
            message_id=405,
            sender_username=None,
            contacts=EMPTY_CONTACTS,
            reachable_routes="[]",
        )
        session.add(lead)
        await session.commit()
        await session.refresh(lead)

        result = await outreach_queue_service.plan_pending(session, tenant_id=1, limit=20)

        assert result["created"] == 0
        await session.refresh(lead)
        assert lead.last_block_reason == outreach_queue_service.BLOCK_NO_CONTACT
        assert lead.last_plan_checked_at is not None
        assert lead.next_plan_at is not None


async def test_only_authorized_setting_blocks_none(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_queue_service, outreach_settings_service

    async with session_scope() as session:
        await outreach_settings_service.update_settings(session, 1, only_authorized=True)
        session.add(_lead(sender_tg_id=991005, message_id=406))
        await session.commit()

        result = await outreach_queue_service.plan_pending(session, tenant_id=1, limit=20)

        assert result["created"] == 0
        assert result["blocked"][0]["reason"] == outreach_queue_service.BLOCK_CONSENT


async def test_clear_queue_preserves_terminal_history(admin_client) -> None:
    from app.db.models import OutreachTask
    from app.db.session import session_scope
    from app.services import outreach_queue_service

    async with session_scope() as session:
        session.add(_lead(sender_tg_id=991006, message_id=407))
        await session.commit()
        await outreach_queue_service.plan_pending(session, tenant_id=1, limit=20)
        task = await session.scalar(select(OutreachTask))
        assert task is not None
        task.status = "SENT"
        await session.commit()

        result = await outreach_queue_service.clear_queue(session, tenant_id=1)

        assert result["deleted_tasks"] == 0
        assert await session.get(OutreachTask, task.id) is not None


async def test_preview_and_settings_api(admin_client) -> None:
    from app.db.models import OutreachTask
    from app.db.session import session_scope

    async with session_scope() as session:
        session.add(_lead(sender_tg_id=991007, message_id=408, keyword="体育"))
        await session.commit()

    preview = await admin_client.get(
        "/api/outreach/queue/preview",
        headers=_headers(),
        params={"keyword": "体育", "limit": 20},
    )
    assert preview.status_code == 200
    assert preview.json()["created"] == 1
    async with session_scope() as session:
        assert int(await session.scalar(select(func.count()).select_from(OutreachTask)) or 0) == 0

    updated = await admin_client.patch(
        "/api/outreach/settings",
        headers=_headers(),
        json={
            "only_authorized": True,
            "auto_queue_enabled": True,
            "max_lead_age_days": 7,
            "timezone": "UTC",
        },
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["only_authorized"] is True
    assert body["auto_queue_enabled"] is True
    assert body["max_lead_age_days"] == 7
    assert body["timezone"] == "UTC"

    invalid = await admin_client.patch(
        "/api/outreach/settings",
        headers=_headers(),
        json={"max_lead_age_days": 15},
    )
    assert invalid.status_code == 400


async def test_auto_queue_tick_uses_enabled_settings(admin_client, api_config) -> None:
    from app.db.models import OutreachTask
    from app.db.session import session_scope
    from app.services import outreach_settings_service
    from app.services.runtime_service import RuntimeService

    async with session_scope() as session:
        await outreach_settings_service.update_settings(session, 1, auto_queue_enabled=True)
        session.add(_lead(sender_tg_id=991008, message_id=409))
        await session.commit()

    service = RuntimeService(api_config)
    created = await service._auto_queue_tick()

    assert created == 1
    async with session_scope() as session:
        assert int(await session.scalar(select(func.count()).select_from(OutreachTask)) or 0) == 1
