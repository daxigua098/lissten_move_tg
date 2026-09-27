"""P6-01：端到端验收（超管 → 代理 → 额度 → 会员 → 线路 → 到期强停 → 续期手动恢复）。

这一条把 P2/P3/P4 的关键承诺串起来跑一遍：

1. 平台开一级代理，并给代理划拨 1 个会员额度；
2. 代理用这个额度开一个 30 天会员（额度被占用）；
3. 会员首次登录改密后配置自己的 TG 账号、把线路跑起来；
4. 到期后巡检强停：线路停跑、在途任务取消、额度释放回代理；
5. 平台续期：只改到期日，**不自动恢复运行**；会员自己手动启动才恢复。

缺口标记：会员配的 TG 账号目前落在**自营租户**而不是会员租户
（业务表 tenant_id 的写入/查询尚未按身份收口，即任务清单 P1-05），
用 ``xfail(strict=True)`` 固化，修好后这条会 XPASS，提示去掉标记。
"""

from __future__ import annotations

import pytest
from conftest import auth_header, login

from app.db.base import utc_now
from app.db.session import session_scope

AGENT_PASSWORD = "AgentPass123"
MEMBER_PASSWORD = "MemberPass123"


async def _platform_token(client) -> str:
    response = await login(client)
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def _first_login(client, username: str, initial: str, final: str) -> str:
    """首次登录 → 改密 → 重新登录，返回可用令牌。"""
    token = (await login(client, username, initial)).json()["token"]
    changed = await client.patch(
        "/api/auth/password",
        json={"current_password": initial, "new_password": final},
        headers=auth_header(token),
    )
    assert changed.status_code == 200, changed.text
    response = await login(client, username, final)
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def _open_agent(client, platform: str, *, username: str) -> int:
    response = await client.post(
        "/api/agent/agents",
        json={"username": username, "password": AGENT_PASSWORD},
        headers=auth_header(platform),
    )
    assert response.status_code == 201, response.text
    return int(response.json()["account"]["id"])


