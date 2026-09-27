"""P4-05：到期提醒（阶段规则、去重、停用/过期不催、后台"已提醒"标记）。"""

from __future__ import annotations

import json
from datetime import timedelta

from conftest import auth_header, login

from app.db.base import utc_now
from app.db.models import CHANNEL_INAPP, CHANNEL_TELEGRAM, SELF_TENANT_ID
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


class _FakeNotifyClient:
    """通知 Bot 替身：只记录发出去的文本，不联网。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail
        self.closed = False

    async def send_message(self, chat_id, text):  # noqa: ANN001, ANN201
        if self.fail:
            raise RuntimeError("模拟发送失败")
        self.sent.append((str(chat_id), text))
        return {}

    async def close(self) -> None:
        self.closed = True


async def _make_notify_bot(
    config,
    *,
    tenant_id: int,
    admin_ids: list[int],
    token: str,
    name: str = "通知Bot",
) -> int:
    """直接落一个「默认通知 Bot」（绕过 Token 校验，测试不联网）。"""
    from app.core.security import FieldCipher
    from app.db.models import ControlBot
    from app.db.tenant_context import tenant_scope

    cipher = FieldCipher.from_config(config)
    async with session_scope() as session:
        with tenant_scope(tenant_id):
            bot = ControlBot(
                name=name,
                token_enc=cipher.encrypt(token),
                admin_ids=json.dumps(admin_ids),
                is_default=True,
                enabled=True,
            )
            session.add(bot)
            await session.commit()
            return int(bot.id)


async def test_deliver_sends_via_tenant_notify_bot(api_config, client) -> None:
    """租户配了默认通知 Bot：提醒按 TG 发出，渠道记成 telegram。"""
    from app.services import reminder_service

    agent_id = await _make_agent(api_config, "rem-tg-agent")
    tenant_id = await _make_member(api_config, "rem-tg-a", days=2, owner_agent_id=agent_id)
    await _make_notify_bot(
        api_config,
        tenant_id=tenant_id,
        admin_ids=[555000111],
        token="token-a",
    )

    client = _FakeNotifyClient()

    async def factory(_config, token):  # noqa: ANN001, ANN202
        assert token == "token-a"
        return client

    async with session_scope() as session:
        created = await reminder_service.sweep(session)
        outcome = await reminder_service.deliver(
            session,
            created["rows"],
            config=api_config,
            sender_factory=factory,
        )
        channels = [row.channel for row in created["rows"]]

    assert created["count"] == 3  # 代理 7 天 / 3 天 + 会员 3 天
    assert outcome["sent"] == 3
    assert outcome["failed"] == 0
    assert channels == [CHANNEL_TELEGRAM] * 3
    assert client.closed is True
    assert len(client.sent) == 3
    assert all(target == "555000111" for target, _ in client.sent)
    texts = " ".join(text for _, text in client.sent)
    assert "预留额度" in texts  # 代理文案
    assert "到期时间" in texts


async def test_deliver_without_bot_keeps_inapp(api_config, client) -> None:
    """没配通知 Bot：一条都不发，提醒留在站内渠道（与接 TG 之前一致）。"""
    from app.services import reminder_service

    agent_id = await _make_agent(api_config, "rem-nb-agent")
    await _make_member(api_config, "rem-nb-a", days=2, owner_agent_id=agent_id)

    async with session_scope() as session:
        created = await reminder_service.sweep(session)
        outcome = await reminder_service.deliver(session, created["rows"], config=api_config)
        channels = [row.channel for row in created["rows"]]

    assert created["count"] == 3
    assert outcome == {"sent": 0, "failed": 0, "skipped": 3, "messages": 0}
    assert set(channels) == {CHANNEL_INAPP}


async def test_deliver_falls_back_to_self_tenant_bot(api_config, client) -> None:
    """会员租户没配 Bot，但自营租户配了：用平台出口发，收件人是平台管理员。"""
    from app.services import reminder_service

    agent_id = await _make_agent(api_config, "rem-fb-agent")
    await _make_member(api_config, "rem-fb-a", days=2, owner_agent_id=agent_id)
    await _make_notify_bot(
        api_config,
        tenant_id=SELF_TENANT_ID,
        admin_ids=[900001],
        token="token-self",
        name="平台通知Bot",
    )

    client = _FakeNotifyClient()

    async def factory(_config, token):  # noqa: ANN001, ANN202
        assert token == "token-self"
        return client

    async with session_scope() as session:
        created = await reminder_service.sweep(session)
        outcome = await reminder_service.deliver(
            session,
            created["rows"],
            config=api_config,
            sender_factory=factory,
        )

    assert outcome["sent"] == 3
    assert len(client.sent) == 3
    assert all(target == "900001" for target, _ in client.sent)


async def test_deliver_failure_keeps_inapp_without_blocking_others(api_config, client) -> None:
    """一个租户通知失败只让它留在站内，别的租户照常收到。"""
    from app.services import reminder_service

    agent_id = await _make_agent(api_config, "rem-iso-agent")
    bad_tenant = await _make_member(api_config, "rem-iso-bad", days=2, owner_agent_id=agent_id)
    ok_tenant = await _make_member(api_config, "rem-iso-ok", days=2, owner_agent_id=agent_id)
    await _make_notify_bot(api_config, tenant_id=bad_tenant, admin_ids=[111], token="token-bad")
    await _make_notify_bot(
        api_config,
        tenant_id=ok_tenant,
        admin_ids=[222],
        token="token-ok",
        name="通知Bot2",
    )

    bad = _FakeNotifyClient(fail=True)
    good = _FakeNotifyClient()
    clients = {"token-bad": bad, "token-ok": good}

    async def factory(_config, token):  # noqa: ANN001, ANN202
        return clients[token]

    async with session_scope() as session:
        created = await reminder_service.sweep(session)
        outcome = await reminder_service.deliver(
            session,
            created["rows"],
            config=api_config,
            sender_factory=factory,
        )
        by_tenant: dict[int, set[str]] = {}
        for row in created["rows"]:
            by_tenant.setdefault(row.tenant_id, set()).add(row.channel)

    assert created["count"] == 6
    assert outcome["sent"] == 3
    assert outcome["failed"] == 3
    assert by_tenant[bad_tenant] == {CHANNEL_INAPP}
    assert by_tenant[ok_tenant] == {CHANNEL_TELEGRAM}
    assert len(good.sent) == 3
    assert good.closed is True
    assert bad.closed is True
