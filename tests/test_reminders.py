"""P4-05：到期提醒（阶段规则、去重、停用/过期不催、后台"已提醒"标记）。"""

from __future__ import annotations

from datetime import timedelta

from conftest import auth_header, login

from app.db.base import utc_now
from app.db.session import session_scope


async def _make_agent(config, username: str) -> int:
    from app.services import user_service

    async with session_scope() as session:
        user = await user_service.create_user(
            session,
            config,
            username=username,
            password="AgentPass123",
            account_type="agent",
            must_change_password=False,
        )
        await session.commit()
        return user.id


async def _make_member(
    config,
    username: str,
    *,
    days: int | None,
    owner_agent_id: int | None = None,
    status: str = "active",
    quota_type: str = "member",
    held: bool = True,
) -> int:
    """建一个会员 + 租户，返回 tenant_id。"""
    from app.services import tenant_service, user_service

    async with session_scope() as session:
        user = await user_service.create_user(
            session,
            config,
            username=username,
            password="Member123456",
            account_type="member",
            parent_user_id=owner_agent_id,
            must_change_password=False,
        )
        await session.flush()
        tenant = await tenant_service.create_tenant(
            session,
            name=f"t-{username}",
            owner_user_id=user.id,
            owner_agent_id=owner_agent_id,
            quota_type=quota_type,
            quota_held=held,
            status=status,
            expires_at=None if days is None else utc_now() + timedelta(days=days),
        )
        user.tenant_id = tenant.id
        await session.commit()
        return tenant.id


async def _shift(tenant_id: int, *, days: int) -> None:
    """把到期时间挪到"还剩 `days` 个自然日"（按到期日 23:59:59 断的规则）。"""
    from app.core.expiry import expiry_for_days
    from app.db.models import Tenant

    async with session_scope() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.expires_at = expiry_for_days(days + 1)
        await session.commit()


async def _sweep() -> dict:
    from app.services import reminder_service

    async with session_scope() as session:
        return await reminder_service.sweep(session)


def _codes(result: dict) -> set[tuple[str, str]]:
    return {(row["stage"], row["audience"]) for row in result["created"]}


async def test_agent_reminded_at_7_3_1_once(api_config, client) -> None:
    """代理按 7/3/1 天各提醒一次，会员只按 3/1 天提醒。"""
    agent_id = await _make_agent(api_config, "rem-agent")
    far = await _make_member(api_config, "rem-far", days=40, owner_agent_id=agent_id)
    near = await _make_member(api_config, "rem-near", days=6, owner_agent_id=agent_id)

    first = await _sweep()
    assert {row["tenant_id"] for row in first["created"]} == {near}  # 40 天在窗口外
    assert _codes(first) == {("7d", "agent")}  # 6 天：只到代理的 7 天阶段
    assert far not in {row["tenant_id"] for row in first["created"]}

    again = await _sweep()
    assert again["count"] == 0  # 同一阶段不会重复提醒

    await _shift(near, days=2)
    third = await _sweep()
    assert _codes(third) == {("3d", "agent"), ("3d", "member")}

    await _shift(near, days=0)
    fourth = await _sweep()
    assert _codes(fourth) == {("1d", "agent"), ("1d", "member")}

    # 提醒文案：代理那侧要带"请预留额度"
    from sqlalchemy import select

    from app.db.models import AUDIENCE_AGENT, TenantReminder

    async with session_scope() as session:
        rows = list(
            await session.scalars(
                select(TenantReminder).where(
                    TenantReminder.tenant_id == near,
                    TenantReminder.audience == AUDIENCE_AGENT,
                )
            )
        )
    assert {row.stage for row in rows} == {"7d", "3d", "1d"}
    assert all("预留额度" in (row.note or "") for row in rows)