async def _grant(client, platform: str, agent_id: int, quota_type: str, delta: int) -> dict:
    response = await client.post(
        f"/api/agent/{agent_id}/adjust",
        json={"quota_type": quota_type, "delta": delta, "note": "端到端验收发放"},
        headers=auth_header(platform),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _open_member(client, agent_token: str, *, username: str, days: int = 30) -> dict:
    response = await client.post(
        "/api/agent/members",
        json={"username": username, "days": days, "modules": ["carry", "monitor"]},
        headers=auth_header(agent_token),
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _add_route_and_job(tenant_id: int, *, tg_id: int = 9101) -> tuple[int, int]:
    """服务层直接铺一条线路 + 一条待投递任务（投递引擎不是本用例的对象）。"""
    from app.core.telegram_client import ChatProfile
    from app.db.models import JOB_PENDING, DeliveryJob, Route
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
            name=f"验收线路{tg_id}",
            source_chat_id=chat.id,
            business_type="A",
            tenant_id=tenant_id,
            enabled=True,
        )
        session.add(route)
        await session.flush()
        job = DeliveryJob(
            route_id=route.id,
            source_chat_id=chat.id,
            source_message_id=601,
            target_chat_id=chat.id,
            status=JOB_PENDING,
            tenant_id=tenant_id,
        )
        session.add(job)
        await session.commit()
        return route.id, job.id


async def _tenant_row(tenant_id: int):
    from app.db.models import Tenant

    async with session_scope() as session:
        return await session.get(Tenant, tenant_id)


async def _job_status(job_id: int) -> str:
    from app.db.models import DeliveryJob

    async with session_scope() as session:
        return (await session.get(DeliveryJob, job_id)).status


async def _sweep() -> dict:
    from app.services import tenant_runtime_service

    async with session_scope() as session:
        return await tenant_runtime_service.sweep_once(session)


async def test_full_chain_from_agent_to_expiry_and_manual_restart(admin_client) -> None:
    from app.db.models import STOP_REASON_EXPIRED
    from app.services import quota_service

    platform = await _platform_token(admin_client)
    agent_id = await _open_agent(admin_client, platform, username="e2e-agent")

    granted = await _grant(admin_client, platform, agent_id, "member", 1)
    assert granted["quota"]["member"] == 1

    agent = await _first_login(admin_client, "e2e-agent", AGENT_PASSWORD, AGENT_PASSWORD)
    opened = await _open_member(admin_client, agent, username="e2e-cust")
    tenant_id = int(opened["tenant"]["id"])
    member_user_id = int(opened["account"]["id"])
    assert opened["tenant"]["owner_agent_id"] == agent_id
    assert opened["tenant"]["quota_held"] is True

    member = await _first_login(
        admin_client, "e2e-cust", opened["initial_password"], MEMBER_PASSWORD
    )

    # 会员配置自己的 TG 账号（账号与机器人是所有功能的基础）
    account = await admin_client.post(
        "/api/accounts",
        json={
            "name": "e2e-主号",
            "phone": "+8613800009999",
            "api_id": 123456,
            "api_hash": "abcdef0123456789abcdef0123456789",
        },
        headers=auth_header(member),
    )
    assert account.status_code == 201, account.text

    # 会员把线路跑起来
    _route_id, job_id = await _add_route_and_job(tenant_id)
    started = await admin_client.post("/api/runtime/start", headers=auth_header(member))
    assert started.status_code == 200, started.text
    assert started.json()["tenant"]["runtime_enabled"] is True

    # 到期：巡检强停（把到期时间推到过去，模拟睡过切点）
    from datetime import timedelta

    from app.db.models import Tenant

    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.expires_at = utc_now() - timedelta(seconds=1)
        await session.commit()

    summary = await _sweep()
    assert tenant_id in summary["expired"]
    assert summary["released"] == [tenant_id]
    assert summary["cancelled_jobs"] == 1

    tenant = await _tenant_row(tenant_id)
    assert tenant.runtime_enabled is False
    assert tenant.runtime_stop_reason == STOP_REASON_EXPIRED
    assert tenant.quota_held is False
    assert await _job_status(job_id) == "cancelled"

    async with session_scope() as session:
        agent_quota = await quota_service.get_quota(session, agent_id)
    assert agent_quota.member_quota == 1  # 到期释放，额度回到代理账上

    # 过期后写操作一律 403（只读放行）
    blocked = await admin_client.post(
        "/api/accounts",
        json={
            "name": "e2e-过期后加的号",
            "phone": "+8613800008888",
            "api_id": 123456,
            "api_hash": "abcdef0123456789abcdef0123456789",
        },
        headers=auth_header(member),
    )
    assert blocked.status_code == 403, blocked.text
    assert blocked.json()["code"] == "TENANT_INACTIVE"

    # 平台续期：只改到期日，不自动恢复运行
    renewed = await admin_client.post(
        f"/api/platform/members/{member_user_id}/renew",
        json={"days": 30},
        headers=auth_header(platform),
    )
    assert renewed.status_code == 200, renewed.text
    tenant = await _tenant_row(tenant_id)
    assert tenant.runtime_enabled is False
    assert tenant.runtime_stop_reason == STOP_REASON_EXPIRED  # 续期不清停止原因
    assert tenant.quota_held is True  # 续期重新占用 1 个会员额度

    # 会员登录后手动启动，功能才恢复
    restarted = await admin_client.post("/api/runtime/start", headers=auth_header(member))
    assert restarted.status_code == 200, restarted.text
    body = restarted.json()["tenant"]
    assert body["runtime_enabled"] is True
    assert body["runtime_stop_reason"] is None


@pytest.mark.xfail(
    strict=True,
    reason="P1-05 未收口：业务表 tenant_id 写入仍走默认自营租户，会员配的 TG 账号不归自己",
)
async def test_member_tg_account_belongs_to_his_own_tenant(admin_client) -> None:
    """会员配的 TG 账号应该归到会员租户名下（账号与机器人的归属承诺）。"""
    from sqlalchemy import select

    from app.db.models import TgAccount

    platform = await _platform_token(admin_client)
    agent_id = await _open_agent(admin_client, platform, username="e2e-owner-agent")
    await _grant(admin_client, platform, agent_id, "member", 1)
    agent = await _first_login(admin_client, "e2e-owner-agent", AGENT_PASSWORD, AGENT_PASSWORD)
    opened = await _open_member(admin_client, agent, username="e2e-owner-cust")
    tenant_id = int(opened["tenant"]["id"])
    member = await _first_login(
        admin_client, "e2e-owner-cust", opened["initial_password"], MEMBER_PASSWORD
    )

    created = await admin_client.post(
        "/api/accounts",
        json={
            "name": "e2e-归属号",
            "phone": "+8613800007777",
            "api_id": 123456,
            "api_hash": "abcdef0123456789abcdef0123456789",
        },
        headers=auth_header(member),
    )
    assert created.status_code == 201, created.text

    async with session_scope() as session:
        owner = await session.scalar(
            select(TgAccount.tenant_id).where(TgAccount.name == "e2e-归属号")
        )
    assert owner == tenant_id
