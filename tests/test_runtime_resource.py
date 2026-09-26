"""运行时里的资源发现：链接滚雪球、限速加群、自动发现与探测。"""

from __future__ import annotations

from types import SimpleNamespace

from conftest import fake_message


async def _prepare_b_route(db, *, listen_mode: str = "all"):
    """建一条 B 线（全量监听），用于验证监听消息里的链接滚雪球。"""
    from app.core.telegram_client import ChatProfile
    from app.db.session import session_scope
    from app.services import chat_service, route_service

    async with session_scope() as session:
        source = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=9101,
                chat_type="supergroup",
                title="被监听的群",
                username=None,
                is_private=True,
            ),
        )
        await chat_service.set_source(session, source)
        target = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=9102,
                chat_type="supergroup",
                title="线索群",
                username=None,
                is_private=True,
            ),
        )
        await chat_service.set_target(session, target, role="lead")
        route = await route_service.create_route(
            session,
            name="监听",
            source_chat_id=source.id,
            business_type="B",
            target_chat_ids=[target.id],
            b_config={"listen_mode": listen_mode},
        )
        return route.id


def _event(text: str, *, message_id: int = 5001, user_id: int = 7001):
    message = fake_message(message_id, text)
    message.sender = SimpleNamespace(
        id=user_id,
        username="member01",
        first_name="会",
        last_name="员",
        phone=None,
        bot=False,
    )
    return SimpleNamespace(message=message)


async def test_monitor_message_snowballs_links(db, fake_delivery_client) -> None:
    """监听中的会员发言里贴的群链接，会被收进候选池（F-R03）。"""
    from sqlalchemy import select

    from app.db.models import TgResource
    from app.db.session import session_scope
    from app.services import route_service
    from app.services.runtime_service import RuntimeService

    route_id = await _prepare_b_route(db)
    service = RuntimeService(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
    await service._on_monitor_message(
        fake_delivery_client,
        _event("有人在吗？另外推荐一个群 t.me/from_live 很好用"),
        [route],
    )

    async with session_scope() as session:
        rows = list(await session.scalars(select(TgResource)))

    assert len(rows) == 1
    assert rows[0].username == "from_live"
    assert rows[0].discovered_by == "link"
    assert rows[0].discovered_from == "被监听的群"


async def test_monitor_message_without_links_creates_nothing(
    db,
    fake_delivery_client,
) -> None:
    from sqlalchemy import select

    from app.db.models import TgResource
    from app.db.session import session_scope
    from app.services import route_service
    from app.services.runtime_service import RuntimeService

    route_id = await _prepare_b_route(db)
    service = RuntimeService(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
    await service._on_monitor_message(
        fake_delivery_client,
        _event("今天天气不错，随便聊聊"),
        [route],
    )

    async with session_scope() as session:
        assert list(await session.scalars(select(TgResource))) == []


async def test_resource_tick_runs_due_join_task(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    """到点的加群任务会被运行时按限速执行（F-R12）。"""
    from app.db.models import JOIN_SUCCESS, ResourceJoinTask
    from app.db.session import session_scope
    from app.services import resource_join_service, resource_service
    from app.services.resource_service import ResourceRef
    from app.services.runtime_service import RuntimeService

    fake_resource_client.add_chat(9201, "待加入群", username="to_join")
    async with session_scope() as session:
        outcome = await resource_service.upsert_resource(
            session,
            ResourceRef(tg_id=9201, title="待加入群", username="to_join"),
        )
        resource = outcome.resource
        task = await resource_join_service.enqueue(
            session,
            db,
            resource,
            account_id=probe_account,
            jitter=False,
        )
        task_id = task.id

    service = RuntimeService(db)
    await service._resource_tick(fake_resource_client)

    assert fake_resource_client.joined == [9201]
    async with session_scope() as session:
        stored = await session.get(ResourceJoinTask, task_id)
    assert stored.status == JOIN_SUCCESS


async def test_resource_tick_runs_due_discover_task(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    """到点的发现任务会被运行时无人值守地执行（F-R02）。"""
    from sqlalchemy import select

    from app.db.models import TgResource
    from app.db.session import session_scope
    from app.services import resource_discover_service
    from app.services.runtime_service import RuntimeService

    chat = fake_resource_client.add_chat(9301, "求职群", username="job_tick", member_count=500)
    fake_resource_client.add_search("求职", [chat])
    async with session_scope() as session:
        await resource_discover_service.create_task(session, kind="keyword", keyword="求职")

    service = RuntimeService(db)
    await service._resource_tick(fake_resource_client)

    async with session_scope() as session:
        rows = list(await session.scalars(select(TgResource)))
    assert [row.username for row in rows] == ["job_tick"]


async def test_resource_tick_probes_never_probed_candidate(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    """候选池里的资源会被自动探测，探测完就进入"已探测"（F-R06）。"""
    from app.db.models import RESOURCE_PROBED
    from app.db.session import session_scope
    from app.services import resource_service
    from app.services.resource_service import ResourceRef
    from app.services.runtime_service import RuntimeService

    fake_resource_client.add_chat(
        9401,
        "求职大群",
        username="job_probe",
        about="每天发布招聘信息",
        member_count=7500,
    )
    fake_resource_client.add_history(9401, [fake_message(1, "招聘日结，当天结算")])
    async with session_scope() as session:
        outcome = await resource_service.upsert_resource(
            session,
            ResourceRef(tg_id=9401, title="求职大群", username="job_probe"),
        )
        resource_id = outcome.resource.id

    service = RuntimeService(db)
    await service._resource_tick(fake_resource_client)

    async with session_scope() as session:
        updated = await resource_service.require_resource(session, resource_id)
    assert updated.status == RESOURCE_PROBED
    assert updated.member_count == 7500
    assert updated.activity_score is not None
    assert resource_service.load_samples(updated)


async def test_resource_tick_prefers_join_queue(db, fake_resource_client, probe_account) -> None:
    """一轮只做一件事：加群队列优先于发现任务（同账号串行）。"""
    from app.db.models import JOIN_SUCCESS, ResourceJoinTask
    from app.db.session import session_scope
    from app.services import (
        resource_discover_service,
        resource_join_service,
        resource_service,
    )
    from app.services.resource_service import ResourceRef
    from app.services.runtime_service import RuntimeService

    fake_resource_client.add_chat(9501, "待加入群", username="join_first")
    chat = fake_resource_client.add_chat(9502, "求职群", username="job_later")
    fake_resource_client.add_search("求职", [chat])
    async with session_scope() as session:
        outcome = await resource_service.upsert_resource(
            session,
            ResourceRef(tg_id=9501, title="待加入群", username="join_first"),
        )
        task = await resource_join_service.enqueue(
            session,
            db,
            outcome.resource,
            account_id=probe_account,
            jitter=False,
        )
        task_id = task.id
        await resource_discover_service.create_task(session, kind="keyword", keyword="求职")

    service = RuntimeService(db)
    await service._resource_tick(fake_resource_client)

    async with session_scope() as session:
        stored = await session.get(ResourceJoinTask, task_id)
        probes = await resource_service.list_resources(session, keyword="job_later")
    assert stored.status == JOIN_SUCCESS
    assert probes[1] == 0  # 这一轮没有执行发现任务
