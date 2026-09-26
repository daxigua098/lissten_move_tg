"""目录同步服务：分页、去重、断点续抓与额度（F-R20 / F-R21 / F-R24）。"""

from __future__ import annotations

import json

from sqlalchemy import select

from app.core.directory_sites import COMBOT, TGME
from app.db.models import (
    DISCOVER_DIRECTORY,
    RATING_NORMAL,
    SYNC_FAILED,
    SYNC_OK,
    SYNC_PARTIAL,
    ResourceDirectoryRun,
    ResourceDiscoverTask,
    TgResource,
)
from app.db.session import session_scope
from app.services import directory_sync_service, resource_discover_service, resource_service
from app.services.resource_service import ResourceRef
from tests.conftest import FakeDirectoryFetcher
from tests.test_directory_sites import TGME_HTML


def _item(title, username, tg_id, members, *, lang="ZH", rank=1) -> dict:
    """一条 combot 形态的目录条目（``i`` 是头像，必须被丢弃）。"""
    return {
        "t": title,
        "u": username,
        "s": members,
        "pc": "none",
        "l": lang,
        "a": "",
        "i": "LONG_BASE64_AVATAR",
        "p": rank,
        "c": tg_id,
        "b": 0,
    }


def _payload(*items: dict) -> str:
    return json.dumps(list(items), ensure_ascii=False)


def _combot_fetcher(*, second_page: str | None = None) -> FakeDirectoryFetcher:
    """第一页两条 + 第二页（默认空 → 视为抓到尾）。"""
    fetcher = FakeDirectoryFetcher()
    fetcher.add_page(
        "offset=0",
        _payload(
            _item("搜群神器", "qqpp", -1001764441693, 172156, rank=1),
            _item("成都车友会", "rongcheng_travel", -1002150667587, 13797, lang="N/A", rank=2),
        ),
    )
    if second_page is not None:
        fetcher.add_page("offset=100", second_page)
    return fetcher


async def test_combot_sync_stores_candidates_with_ids(db) -> None:
    db.resource.directory_request_interval = 0
    fetcher = _combot_fetcher()

    async with session_scope() as session:
        outcome = await directory_sync_service.sync_once(session, db, COMBOT, "zh", fetcher=fetcher)
        rows = list(await session.scalars(select(TgResource).order_by(TgResource.id)))

    assert outcome["result"] == SYNC_OK
    run = outcome["run"]
    # 第 1 页有内容、第 2 页空 → 一共发了 2 个请求，落库 2 条
    assert run["pages_done"] == 1
    assert run["requests_used"] == 2
    assert run["items_seen"] == 2
    assert run["items_added"] == 2
    assert run["pages_total"] == 24  # 实测中文群 2320 个 / 每页 100

    first, second = rows
    assert first.tg_id == -1001764441693
    assert first.username == "qqpp"
    assert first.source_site == "combot"
    assert first.discovered_by == DISCOVER_DIRECTORY
    assert first.discovered_from == "Combot 目录：zh 第 1 页"
    assert first.status == "candidate"
    # 入库时按标题粗判内容分级（F-R22）：这两个标题都不敏感
    assert first.content_rating == RATING_NORMAL
    # 三方成员数单独存，且不冒充探测出来的成员数
    assert first.directory_member_count == 172156
    assert first.member_count is None
    assert first.directory_rank == 1
    assert first.language == "zh"
    # N/A 的语言不落库
    assert second.language is None
    assert first.source_url == "https://t.me/qqpp"
    assert first.avatar_path is None


async def test_second_sync_does_not_duplicate(db) -> None:
    db.resource.directory_request_interval = 0
    fetcher = _combot_fetcher()

    async with session_scope() as session:
        first = await directory_sync_service.sync_once(session, db, COMBOT, "zh", fetcher=fetcher)
        second = await directory_sync_service.sync_once(session, db, COMBOT, "zh", fetcher=fetcher)
        rows = list(await session.scalars(select(TgResource)))

    assert first["run"]["items_added"] == 2
    assert second["run"]["items_added"] == 0
    # 跑完的一轮从头再来（不续点），但按 tg_id 归一，不会多出记录
    assert second["resumed_from"] is None
    assert second["run"]["items_seen"] == 2
    assert len(rows) == 2


