"""发现器：关键词、句式、热门词与限流的处理（F-R02 / F-R04 / F-R05）。"""

from __future__ import annotations

from sqlalchemy import select

from app.db.base import utc_now
from app.db.models import (
    DISCOVER_HOTWORD,
    DISCOVER_PHRASE,
    HotKeyword,
    ResourceDiscoverTask,
    TgResource,
)
from app.db.session import session_scope
from app.services import (
    resource_discover_service,
    resource_quota_service,
    resource_service,
)
from tests.conftest import resource_message


async def test_keyword_task_discovers_candidates(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    first = fake_resource_client.add_chat(2001, "求职群一", username="job_one", member_count=3000)
    second = fake_resource_client.add_chat(2002, "求职群二", username="job_two", member_count=900)
    fake_resource_client.add_search("求职", [first, second])

    async with session_scope() as session:
        task = await resource_discover_service.create_task(
            session,
            kind="keyword",
            keyword="求职",
            category="行业",
        )
        outcome = await resource_discover_service.run_task(
            session,
            db,
            fake_resource_client,
            task,
            account_id=probe_account,
        )

    assert outcome.hits == 2
    assert outcome.new_resources == 2
    async with session_scope() as session:
        rows = list(await session.scalars(select(TgResource)))
        quota = await resource_quota_service.get_quota(session, probe_account)
        stored = await resource_discover_service.get_task(session, task.id)
    assert {row.username for row in rows} == {"job_one", "job_two"}
    assert all(row.discovered_by == "keyword" for row in rows)
    assert quota is not None and quota.searches == 1
    assert stored is not None and stored.hits == 2 and stored.new_found == 2


async def test_duplicate_discovery_does_not_create_second_row(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    chat = fake_resource_client.add_chat(2010, "求职群一", username="job_one")
    fake_resource_client.add_search("求职", [chat])

    async with session_scope() as session:
        task = await resource_discover_service.create_task(session, kind="keyword", keyword="求职")
        first = await resource_discover_service.run_task(
            session, db, fake_resource_client, task, account_id=probe_account
        )
        # 窗口期内再跑一次：直接跳过，不重复搜
        second = await resource_discover_service.run_task(
            session, db, fake_resource_client, task, account_id=probe_account
        )

    assert first.new_resources == 1
    assert second.skipped == "recent"
    async with session_scope() as session:
        assert len(list(await session.scalars(select(TgResource)))) == 1


async def test_flood_wait_is_queued_not_failed(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    fake_resource_client.search_errors["求职"] = RuntimeError(
        "A wait of 3600 seconds is required (caused by FloodWaitError)"
    )

    async with session_scope() as session:
        task = await resource_discover_service.create_task(session, kind="keyword", keyword="求职")
        before = utc_now()
        outcome = await resource_discover_service.run_task(
            session, db, fake_resource_client, task, account_id=probe_account
        )
        stored = await resource_discover_service.get_task(session, task.id)
        quota = await resource_quota_service.get_quota(session, probe_account)

    assert outcome.skipped == "flood_wait"
    assert outcome.wait_seconds == 3600
    assert stored is not None
    assert stored.flood_waits == 1
    assert stored.next_run_at is not None
    assert (stored.next_run_at - before).total_seconds() >= 3500
    assert quota is not None and quota.flood_waits == 1


async def test_daily_search_quota_queues_to_tomorrow(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    db.resource.search_daily_limit = 1
    chat = fake_resource_client.add_chat(2020, "群一", username="group_one")
    fake_resource_client.add_search("第一词", [chat])

    async with session_scope() as session:
        first = await resource_discover_service.create_task(
            session, kind="keyword", keyword="第一词"
        )
        await resource_discover_service.run_task(
            session, db, fake_resource_client, first, account_id=probe_account
        )
        second = await resource_discover_service.create_task(
            session, kind="keyword", keyword="第二词"
        )
        outcome = await resource_discover_service.run_task(
            session, db, fake_resource_client, second, account_id=probe_account
        )
        stored = await resource_discover_service.get_task(session, second.id)

    assert outcome.skipped == "quota"
    assert stored is not None and stored.next_run_at is not None
    assert stored.next_run_at.date() > utc_now().date()
    assert fake_resource_client.searches == ["第一词"]


async def test_import_hotwords_creates_tasks(db) -> None:
    async with session_scope() as session:
        session.add(HotKeyword(token="篮球", count=9))
        session.add(HotKeyword(token="足球", count=5))
        session.add(HotKeyword(token="冷门词", count=1))
        await session.commit()

    async with session_scope() as session:
        result = await resource_discover_service.import_hotwords(session, top_n=5, min_count=2)
        tasks = await resource_discover_service.list_tasks(session, kind=DISCOVER_HOTWORD)

    assert result["added"] == ["篮球", "足球"]
    assert [item.keyword for item in tasks] == ["篮球", "足球"]
    assert all(item.category == "热门词" for item in tasks)

    # 再导一次不重复建任务
    async with session_scope() as session:
        again = await resource_discover_service.import_hotwords(session, top_n=5, min_count=2)
    assert again["added"] == []
    assert set(again["skipped"]) == {"篮球", "足球"}


async def test_phrase_presets_and_run(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    fake_resource_client.add_global(
        "求群",
        [
            resource_message(1, "谁有资源群？t.me/phrase_group"),
            resource_message(2, "拉我进群 @phrase_second"),
        ],
    )

    async with session_scope() as session:
        created = await resource_discover_service.ensure_phrase_presets(session)
        tasks = await resource_discover_service.list_tasks(session, kind=DISCOVER_PHRASE)
        task = next(item for item in tasks if item.keyword == "求群")
        outcome = await resource_discover_service.run_task(
            session, db, fake_resource_client, task, account_id=probe_account
        )

    assert created == len(resource_discover_service.PHRASE_PRESETS)
    assert outcome.hits == 2
    assert outcome.new_resources == 2
    async with session_scope() as session:
        rows = list(await session.scalars(select(TgResource)))
    assert {row.username for row in rows} == {"phrase_group", "phrase_second"}
    assert all(row.discovered_by == "phrase" for row in rows)


async def test_next_due_task_skips_disabled(db) -> None:
    async with session_scope() as session:
        enabled = await resource_discover_service.create_task(
            session, kind="keyword", keyword="在跑的"
        )
        disabled = await resource_discover_service.create_task(
            session, kind="keyword", keyword="停用的", enabled=False
        )

    async with session_scope() as session:
        found = await resource_discover_service.next_due_task(session)

    assert found is not None
    assert found.id == enabled.id
    assert found.id != disabled.id


async def test_discover_once_with_explicit_keywords(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    chat = fake_resource_client.add_chat(2030, "临时群", username="temp_group")
    fake_resource_client.add_search("临时词", [chat])

    async with session_scope() as session:
        result = await resource_discover_service.discover_once(
            session,
            db,
            fake_resource_client,
            keywords=["临时词", "有错的词"],
            account_id=probe_account,
        )
        rows = list(await session.scalars(select(TgResource)))
        quota = await resource_quota_service.get_quota(session, probe_account)

    first, second = result["results"]
    assert first["new_resources"] == 1
    assert second["new_resources"] == 0
    assert [row.username for row in rows] == ["temp_group"]
    # 关键词本身不该被写进任务表
    async with session_scope() as session:
        assert list(await session.scalars(select(ResourceDiscoverTask))) == []
    assert quota is not None and quota.searches == 2


async def test_task_crud(db) -> None:
    async with session_scope() as session:
        task = await resource_discover_service.create_task(
            session, kind="keyword", keyword="词", category="行业"
        )
    async with session_scope() as session:
        updated = await resource_discover_service.update_task(
            session, task.id, keyword="新词", enabled=False
        )
        listed = await resource_discover_service.list_tasks(session, enabled=False)
        await resource_discover_service.delete_task(session, task.id)
        remaining = await resource_discover_service.list_tasks(session)

    assert updated.keyword == "新词"
    assert updated.enabled is False
    assert [item.id for item in listed] == [task.id]
    assert remaining == []


async def test_serialize_task_reports_due(db) -> None:
    async with session_scope() as session:
        task = await resource_discover_service.create_task(session, kind="keyword", keyword="词")
        payload = resource_discover_service.serialize_task(task)
        await resource_service.stats(session)  # 顺带覆盖资源统计

    assert payload["due"] is True
    assert payload["keyword"] == "词"
