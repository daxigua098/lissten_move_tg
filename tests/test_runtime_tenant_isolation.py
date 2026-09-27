"""P1-06：运行时按租户取执行账号与连接。

覆盖三件事：
1. ``get_default_account(tenant_id=...)`` 只在本租户里取号；
2. 投递循环用**线路所属租户**的连接（连接按租户切分）；
3. 某个租户没有可用账号时只跳过它的任务，不拖垮其他租户的队列与注册。
"""

from __future__ import annotations

from app.db.models import (
    ACCOUNT_ACTIVE,
    JOB_PENDING,
    JOB_SKIPPED,
    JOB_SUCCESS,
    DeliveryJob,
    Route,
)
from app.db.session import session_scope
from app.db.tenant_context import tenant_scope
from app.services import (
    chat_service,
    tenant_service,
    tg_account_service,
    user_service,
)

_API_ID = 123456
_API_HASH = "abcdef0123456789abcdef0123456789"


async def _make_tenant(db, *, username: str) -> int:
    """建一个会员账号 + 租户（运行时开关打开），返回租户 ID。"""
    async with session_scope() as session:
        owner = await user_service.create_user(
            session,
            db,
            username=username,
            password="Pass1234",
            account_type="member",
        )
        tenant = await tenant_service.create_tenant(
            session,
            name=f"t-{username}",
            owner_user_id=owner.id,
        )
        tenant.runtime_enabled = True
        await session.commit()
        return tenant.id


async def _add_account(
    db,
    tenant_id: int,
    *,
    name: str,
    phone: str,
    active: bool = True,
) -> int:
    """给指定租户登记一个执行账号（默认仍在自己的租户里取号）。"""
    async with session_scope() as session:
        with tenant_scope(tenant_id):
            account = await tg_account_service.create_account(
                session,
                db,
                name=name,
                phone=phone,
                api_id=_API_ID,
                api_hash=_API_HASH,
                is_default=True,
            )
        if active:
            account.status = ACCOUNT_ACTIVE
        await session.commit()
        return account.id


async def _add_route(db, tenant_id: int, *, tg_id: int) -> int:
    """在指定租户下建一条 A 线（只为拿一条可投递的线路）。"""
    from app.core.telegram_client import ChatProfile

    async with session_scope() as session:
        chat = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=tg_id,
                chat_type="channel",
                title=f"源{tg_id}",
                username=f"src{tg_id}",
                is_private=False,
            ),
            tenant_id=tenant_id,
        )
        route = Route(
            name=f"线路{tg_id}",
            source_chat_id=chat.id,
            business_type="A",
            tenant_id=tenant_id,
            enabled=True,
        )
        session.add(route)
        await session.commit()
        return route.id


async def _add_job(db, tenant_id: int, route_id: int, *, message_id: int) -> int:
    """直接塞一条待投递任务（投递引擎不是本文件的测试对象）。"""
    async with session_scope() as session:
        route = await session.get(Route, route_id)
        job = DeliveryJob(
            route_id=route_id,
            source_chat_id=route.source_chat_id,
            source_message_id=message_id,
            target_chat_id=route.source_chat_id,
            status=JOB_PENDING,
            tenant_id=tenant_id,
        )
        session.add(job)
        await session.commit()
        return job.id


async def test_default_account_is_tenant_scoped(db) -> None:
    """默认账号按租户取：两个租户各一个号，互相取不到。"""
    tenant_a = await _make_tenant(db, username="p106-a")
    tenant_b = await _make_tenant(db, username="p106-b")
    account_a = await _add_account(db, tenant_a, name="A号", phone="+8613800000201")
    account_b = await _add_account(db, tenant_b, name="B号", phone="+8613800000202")

    async with session_scope() as session:
        found_a = await tg_account_service.get_default_account(session, tenant_id=tenant_a)
        found_b = await tg_account_service.get_default_account(session, tenant_id=tenant_b)

    assert found_a is not None and found_a.id == account_a
    assert found_a.tenant_id == tenant_a
    assert found_b is not None and found_b.id == account_b
    assert found_b.tenant_id == tenant_b


