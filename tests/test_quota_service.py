"""P3-02/P3-03：额度划拨、回收、扣减、对账与流水。"""

from __future__ import annotations

import asyncio

import pytest

from app.core.errors import (
    InsufficientQuotaError,
    NotDirectSubordinateError,
    PermissionDeniedError,
    ValidationFailedError,
)


async def _make_user(session, config, username, *, account_type="platform", parent_user_id=None):
    from app.services import user_service

    return await user_service.create_user(
        session,
        config,
        username=username,
        password="Passw0rd12345",
        account_type=account_type,
        parent_user_id=parent_user_id,
        must_change_password=False,
    )


async def _grant(session, *, actor, target, quota_type="member", count=10):
    from app.services import quota_service

    return await quota_service.manual_adjust(
        session,
        actor=actor,
        target=target,
        quota_type=quota_type,
        delta=count,
        note="测试发放",
    )


async def test_allocate_moves_quota_and_writes_paired_ledger(db) -> None:
    """一次划拨：上级减、下级增，总量守恒，两行流水共享 transfer_id。"""
    from app.db.session import session_scope
    from app.services import quota_service

    async with session_scope() as session:
        admin = await _make_user(session, db, "qa-admin")
        agent = await _make_user(session, db, "qa-agent", account_type="agent")
        child = await _make_user(
            session, db, "qa-agent-child", account_type="agent", parent_user_id=agent.id
        )
        agent_id, child_id = agent.id, child.id
        await _grant(session, actor=admin, target=agent, count=10)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        target = await _load(session, child_id)
        result = await quota_service.allocate(
            session,
            actor=actor,
            target=target,
            quota_type="member",
            count=4,
        )

    async with session_scope() as session:
        assert await quota_service.balance_of(session, agent_id, "member") == 6
        assert await quota_service.balance_of(session, child_id, "member") == 4
        rows, total = await quota_service.list_ledger(
            session, subject_user_ids=[agent_id, child_id]
        )

    assert total == 3  # 1 行调账 + 2 行划拨
    transfer = [row for row in rows if row.transfer_id == result["transfer_id"]]
    assert len(transfer) == 2
    assert {row.change for row in transfer} == {-4, 4}
    assert {row.action for row in transfer} == {"allocate_out", "allocate_in"}


async def _load(session, user_id: int):
    from app.db.models import User

    return await session.get(User, user_id)


async def test_allocate_total_is_conserved(db) -> None:
    """划拨只是搬家：所有相关账号的余额合计不变。"""
    from app.db.session import session_scope
    from app.services import quota_service

    async with session_scope() as session:
        admin = await _make_user(session, db, "qc-admin")
        agent = await _make_user(session, db, "qc-agent", account_type="agent")
        child = await _make_user(
            session, db, "qc-child", account_type="agent", parent_user_id=agent.id
        )
        agent_id, child_id = agent.id, child.id
        await _grant(session, actor=admin, target=agent, count=7)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        target = await _load(session, child_id)
        await quota_service.allocate(
            session, actor=actor, target=target, quota_type="member", count=7
        )

    async with session_scope() as session:
        total = sum(
            [
                await quota_service.balance_of(session, agent_id, "member"),
                await quota_service.balance_of(session, child_id, "member"),
            ]
        )
    assert total == 7


async def test_allocate_to_grandchild_is_forbidden(db) -> None:
    """隔层不能划拨：只能操作直属下级。"""
    from app.db.session import session_scope
    from app.services import quota_service

    async with session_scope() as session:
        admin = await _make_user(session, db, "qd-admin")
        agent = await _make_user(session, db, "qd-agent", account_type="agent")
        child = await _make_user(
            session, db, "qd-child", account_type="agent", parent_user_id=agent.id
        )
        grand = await _make_user(
            session, db, "qd-grand", account_type="agent", parent_user_id=child.id
        )
        agent_id, grand_id = agent.id, grand.id
        await _grant(session, actor=admin, target=agent, count=5)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        target = await _load(session, grand_id)
        with pytest.raises(NotDirectSubordinateError):
            await quota_service.allocate(
                session, actor=actor, target=target, quota_type="member", count=1
            )


async def test_platform_cannot_allocate(db) -> None:
    """平台账号没有余额行，划拨这类动作只属于代理。"""
    from app.db.session import session_scope
    from app.services import quota_service

    async with session_scope() as session:
        admin = await _make_user(session, db, "qe-admin")
        agent = await _make_user(
            session, db, "qe-agent", account_type="agent", parent_user_id=admin.id
        )
        admin_id, agent_id = admin.id, agent.id

    async with session_scope() as session:
        actor = await _load(session, admin_id)
        target = await _load(session, agent_id)
        with pytest.raises(PermissionDeniedError):
            await quota_service.allocate(
                session, actor=actor, target=target, quota_type="member", count=1
            )


