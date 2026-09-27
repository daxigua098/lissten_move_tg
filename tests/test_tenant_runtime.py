"""P4-03：租户运行开关、投递前实时过滤、心跳巡检与在途任务取消。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.core.errors import ValidationFailedError
from app.db.base import utc_now
from app.db.models import (
    JOB_CANCELLED,
    JOB_PENDING,
    STOP_REASON_EXPIRED,
    STOP_REASON_MANUAL,
    STOP_REASON_SUSPENDED,
    TENANT_STATUS_SUSPENDED,
    DeliveryJob,
    Route,
    Tenant,
    TenantChat,
)
from app.db.session import session_scope
from app.services import (
    provision_service,
    quota_service,
    tenant_runtime_service,
    tenant_service,
    user_service,
)


async def _make_tenant(
    db,
    *,
    username: str,
    expires_at=None,
    runtime_enabled: bool = True,
    quota_type: str = "none",
    quota_held: bool = False,
    owner_agent_id: int | None = None,
) -> int:
    """建一个会员账号 + 租户，返回租户 ID。"""
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
            expires_at=expires_at,
            quota_type=quota_type,
            quota_held=quota_held,
            owner_agent_id=owner_agent_id,
        )
        tenant.runtime_enabled = runtime_enabled
        await session.commit()
        return tenant.id


async def _make_agent(db, *, username: str) -> int:
    async with session_scope() as session:
        agent = await user_service.create_user(
            session,
            db,
            username=username,
            password="Pass1234",
            account_type="agent",
        )
        return agent.id


async def _grant_and_open_member(db, *, agent_id: int, username: str) -> tuple[int, dict]:
    """给代理 1 个会员额度，再用它开一个会员号，返回（租户 ID，开号结果）。"""
    async with session_scope() as session:
        agent = await user_service.get_user(session, agent_id)
        await quota_service.manual_adjust(
            session,
            actor=agent,
            target=agent,
            quota_type="member",
            delta=1,
            note="P4 测试发放",
        )
        opened = await provision_service.open_member(
            session,
            db,
            actor=agent,
            username=username,
            days=30,
            modules=["carry"],
        )
    return int(opened["tenant"]["id"]), opened


async def _add_route(db, tenant_id: int, *, enabled: bool = True, tg_id: int = 9001) -> int:
    """在指定租户下建一条 A 线（只为拿一个投递任务的载体）。"""
    from app.core.telegram_client import ChatProfile
    from app.services import chat_service

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
            enabled=enabled,
        )
        session.add(route)
        await session.commit()
        return route.id


async def _add_job(db, tenant_id: int, route_id: int, *, message_id: int = 501) -> int:
    """直接塞一条待投递任务（投递引擎不是本文件的测试对象）。"""
    async with session_scope() as session:
        route = await session.get(Route, route_id)
        chat = await session.get(TenantChat, route.source_chat_id)
        job = DeliveryJob(
            route_id=route_id,
            source_chat_id=route.source_chat_id,
            source_message_id=message_id,
            target_chat_id=chat.id,
            status=JOB_PENDING,
            tenant_id=tenant_id,
        )
        session.add(job)
        await session.commit()
        return job.id


async def test_gate_blocks_when_switch_off(db) -> None:
    tenant_runtime_service.clear_cache()
    tenant_id = await _make_tenant(db, username="p4-off", runtime_enabled=False)

    async with session_scope() as session:
        assert await tenant_runtime_service.is_runtime_allowed(session, tenant_id) is False


async def test_gate_blocks_expired_even_with_switch_on(db) -> None:
    """开关开着也不能投：到期日在过去，投递前现算就挡住。"""
    tenant_runtime_service.clear_cache()
    tenant_id = await _make_tenant(
        db,
        username="p4-expired",
        expires_at=utc_now() - timedelta(hours=1),
        runtime_enabled=True,
    )

    async with session_scope() as session:
        assert await tenant_runtime_service.is_runtime_allowed(session, tenant_id) is False


async def test_gate_cache_ttl_and_invalidate(db, monkeypatch) -> None:
    """放行结果缓存 30 秒；被挡结果 5 秒回源；invalidate 立即生效。"""
    tenant_runtime_service.clear_cache()
    clock = {"t": 1000.0}
    monkeypatch.setattr(tenant_runtime_service, "_monotonic", lambda: clock["t"])
    tenant_id = await _make_tenant(db, username="p4-cache", runtime_enabled=True)

    async with session_scope() as session:
        assert await tenant_runtime_service.is_runtime_allowed(session, tenant_id) is True
    assert tenant_runtime_service.cache_size() == 1

    # 库里关掉开关，但放行缓存还在 30 秒内 → 仍然放行（这是设计里接受的延迟）
    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.runtime_enabled = False
        await session.commit()
    clock["t"] += 1.0
    async with session_scope() as session:
        assert await tenant_runtime_service.is_runtime_allowed(session, tenant_id) is True

    # 超过 TTL 就回源，读到关闭
    clock["t"] += 31.0
    async with session_scope() as session:
        assert await tenant_runtime_service.is_runtime_allowed(session, tenant_id) is False

    # 重新打开开关：被挡缓存 5 秒内仍然挡着，过了就立即恢复
    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.runtime_enabled = True
        await session.commit()
    clock["t"] += 1.0
    async with session_scope() as session:
        assert await tenant_runtime_service.is_runtime_allowed(session, tenant_id) is False
    clock["t"] += 5.0
    async with session_scope() as session:
        assert await tenant_runtime_service.is_runtime_allowed(session, tenant_id) is True

    # 主动失效：会员点「停止」后同一进程内的判断立刻变
    tenant_runtime_service.invalidate(tenant_id)
    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.runtime_enabled = False
        await session.commit()
    async with session_scope() as session:
        assert await tenant_runtime_service.is_runtime_allowed(session, tenant_id) is False


async def test_sweep_expires_releases_quota_and_cancels_jobs(db) -> None:
    """巡检：置过期痕迹、关开关、释放额度、取消排队任务；再跑一次是空操作。"""
    tenant_runtime_service.clear_cache()
    agent_id = await _make_agent(db, username="p4-agent")
    tenant_id, opened = await _grant_and_open_member(db, agent_id=agent_id, username="p4-cust")
    assert opened["tenant"]["quota_held"] is True
    route_id = await _add_route(db, tenant_id)
    job_id = await _add_job(db, tenant_id, route_id)

    # 把到期时间推到过去，模拟"睡过切点"
    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.expires_at = utc_now() - timedelta(seconds=1)
        await session.commit()

    async with session_scope() as session:
        summary = await tenant_runtime_service.sweep_once(session)

    assert summary["expired"] == [tenant_id]
    assert summary["released"] == [tenant_id]
    assert summary["cancelled_jobs"] == 1

    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        job = await session.get(DeliveryJob, job_id)
        quota = await quota_service.get_quota(session, agent_id)

    assert tenant.expired_at is not None
    assert tenant.runtime_enabled is False
    assert tenant.runtime_stop_reason == STOP_REASON_EXPIRED
    assert tenant.quota_held is False
    assert quota.member_quota == 1  # 1 个发出去 → 到期释放回 1 个
    assert job.status == JOB_CANCELLED

    # 幂等：再巡检一次不重复释放、不重复取消
    async with session_scope() as session:
        again = await tenant_runtime_service.sweep_once(session)

    assert again["expired"] == []
    assert again["released"] == []
    assert again["cancelled_jobs"] == 0


async def test_sweep_suspend_does_not_release_quota(db) -> None:
    """停用只强停、不释放额度（额度仍握在代理手上）。"""
    tenant_runtime_service.clear_cache()
    agent_id = await _make_agent(db, username="p4-agent2")
    tenant_id, _opened = await _grant_and_open_member(db, agent_id=agent_id, username="p4-cust2")
    route_id = await _add_route(db, tenant_id, tg_id=9002)
    job_id = await _add_job(db, tenant_id, route_id, message_id=502)

    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.status = TENANT_STATUS_SUSPENDED
        await session.commit()

    async with session_scope() as session:
        summary = await tenant_runtime_service.sweep_once(session)

    assert summary["suspended"] == [tenant_id]
    assert summary["released"] == []
    assert summary["cancelled_jobs"] == 1

    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        job = await session.get(DeliveryJob, job_id)
        quota = await quota_service.get_quota(session, agent_id)

    assert tenant.runtime_enabled is False
    assert tenant.runtime_stop_reason == STOP_REASON_SUSPENDED
    assert tenant.quota_held is True  # 停用不释放
    assert quota.member_quota == 0
    assert job.status == JOB_CANCELLED


async def test_stop_and_start_tenant_runtime(db) -> None:
    """会员的启动 / 停止：只改租户开关，并取消排队任务。"""
    tenant_runtime_service.clear_cache()
    tenant_id = await _make_tenant(db, username="p4-manual")
    route_id = await _add_route(db, tenant_id, tg_id=9003)
    job_id = await _add_job(db, tenant_id, route_id, message_id=503)

    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        stopped = await tenant_runtime_service.stop_tenant_runtime(session, tenant)

    assert stopped["runtime_enabled"] is False
    assert stopped["runtime_stop_reason"] == STOP_REASON_MANUAL
    assert stopped["cancelled_jobs"] == 1

    async with session_scope() as session:
        job = await session.get(DeliveryJob, job_id)
        assert job.status == JOB_CANCELLED

    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        started = await tenant_runtime_service.start_tenant_runtime(session, tenant)

    assert started["runtime_enabled"] is True
    assert started["runtime_stop_reason"] is None

    async with session_scope() as session:
        assert await tenant_runtime_service.is_runtime_allowed(session, tenant_id) is True


async def test_start_tenant_runtime_rejects_inactive(db) -> None:
    """过期 / 停用的租户不能自己启动，要先由上级续期或解停。"""
    tenant_runtime_service.clear_cache()
    tenant_id = await _make_tenant(
        db,
        username="p4-blocked",
        expires_at=utc_now() - timedelta(days=1),
        runtime_enabled=False,
    )

    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        with pytest.raises(ValidationFailedError):
            await tenant_runtime_service.start_tenant_runtime(session, tenant)


async def test_deliver_once_cancels_jobs_of_stopped_tenant(db) -> None:
    """投递前实时过滤：租户停了之后，队头任务在真正发送前就被取消。

    这条是 P4 的核心：不重启进程、不等巡检，投递循环碰到就该停。
    """
    from app.services.runtime_service import RuntimeService

    tenant_runtime_service.clear_cache()
    tenant_id = await _make_tenant(db, username="p4-deliver", runtime_enabled=True)
    route_id = await _add_route(db, tenant_id, tg_id=9020)
    job_id = await _add_job(db, tenant_id, route_id, message_id=520)

    # 直接改库把开关关掉（等价于"进程外把租户停了"），队列里仍留着任务
    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.runtime_enabled = False
        tenant.runtime_stop_reason = STOP_REASON_MANUAL
        await session.commit()
    tenant_runtime_service.clear_cache()

    service = RuntimeService(db, poll_interval=0.01)
    delivered = await service._deliver_once(client=object())

    assert delivered == 1  # 队列里确实有东西被处理掉了，不是空转
    async with session_scope() as session:
        job = await session.get(DeliveryJob, job_id)
    assert job.status == JOB_CANCELLED
    assert "取消" in (job.last_error or "")


async def test_enable_all_routes(db) -> None:
    """一键启动：把租户下停用的线路恢复为启用，只开不关。"""
    tenant_runtime_service.clear_cache()
    tenant_id = await _make_tenant(db, username="p4-enable")
    first = await _add_route(db, tenant_id, enabled=False, tg_id=9010)
    second = await _add_route(db, tenant_id, enabled=True, tg_id=9011)
    other = await _make_tenant(db, username="p4-other")
    foreign = await _add_route(db, other, enabled=False, tg_id=9012)

    async with session_scope() as session:
        assert await tenant_runtime_service.enable_all_routes(session, tenant_id) == 1

    async with session_scope() as session:
        assert (await session.get(Route, first)).enabled is True
        assert (await session.get(Route, second)).enabled is True
        assert (await session.get(Route, foreign)).enabled is False  # 别的租户不受影响