async def test_partial_run_resumes_from_next_page(db) -> None:
    db.resource.directory_request_interval = 0
    fetcher = _combot_fetcher(
        second_page=_payload(_item("第三群", "third_group", -1003333333333, 5000, rank=101)),
    )

    async with session_scope() as session:
        # 只允许抓 1 页 → partial，续点位停在 1
        first = await directory_sync_service.sync_once(
            session, db, COMBOT, "zh", fetcher=fetcher, max_pages=1
        )
    assert first["result"] == SYNC_PARTIAL
    assert first["run"]["pages_done"] == 1
    assert first["run"]["requests_used"] == 1
    assert first["run"]["resumable"] is True

    async with session_scope() as session:
        second = await directory_sync_service.sync_once(session, db, COMBOT, "zh", fetcher=fetcher)
        rows = list(await session.scalars(select(TgResource)))

    # 第二轮从第 2 页继续，第 1 页不再重复请求
    assert second["resumed_from"] == 2
    assert second["result"] == SYNC_OK
    assert second["run"]["pages_done"] == 2
    assert sum(1 for url in fetcher.requests if "offset=0&" in url) == 1
    assert any("offset=100&" in url for url in fetcher.requests)
    assert len(rows) == 3


async def test_fetch_error_keeps_resume_point(db) -> None:
    db.resource.directory_request_interval = 0
    fetcher = _combot_fetcher()
    fetcher.add_error("offset=100", "HTTP 500：boom")

    async with session_scope() as session:
        outcome = await directory_sync_service.sync_once(session, db, COMBOT, "zh", fetcher=fetcher)

    assert outcome["result"] == SYNC_PARTIAL
    assert outcome["run"]["pages_done"] == 1
    assert "HTTP 500" in outcome["run"]["error"]
    assert outcome["run"]["resumable"] is True

    # 修好之后从断点继续，已经抓过的第 1 页不再重复
    fetcher.errors.clear()
    fetcher.add_page("offset=100", _payload(_item("第二页群", "page_two", -1002222222222, 8000)))
    async with session_scope() as session:
        again = await directory_sync_service.sync_once(session, db, COMBOT, "zh", fetcher=fetcher)
        rows = list(await session.scalars(select(TgResource)))

    assert again["resumed_from"] == 2
    assert again["result"] == SYNC_OK
    assert len(rows) == 3


async def test_failure_on_first_page_is_not_resumable(db) -> None:
    db.resource.directory_request_interval = 0
    fetcher = FakeDirectoryFetcher()
    fetcher.add_error("combot.org", "HTTP 403（站点要求人机校验，按合规约定不绕过）")

    async with session_scope() as session:
        outcome = await directory_sync_service.sync_once(session, db, COMBOT, "zh", fetcher=fetcher)

    assert outcome["result"] == SYNC_FAILED
    assert outcome["run"]["pages_done"] == 0
    assert outcome["run"]["resumable"] is False
    assert "人机校验" in outcome["run"]["error"]


async def test_daily_request_limit_stops_then_skips(db) -> None:
    db.resource.directory_request_interval = 0
    db.resource.directory_daily_requests = 1
    fetcher = _combot_fetcher(
        second_page=_payload(_item("第二页群", "page_two", -1002222222222, 8000)),
    )

    async with session_scope() as session:
        first = await directory_sync_service.sync_once(session, db, COMBOT, "zh", fetcher=fetcher)
        used = await directory_sync_service.daily_requests_used(session)
        second = await directory_sync_service.sync_once(session, db, COMBOT, "zh", fetcher=fetcher)

    assert first["result"] == SYNC_PARTIAL
    assert first["run"]["requests_used"] == 1
    assert "额度" in first["run"]["error"]
    assert used == 1
    # 超限之后不再出网，直接跳过
    assert second["result"] == "skipped"
    assert second["reason"] == "quota"
    assert len(fetcher.requests) == 1


async def test_disabled_directory_skips_without_run(db) -> None:
    db.resource.directory_enabled = False
    fetcher = _combot_fetcher()

    async with session_scope() as session:
        outcome = await directory_sync_service.sync_once(session, db, COMBOT, "zh", fetcher=fetcher)
        runs = list(await session.scalars(select(ResourceDirectoryRun)))

    assert outcome == {"result": "skipped", "reason": "disabled"}
    assert runs == []
    assert fetcher.requests == []


