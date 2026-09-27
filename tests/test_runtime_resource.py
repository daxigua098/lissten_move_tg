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


async def test_resource_tick_runs_due_directory_task(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    """到点的目录同步任务会被运行时无人值守地执行（F-R24）。"""
    from sqlalchemy import select

    from app.db.models import TgResource
    from app.db.session import session_scope
    from app.services import directory_sync_service
    from app.services.runtime_service import RuntimeService
    from tests.test_directory_sync_service import _combot_fetcher

    db.resource.directory_request_interval = 0
    async with session_scope() as session:
        await directory_sync_service.ensure_default_task(session, tenant_id=1)

    service = RuntimeService(db)
    # 注入抓取替身：运行时不该真的去访问三方站
    service._directory_fetcher = _combot_fetcher()
    await service._resource_tick(fake_resource_client)

    async with session_scope() as session:
        rows = list(await session.scalars(select(TgResource)))
    assert {row.username for row in rows} == {"qqpp", "rongcheng_travel"}
    assert all(row.source_site == "combot" for row in rows)


async def test_directory_tick_writes_to_task_tenant(db, fake_resource_client) -> None:
    """目录任务属于哪个租户，运行记录和候选资源就写进哪个租户。"""
    from sqlalchemy import select

    from app.db.models import ResourceDirectoryRun, ResourceDiscoverTask, Tenant, TgResource
    from app.db.session import session_scope
    from app.services import directory_sync_service
    from app.services.runtime_service import RuntimeService
    from tests.test_directory_sync_service import _combot_fetcher

    db.resource.directory_request_interval = 0
    async with session_scope() as session:
        session.add(
            Tenant(
                id=2,
                name="目录租户",
                kind="self",
                status="active",
                runtime_enabled=True,
            )
        )
        await session.flush()
        task = await directory_sync_service.ensure_default_task(session, tenant_id=2)
        task_id = task.id
        await session.commit()

    service = RuntimeService(db)
    service._directory_fetcher = _combot_fetcher()
    await service._resource_tick(fake_resource_client)

    async with session_scope() as session:
        task = await session.get(ResourceDiscoverTask, task_id)
        resources = list(await session.scalars(select(TgResource)))
        runs = list(await session.scalars(select(ResourceDirectoryRun)))

    assert task is not None and task.tenant_id == 2
    assert resources and {row.tenant_id for row in resources} == {2}
    assert runs and {row.tenant_id for row in runs} == {2}


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
    """一轮只做一件事：加群队列优先于目录同步（同账号串行）。"""
    from app.db.models import JOIN_SUCCESS, ResourceJoinTask
    from app.db.session import session_scope
    from app.services import (
        directory_sync_service,
        resource_join_service,
        resource_service,
    )
    from app.services.resource_service import ResourceRef
    from app.services.runtime_service import RuntimeService
    from tests.test_directory_sync_service import _combot_fetcher

    db.resource.directory_request_interval = 0
    fake_resource_client.add_chat(9501, "待加入群", username="join_first")
    directory_fetcher = _combot_fetcher()
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
        await directory_sync_service.ensure_default_task(session, tenant_id=1)

    service = RuntimeService(db)
    service._directory_fetcher = directory_fetcher
    await service._resource_tick(fake_resource_client)

    async with session_scope() as session:
        stored = await session.get(ResourceJoinTask, task_id)
    assert stored.status == JOIN_SUCCESS
    assert directory_fetcher.requests == []  # 这一轮没有执行目录同步
