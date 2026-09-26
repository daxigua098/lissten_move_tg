"""加群队列：限速排队、失败分类与审计（F-R12 / F-R14）。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.core.errors import ConflictError
from app.core.resource_probe import ProbeMetrics
from app.db.base import as_utc, utc_now
from app.db.models import (
    JOIN_FAILED,
    JOIN_PENDING,
    JOIN_SUCCESS,
    JOIN_WAITING_APPROVAL,
    RESOURCE_RETIRED,
    AuditLog,
    ResourceJoinTask,
    TgResource,
)
from app.db.session import session_scope
from app.services import resource_join_service, resource_quota_service, resource_service
from app.services.resource_service import ResourceRef


async def _create(tg_id: int | None, title: str, **kwargs) -> TgResource:
    async with session_scope() as session:
        outcome = await resource_service.upsert_resource(
            session,
            ResourceRef(tg_id=tg_id, title=title, **kwargs),
        )
        assert outcome.resource is not None
        return outcome.resource


async def _enqueue(db, resource_id: int, account_id: int, **kwargs) -> int:
    """入队并返回任务 ID（会话关掉后不再持有 ORM 对象）。"""
    async with session_scope() as session:
        fresh = await resource_service.require_resource(session, resource_id)
        task = await resource_join_service.enqueue(
            session,
            db,
            fresh,
            account_id=account_id,
            jitter=False,
            **kwargs,
        )
        return task.id


async def _run(db, client, task_id: int, *, actor: str = "runtime") -> dict:
    async with session_scope() as session:
        task = await session.get(ResourceJoinTask, task_id)
        assert task is not None
        return await resource_join_service.run_task(session, db, client, task, actor=actor)


async def test_enqueue_creates_pending_task(db, probe_account) -> None:
    resource = await _create(3001, "求职群", username="job_group")
    task_id = await _enqueue(db, resource.id, probe_account)

    async with session_scope() as session:
        task = await session.get(ResourceJoinTask, task_id)

    assert task is not None
    assert task.status == JOIN_PENDING
    assert task.action == "join"
    assert task.account_id == probe_account
    assert task.scheduled_at is not None
    assert as_utc(task.scheduled_at) <= utc_now() + timedelta(seconds=5)


async def test_enqueue_is_idempotent_for_same_resource(db, probe_account) -> None:
    resource = await _create(3002, "求职群")
    first = await _enqueue(db, resource.id, probe_account)
    second = await _enqueue(db, resource.id, probe_account)

    assert first == second


async def test_blacklisted_cannot_enqueue(db, probe_account) -> None:
    resource = await _create(3003, "拉黑群")
    async with session_scope() as session:
        row = await resource_service.require_resource(session, resource.id)
        await resource_service.set_blacklisted(session, row, True, reason="噪声")

    try:
        await _enqueue(db, resource.id, probe_account)
    except ConflictError as exc:
        assert "黑名单" in exc.detail
    else:  # pragma: no cover - 走到这里说明没拦住
        raise AssertionError("黑名单资源不该进加群队列")


async def test_adopted_resource_can_still_queue_join(db, probe_account) -> None:
    """已采纳也能补排加群：采纳后才发现账号没进群，必须能救回来。

    排队中的任务会被去重，所以重复点「让账号加入」不会攒出一堆任务。
    """
    resource = await _create(3004, "已采纳群", username="adopted_group")
    async with session_scope() as session:
        row = await resource_service.require_resource(session, resource.id)
        await resource_service.apply_metrics(session, row, ProbeMetrics())
    async with session_scope() as session:
        row = await resource_service.require_resource(session, resource.id)
        await resource_service.mark_adopted(session, row, adopted_by="admin")

    task_id = await _enqueue(db, resource.id, probe_account)
    again = await _enqueue(db, resource.id, probe_account)

    async with session_scope() as session:
        task = await session.get(ResourceJoinTask, task_id)
        assert task is not None
        assert task.status == JOIN_PENDING
    assert again == task_id


async def test_hourly_limit_pushes_next_slot(db, probe_account) -> None:
    db.resource.join_hourly_limit = 1
    first = await _create(3005, "群一")
    second = await _create(3006, "群二")
    task_a = await _enqueue(db, first.id, probe_account)
    task_b = await _enqueue(db, second.id, probe_account)

    async with session_scope() as session:
        a = await session.get(ResourceJoinTask, task_a)
        b = await session.get(ResourceJoinTask, task_b)

    assert b.scheduled_at >= a.scheduled_at + timedelta(hours=1)


async def test_daily_limit_queues_to_tomorrow(db, probe_account) -> None:
    db.resource.join_daily_limit = 1
    first = await _create(3007, "群一")
    second = await _create(3008, "群二")
    await _enqueue(db, first.id, probe_account)
    task_id = await _enqueue(db, second.id, probe_account)

    async with session_scope() as session:
        task = await session.get(ResourceJoinTask, task_id)

    assert task.scheduled_at.date() > utc_now().date()
    assert task.scheduled_at.hour == 0


async def test_run_join_success_writes_quota_and_audit(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    fake_resource_client.add_chat(3010, "求职群", username="job_group")
    resource = await _create(3010, "求职群")
    task_id = await _enqueue(db, resource.id, probe_account)

    result = await _run(db, fake_resource_client, task_id, actor="admin")

    assert result["status"] == JOIN_SUCCESS
    assert fake_resource_client.joined == [3010]
    async with session_scope() as session:
        quota = await resource_quota_service.get_quota(session, probe_account)
        audits = list(await session.scalars(select(AuditLog)))
    assert quota is not None and quota.joins == 1
    assert audits[0].method == "JOIN"
    assert audits[0].username == "admin"


async def test_waiting_approval_is_not_a_failure(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    fake_resource_client.add_chat(3011, "需审批群", username="approval_group")
    fake_resource_client.join_errors[3011] = RuntimeError("INVITE_REQUEST_SENT")
    resource = await _create(3011, "需审批群")
    task_id = await _enqueue(db, resource.id, probe_account)

    result = await _run(db, fake_resource_client, task_id)

    assert result["status"] == JOIN_WAITING_APPROVAL
    assert "审批" in result["note"]
    async with session_scope() as session:
        task = await session.get(ResourceJoinTask, task_id)
    assert task.attempts == 1


async def test_already_member_counts_as_success(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    fake_resource_client.add_chat(3012, "已在群", username="in_group")
    fake_resource_client.join_errors[3012] = RuntimeError("USER_ALREADY_PARTICIPANT")
    resource = await _create(3012, "已在群")
    task_id = await _enqueue(db, resource.id, probe_account)

    result = await _run(db, fake_resource_client, task_id)

    assert result["status"] == JOIN_SUCCESS
    async with session_scope() as session:
        quota = await resource_quota_service.get_quota(session, probe_account)
    assert quota is not None and quota.joins == 1


async def test_invalid_invite_fails_permanently(db, fake_resource_client, probe_account) -> None:
    fake_resource_client.add_chat(3013, "失效邀请", username="dead_group")
    fake_resource_client.join_errors[3013] = RuntimeError("INVITE_HASH_EXPIRED")
    resource = await _create(3013, "失效邀请")
    task_id = await _enqueue(db, resource.id, probe_account)

    result = await _run(db, fake_resource_client, task_id)

    assert result["status"] == JOIN_FAILED
    assert "失效" in result["error"]


async def test_unknown_error_retries_then_fails(db, fake_resource_client, probe_account) -> None:
    db.resource.max_join_attempts = 2
    fake_resource_client.add_chat(3014, "怪群", username="weird_group")
    fake_resource_client.join_errors[3014] = RuntimeError("内部超时")
    resource = await _create(3014, "怪群")
    task_id = await _enqueue(db, resource.id, probe_account)

    statuses = []
    for _ in range(2):
        statuses.append((await _run(db, fake_resource_client, task_id))["status"])

    assert statuses == [JOIN_PENDING, JOIN_FAILED]
    async with session_scope() as session:
        task = await session.get(ResourceJoinTask, task_id)
    assert task.attempts == 2
    assert task.last_error


async def test_leave_retires_resource(db, fake_resource_client, probe_account) -> None:
    fake_resource_client.add_chat(3015, "低产出群", username="low_group")
    resource = await _create(3015, "低产出群")
    task_id = await _enqueue(db, resource.id, probe_account, action="leave")

    await _run(db, fake_resource_client, task_id)

    assert fake_resource_client.left == [3015]
    async with session_scope() as session:
        updated = await resource_service.require_resource(session, resource.id)
        quota = await resource_quota_service.get_quota(session, probe_account)
    assert updated.status == RESOURCE_RETIRED
    assert quota is not None and quota.leaves == 1


async def test_queue_stats_and_serialize(db, probe_account) -> None:
    resource = await _create(3017, "求职群", username="stat_group")
    await _enqueue(db, resource.id, probe_account)

    async with session_scope() as session:
        rows = list(await session.scalars(select(ResourceJoinTask)))
        counts = await resource_join_service.stats(session)
        payload = resource_join_service.serialize_task(rows[0], resource)

    assert len(rows) == 1
    assert counts["pending"] == 1
    assert payload["action"] == "join"
    assert payload["resource_name"] == "求职群"


async def test_next_due_task_returns_scheduled(db, probe_account) -> None:
    resource = await _create(3018, "群", username="due_group")
    task_id = await _enqueue(db, resource.id, probe_account)

    async with session_scope() as session:
        due = await resource_join_service.next_due_task(session)

    assert due is not None and due.id == task_id