async def test_default_account_without_tenant_keeps_old_scope(db) -> None:
    """不传租户时仍是"全库第一个"——老调用方（脚本 / CLI）口径不变。"""
    tenant_a = await _make_tenant(db, username="p106-old-a")
    account_a = await _add_account(db, tenant_a, name="旧号", phone="+8613800000205")

    async with session_scope() as session:
        found = await tg_account_service.get_default_account(session)

    assert found is not None and found.id == account_a


async def test_deliver_once_uses_route_tenant_connection(db, fake_delivery_client) -> None:
    """投递时按线路所属租户取连接，不是全库第一条连接。"""
    from app.services.runtime_service import RuntimeService

    tenant_id = await _make_tenant(db, username="p106-deliver")
    route_id = await _add_route(db, tenant_id, tg_id=9310)
    job_id = await _add_job(db, tenant_id, route_id, message_id=9301)

    service = RuntimeService(db, poll_interval=0.01)
    seen: list[int] = []

    async def fake_client_for(tenant: int, session=None):  # noqa: ANN001, ANN202
        seen.append(tenant)
        return fake_delivery_client

    service._client_for = fake_client_for  # type: ignore[method-assign]
    delivered = await service._deliver_once()

    assert delivered == 1
    assert seen == [tenant_id]
    assert fake_delivery_client.forwarded  # 真的用这条连接转发了
    async with session_scope() as session:
        job = await session.get(DeliveryJob, job_id)
    assert job.status == JOB_SUCCESS


async def test_missing_account_skips_tenant_without_blocking_others(
    db, fake_delivery_client
) -> None:
    """一个租户没号只跳过它自己的任务，别的租户照样投递。"""
    from app.services.runtime_service import RuntimeService

    # 队头租户：没登记任何执行账号
    tenant_none = await _make_tenant(db, username="p106-none")
    route_none = await _add_route(db, tenant_none, tg_id=9320)
    job_none = await _add_job(db, tenant_none, route_none, message_id=9321)

    # 后一个租户：有可用账号，应当被正常投递
    tenant_ok = await _make_tenant(db, username="p106-ok")
    await _add_account(db, tenant_ok, name="可用号", phone="+8613800000203")
    route_ok = await _add_route(db, tenant_ok, tg_id=9330)
    job_ok = await _add_job(db, tenant_ok, route_ok, message_id=9331)

    async def factory(config, *, api_id, api_hash, session_path):  # noqa: ANN001, ANN202
        return fake_delivery_client

    service = RuntimeService(db, client_factory=factory, poll_interval=0.01)

    first = await service._deliver_once()  # 没号的租户：跳过，不卡队列
    second = await service._deliver_once()  # 有号的租户：正常投递
    third = await service._deliver_once()  # 队列空了

    assert (first, second, third) == (1, 1, 0)
    async with session_scope() as session:
        row_none = await session.get(DeliveryJob, job_none)
        row_ok = await session.get(DeliveryJob, job_ok)
    assert row_none.status == JOB_SKIPPED
    assert "租户" in (row_none.last_error or "")
    assert row_ok.status == JOB_SUCCESS


async def test_register_handlers_skips_tenant_without_account(db) -> None:
    """启动注册：没号的租户跳过，counts 里只记有号的租户。"""
    from app.services.runtime_service import RuntimeService

    db.app.demo_mode = True  # 演练模式：有账号就返回模拟连接，不真连 Telegram

    tenant_none = await _make_tenant(db, username="p106-reg-none")
    await _add_route(db, tenant_none, tg_id=9340)

    tenant_ok = await _make_tenant(db, username="p106-reg-ok")
    await _add_account(db, tenant_ok, name="注册号", phone="+8613800000204", active=False)
    await _add_route(db, tenant_ok, tg_id=9350)

    service = RuntimeService(db, poll_interval=0.01)
    counts = await service._register_handlers()

    assert counts["tenants"] == [tenant_ok]
    assert tenant_none not in counts["tenants"]
