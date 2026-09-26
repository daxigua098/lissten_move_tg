"""资源探测服务：指标落库、链接滚雪球、失败分类与配额（F-R03 / F-R06）。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.core.errors import ConflictError
from app.db.models import PROBE_FAILED, PROBE_OK, ResourceProbeLog, TgResource
from app.db.session import session_scope
from app.services import resource_probe_service, resource_quota_service, resource_service
from app.services.resource_probe_service import _state_from_error
from app.services.resource_service import ResourceRef
from tests.conftest import resource_message


async def _create(tg_id: int | None, title: str, **kwargs) -> TgResource:
    async with session_scope() as session:
        outcome = await resource_service.upsert_resource(
            session,
            ResourceRef(tg_id=tg_id, title=title, **kwargs),
        )
        assert outcome.resource is not None
        return outcome.resource


async def _probe(db, client, resource_id: int, **kwargs):
    async with session_scope() as session:
        fresh = await resource_service.require_resource(session, resource_id)
        return await resource_probe_service.probe_resource(session, db, client, fresh, **kwargs)


async def test_probe_writes_metrics_and_log(db, fake_resource_client) -> None:
    fake_resource_client.add_chat(
        1001,
        "求职大群",
        username="job_big",
        about="每天发布招聘信息",
        member_count=8000,
    )
    fake_resource_client.add_history(
        1001,
        [resource_message(i, "招聘日结，当天结算", sender_id=100 + i) for i in range(6)],
    )
    resource = await _create(1001, "求职大群")

    outcome = await _probe(db, fake_resource_client, resource.id)

    assert outcome.result == PROBE_OK
    assert outcome.requests_used == 2
    assert outcome.resource.member_count == 8000
    assert outcome.resource.member_count_approx is True
    assert outcome.resource.about == "每天发布招聘信息"
    assert outcome.resource.activity_score > 0
    assert outcome.resource.language == "zh"
    assert "求职招聘" in resource_service.load_categories(outcome.resource)
    assert outcome.resource.resource_state == "active"
    assert outcome.resource.next_refresh_at is not None

    async with session_scope() as session:
        logs = list(await session.scalars(select(ResourceProbeLog)))
    assert len(logs) == 1
    assert logs[0].result == "ok"


async def test_probe_counts_quota(db, fake_resource_client, probe_account) -> None:
    fake_resource_client.add_chat(1002, "闲聊群", member_count=50)
    fake_resource_client.add_history(1002, [resource_message(1, "晚上好")])
    resource = await _create(1002, "闲聊群")

    await _probe(db, fake_resource_client, resource.id, account_id=probe_account)

    async with session_scope() as session:
        quota = await resource_quota_service.get_quota(session, probe_account)
    assert quota is not None
    assert quota.probes == 1


async def test_probe_resolves_username_only_candidate(db, fake_resource_client) -> None:
    """只有 @username 的候选，探测时解析出 tg_id 并回填（不产生第二条记录）。"""
    fake_resource_client.add_chat(1003, "搜索导航", username="nav_group", member_count=9000)
    fake_resource_client.add_history(1003, [resource_message(1, "大家好，欢迎加入")])
    resource = await _create(None, "nav_group", username="nav_group")
    assert resource.tg_id is None

    outcome = await _probe(db, fake_resource_client, resource.id)

    assert outcome.result == PROBE_OK
    assert outcome.resource.tg_id == 1003
    async with session_scope() as session:
        assert len(list(await session.scalars(select(TgResource)))) == 1


async def test_probe_snowballs_links_from_about_and_messages(
    db,
    fake_resource_client,
) -> None:
    fake_resource_client.add_chat(
        1004,
        "资源群",
        about="更多资源看 t.me/other_group",
        member_count=1200,
    )
    fake_resource_client.add_history(
        1004,
        [
            resource_message(1, "好资源 @third_group"),
            resource_message(2, "看 t.me/+AbCdEf123456"),
        ],
    )
    resource = await _create(1004, "资源群")

    outcome = await _probe(db, fake_resource_client, resource.id)

    assert outcome.result == PROBE_OK
    assert outcome.new_resources == 3
    assert outcome.links_found == 3
    async with session_scope() as session:
        rows = list(await session.scalars(select(TgResource)))
    by_username = {row.username for row in rows if row.username}
    assert {"other_group", "third_group"} <= by_username
    assert any(row.invite_link and row.invite_link.endswith("AbCdEf123456") for row in rows)
    assert all(row.discovered_by == "link" for row in rows if row.tg_id != 1004)


async def test_index_group_links_get_priority(db, fake_resource_client) -> None:
    """来自索引型群的链接排队更靠前（F-R03 的优先级）。"""
    fake_resource_client.add_chat(1005, "超级搜索", about="t.me/from_index", member_count=9000)
    fake_resource_client.add_history(
        1005,
        [resource_message(i, f"t.me/idx{i}_group", sender_id=900, is_bot=True) for i in range(10)],
    )
    index_resource = await _create(1005, "超级搜索")

    fake_resource_client.add_chat(1006, "普通群", about="t.me/from_normal", member_count=200)
    fake_resource_client.add_history(1006, [resource_message(1, "大家好")])
    normal_resource = await _create(1006, "普通群")

    await _probe(db, fake_resource_client, index_resource.id)
    await _probe(db, fake_resource_client, normal_resource.id)

    async with session_scope() as session:
        priority = await resource_service.find_by_key(session, username="from_index")
        plain = await resource_service.find_by_key(session, username="from_normal")

    assert priority is not None and plain is not None
    assert priority.next_refresh_at < plain.next_refresh_at


async def test_probe_failure_keeps_state_and_logs(db, fake_resource_client) -> None:
    # 没有登记实体 = 这个账号看不到该群，探测必须失败并留下可查的原因
    resource = await _create(1007, "读不到的群")

    outcome = await _probe(db, fake_resource_client, resource.id)

    assert outcome.result == PROBE_FAILED
    assert outcome.resource.probe_error
    assert outcome.resource.next_refresh_at is not None
    async with session_scope() as session:
        logs = list(await session.scalars(select(ResourceProbeLog)))
    assert logs[0].result == "failed"
    assert logs[0].error


def test_parse_state_from_error_text() -> None:
    assert _state_from_error(RuntimeError("The channel is banned")) == "banned"
    assert _state_from_error(RuntimeError("You are not a participant")) == "left"
    assert _state_from_error(RuntimeError("This channel is private")) == "private"
    assert _state_from_error(RuntimeError("超时")) is None


async def test_probe_skips_blacklisted(db, fake_resource_client) -> None:
    fake_resource_client.add_chat(1008, "拉黑群", member_count=100)
    resource = await _create(1008, "拉黑群")
    async with session_scope() as session:
        fresh = await resource_service.require_resource(session, resource.id)
        await resource_service.set_blacklisted(session, fresh, True, reason="噪声")

    try:
        await _probe(db, fake_resource_client, resource.id)
    except ConflictError as exc:
        assert "黑名单" in exc.detail
    else:  # pragma: no cover - 走到这里说明没拦住
        raise AssertionError("黑名单资源不应被探测")


async def test_batch_probe_reports_failures(db, fake_resource_client) -> None:
    fake_resource_client.add_chat(1009, "正常群", member_count=300)
    fake_resource_client.add_history(1009, [resource_message(1, "你好")])
    good = await _create(1009, "正常群")
    # 1010 没有任何登记，采样与资料都拿不到
    bad = await _create(1010, "读不到的群")

    async with session_scope() as session:
        rows = [
            await resource_service.require_resource(session, good.id),
            await resource_service.require_resource(session, bad.id),
        ]
        result = await resource_probe_service.probe_many(
            session,
            db,
            fake_resource_client,
            rows,
        )

    assert [item["id"] for item in result["succeeded"]] == [good.id]
    assert [item["id"] for item in result["failed"]] == [bad.id]
    assert result["failed"][0]["error"]


async def test_next_probe_candidates_orders_never_probed_first(
    db,
    fake_resource_client,
) -> None:
    fake_resource_client.add_chat(1011, "探过的群", member_count=300)
    fake_resource_client.add_history(1011, [resource_message(1, "你好")])
    probed = await _create(1011, "探过的群")
    await _probe(db, fake_resource_client, probed.id)
    fresh = await _create(1012, "新候选")

    async with session_scope() as session:
        rows = await resource_service.next_probe_candidates(session, limit=5)

    ids = [row.id for row in rows]
    assert fresh.id in ids
    # 刚探测过的资源已经排到 7 天后，不该出现在队首
    assert ids[0] == fresh.id


async def test_probe_uses_configured_sample_depth(db, fake_resource_client) -> None:
    fake_resource_client.add_chat(1013, "大群", member_count=300)
    fake_resource_client.add_history(
        1013,
        [
            resource_message(i, "聊天", sender_id=200 + i, date=datetime(2026, 9, 1, tzinfo=UTC))
            for i in range(50)
        ],
    )
    resource = await _create(1013, "大群")

    outcome = await _probe(db, fake_resource_client, resource.id, sample_depth=20)

    assert outcome.result == PROBE_OK
    assert outcome.resource.unique_senders == 20
    async with session_scope() as session:
        log = await session.scalar(select(ResourceProbeLog))
    assert log is not None