async def test_tgme_sync_falls_back_to_mirror(db) -> None:
    db.resource.directory_request_interval = 0
    fetcher = FakeDirectoryFetcher()
    fetcher.add_error("https://www.tg-me.com/telegram-group/车友/1.html")
    fetcher.add_page("https://tgoop.com/telegram-group/车友/1.html", TGME_HTML)

    async with session_scope() as session:
        outcome = await directory_sync_service.sync_once(session, db, TGME, "车友", fetcher=fetcher)
        rows = list(await session.scalars(select(TgResource).order_by(TgResource.id)))

    assert outcome["result"] == SYNC_OK
    assert outcome["run"]["items_added"] == 2
    # 主站失败 → 镜像顶上，仍然只算一个"页"
    assert len(fetcher.requests) == 3
    first, second = rows
    assert first.source_site == "tgme"
    assert first.username == "Wealth"
    assert first.invite_link is None
    assert first.tg_id is None
    assert first.member_count is None
    assert first.directory_member_count == 5205136
    assert second.chat_type == "channel"
    assert second.invite_link == "https://t.me/+qu9ID0yVWhtiYmQ0"


async def test_directory_metadata_does_not_clobber_probed_values(db) -> None:
    db.resource.directory_request_interval = 0
    # 先按"已经探测过"的样子登记一条（成员数与语言都是自算值）
    async with session_scope() as session:
        await resource_service.upsert_resource(
            session,
            ResourceRef(
                title="成都车友会",
                tg_id=-1002150667587,
                username="rongcheng_travel",
                member_count=8000,
                language="zh",
            ),
            discovered_by="keyword",
        )

    fetcher = _combot_fetcher()
    async with session_scope() as session:
        await directory_sync_service.sync_once(session, db, COMBOT, "zh", fetcher=fetcher)
        row = await session.scalar(select(TgResource).where(TgResource.tg_id == -1002150667587))

    assert row is not None
    # 三方成员数进单独字段，自算的成员数不被覆盖
    assert row.member_count == 8000
    assert row.directory_member_count == 13797
    # 已经探测出语言就不被三方语言覆盖；首次记录来源站
    assert row.language == "zh"
    assert row.source_site == "combot"
    assert row.directory_rank == 2
    assert row.directory_synced_at is not None


async def test_blacklisted_candidate_is_not_stored(db) -> None:
    db.resource.directory_request_interval = 0
    async with session_scope() as session:
        seeded = await resource_service.upsert_resource(
            session,
            ResourceRef(title="搜群神器", tg_id=-1001764441693, username="qqpp"),
        )
        await resource_service.set_blacklisted(session, seeded.resource, True, reason="垃圾群")

    fetcher = _combot_fetcher()
    async with session_scope() as session:
        outcome = await directory_sync_service.sync_once(session, db, COMBOT, "zh", fetcher=fetcher)
        rows = list(await session.scalars(select(TgResource)))

    # 黑名单资源不会被目录同步再次收录（也不会重复建一条）
    assert outcome["run"]["items_added"] == 1
    assert len(rows) == 2


async def test_directory_task_counters_and_telegram_runner_ignores_it(db) -> None:
    db.resource.directory_request_interval = 0
    fetcher = _combot_fetcher()

    async with session_scope() as session:
        task = await directory_sync_service.create_task(session, source=COMBOT, scope="zh")
        again = await directory_sync_service.create_task(session, source=COMBOT, scope="zh")
        task_id = task.id

    assert task.kind == DISCOVER_DIRECTORY
    assert task.source == COMBOT
    assert task.category == "zh"
    assert again.id == task.id  # 同站同范围幂等

    async with session_scope() as session:
        # 换一个 session 就要重新取实体，别跨 session 用旧实例
        stored = await session.get(ResourceDiscoverTask, task_id)
        outcome = await directory_sync_service.run_task(session, db, stored, fetcher=fetcher)
        stored = await session.get(ResourceDiscoverTask, task_id)
        # Telegram 的发现循环不会把目录任务当成关键词去搜群
        assert await resource_discover_service.next_due_task(session) is None
        # 就算被错误地喂给 Telegram 发现器，也明确跳过而不是搜"zh"
        skipped = await resource_discover_service.run_task(
            session, db, fetcher, stored, account_id=None
        )

    assert outcome["scope"] == "zh"
    assert outcome["run"]["items_added"] == 2
    assert stored is not None
    assert stored.hits == 2
    assert stored.new_found == 2
    assert stored.last_run_at is not None
    assert stored.next_run_at > stored.last_run_at
    assert stored.last_error is None
    assert skipped.skipped == "directory"
    assert fetcher.requests and "combot.org" in fetcher.requests[0]