async def test_suspended_expired_and_unbounded_tenants_are_skipped(api_config, client) -> None:
    """停用中不催、已过期不再生成、永不过期的不提醒；平台直开只提醒会员。"""
    agent_id = await _make_agent(api_config, "rem-agent2")
    suspended = await _make_member(
        api_config, "rem-susp", days=1, owner_agent_id=agent_id, status="suspended"
    )
    expired = await _make_member(api_config, "rem-expired", days=-2, owner_agent_id=agent_id)
    forever = await _make_member(api_config, "rem-forever", days=None, owner_agent_id=agent_id)
    direct = await _make_member(
        api_config,
        "rem-direct",
        days=1,
        owner_agent_id=None,
        quota_type="none",
        held=False,
    )

    result = await _sweep()
    touched = {row["tenant_id"] for row in result["created"]}
    assert suspended not in touched
    assert expired not in touched
    assert forever not in touched
    assert direct in touched
    # 平台直开的号没有代理可提醒，只有会员受众
    assert {(row["stage"], row["audience"]) for row in result["created"]} == {
        ("3d", "member"),
        ("1d", "member"),
    }


async def test_stages_by_tenant_dedupes_across_audiences(api_config, client) -> None:
    """列表展示用的阶段编码按阶段去重（同一阶段只显示一次）。"""
    from app.services import reminder_service

    agent_id = await _make_agent(api_config, "rem-agent3")
    tenant_id = await _make_member(api_config, "rem-both", days=2, owner_agent_id=agent_id)
    await _sweep()

    async with session_scope() as session:
        stages = await reminder_service.stages_by_tenant(session, [tenant_id])
    assert stages[tenant_id] == ["7d", "3d"]


async def test_expiring_lists_show_reminded_mark(admin_client, api_config) -> None:
    """代理工作台与平台到期看板都带"已提醒"标记。"""

    agent_id = await _make_agent(api_config, "rem-api-agent")
    tenant_id = await _make_member(api_config, "rem-api-c", days=2, owner_agent_id=agent_id)
    await _sweep()

    agent_token = (await login(admin_client, "rem-api-agent", "AgentPass123")).json()["token"]
    expiring = await admin_client.get("/api/agent/expiring", headers=auth_header(agent_token))
    assert expiring.status_code == 200, expiring.text
    item = next(row for row in expiring.json()["items"] if row["tenant_id"] == tenant_id)
    assert item["reminded_stages"] == ["7d", "3d"]

    platform_token = (await login(admin_client)).json()["token"]
    growth = await admin_client.get("/api/platform/expiry", headers=auth_header(platform_token))
    assert growth.status_code == 200, growth.text
    board_items = growth.json()["buckets"]["days3"]["items"]
    target = next(row for row in board_items if row["tenant_id"] == tenant_id)
    assert target["reminded_stages"] == ["7d", "3d"]

    overview = await admin_client.get("/api/platform/overview", headers=auth_header(platform_token))
    assert overview.status_code == 200, overview.text
    overview_item = next(
        row for row in overview.json()["expiring"] if row["tenant_id"] == tenant_id
    )
    assert overview_item["reminded_stages"] == ["7d", "3d"]

    # 续期到窗口外，提醒标记自然从列表消失
    renewed = await admin_client.post(
        f"/api/platform/members/{await _user_id(api_config, 'rem-api-c')}/renew",
        json={"days": 60},
        headers=auth_header(platform_token),
    )
    assert renewed.status_code == 200, renewed.text
    after = await admin_client.get("/api/platform/expiry", headers=auth_header(platform_token))
    assert all(row["tenant_id"] != tenant_id for key in about_buckets(after.json()) for row in key)


def about_buckets(payload: dict) -> list[list[dict]]:
    """到期看板所有分组里的明细。"""
    return [bucket["items"] for bucket in payload["buckets"].values()]


async def _user_id(config, username: str) -> int:
    from sqlalchemy import select

    from app.db.models import User

    async with session_scope() as session:
        value = await session.scalar(select(User.id).where(User.username == username))
    assert value is not None
    return int(value)