async def test_insufficient_quota_raises_and_leaves_balance(db) -> None:
    """额度不足时报错且余额不变（不会扣成负数）。"""
    from app.db.session import session_scope
    from app.services import quota_service

    async with session_scope() as session:
        admin = await _make_user(session, db, "qf-admin")
        agent = await _make_user(session, db, "qf-agent", account_type="agent")
        agent_id = agent.id
        await _grant(session, actor=admin, target=agent, count=2)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        with pytest.raises(InsufficientQuotaError):
            await quota_service.consume(
                session,
                actor=actor,
                subject=actor,
                quota_type="member",
                action="open_member",
                count=3,
            )

    async with session_scope() as session:
        assert await quota_service.balance_of(session, agent_id, "member") == 2
        _rows, total = await quota_service.list_ledger(
            session, subject_user_id=agent_id, action="open_member"
        )
    assert total == 0


async def test_reclaim_only_takes_unused_balance(db) -> None:
    """回收只能拿未使用的余额：已开号占用的部分拿不回来。"""
    from app.db.session import session_scope
    from app.services import quota_service

    async with session_scope() as session:
        admin = await _make_user(session, db, "qg-admin")
        agent = await _make_user(session, db, "qg-agent", account_type="agent")
        child = await _make_user(
            session, db, "qg-child", account_type="agent", parent_user_id=agent.id
        )
        agent_id, child_id = agent.id, child.id
        await _grant(session, actor=admin, target=agent, count=10)

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        target = await _load(session, child_id)
        await quota_service.allocate(
            session, actor=actor, target=target, quota_type="member", count=5
        )

    async with session_scope() as session:
        child = await _load(session, child_id)
        await quota_service.consume(
            session,
            actor=child,
            subject=child,
            quota_type="member",
            action="open_member",
            count=1,
        )

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        target = await _load(session, child_id)
        with pytest.raises(InsufficientQuotaError):
            await quota_service.reclaim(
                session, actor=actor, target=target, quota_type="member", count=5
            )

    async with session_scope() as session:
        actor = await _load(session, agent_id)
        target = await _load(session, child_id)
        await quota_service.reclaim(
            session, actor=actor, target=target, quota_type="member", count=4
        )

    async with session_scope() as session:
        assert await quota_service.balance_of(session, agent_id, "member") == 9
        assert await quota_service.balance_of(session, child_id, "member") == 0


async def test_concurrent_consume_does_not_overspend(db) -> None:
    """并发开号不超发：余额只有 1 时，两个并发的扣减只能成功一个。"""
    from app.db.models import User
    from app.db.session import session_scope
    from app.services import quota_service

    async with session_scope() as session:
        admin = await _make_user(session, db, "qh-admin")
        agent = await _make_user(session, db, "qh-agent", account_type="agent")
        agent_id = agent.id
        await _grant(session, actor=admin, target=agent, count=1)

    async def _try() -> bool:
        async with session_scope() as session:
            actor = await session.get(User, agent_id)
            try:
                await quota_service.consume(
                    session,
                    actor=actor,
                    subject=actor,
                    quota_type="member",
                    action="open_member",
                    count=1,
                )
            except InsufficientQuotaError:
                return False
            return True

    results = await asyncio.gather(_try(), _try(), return_exceptions=True)
    successes = [item for item in results if item is True]

    async with session_scope() as session:
        balance = await quota_service.balance_of(session, agent_id, "member")
        _rows, consumed = await quota_service.list_ledger(
            session, subject_user_id=agent_id, action="open_member"
        )

    assert len(successes) == 1
    assert balance == 0
    assert consumed == 1


async def test_manual_adjust_requires_note_and_reconciles(db) -> None:
    """手工调账必须写备注；流水能倒推出余额，人为改余额会被对账发现。"""
    from app.db.session import session_scope
    from app.services import quota_service

    async with session_scope() as session:
        admin = await _make_user(session, db, "qi-admin")
        agent = await _make_user(session, db, "qi-agent", account_type="agent")
        agent_id = agent.id
        with pytest.raises(ValidationFailedError):
            await quota_service.manual_adjust(
                session,
                actor=admin,
                target=agent,
                quota_type="member",
                delta=3,
                note="  ",
            )
        await quota_service.manual_adjust(
            session,
            actor=admin,
            target=agent,
            quota_type="member",
            delta=3,
            note="补偿发放",
        )

    async with session_scope() as session:
        report = await quota_service.reconcile(session, agent_id)
    assert report["consistent"] is True
    assert report["balances"]["member"] == 3
    assert report["ledger_sums"]["member"] == 3

    async with session_scope() as session:
        from app.db.models import AgentQuota

        row = await session.get(AgentQuota, (await quota_service.get_quota(session, agent_id)).id)
        row.member_quota = 99
        await session.commit()

    async with session_scope() as session:
        report = await quota_service.reconcile(session, agent_id)
    assert report["consistent"] is False
    assert report["ledger_sums"]["member"] == 3
