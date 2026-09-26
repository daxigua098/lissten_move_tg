"""在线补搜（F-R19）：按渠道聚合、失败隔离与配额。"""

from __future__ import annotations

from sqlalchemy import select

from app.core.directory_sites import COMBOT
from app.db.models import TgResource
from app.db.session import session_scope
from app.services import (
    directory_sync_service,
    resource_discover_service,
    resource_quota_service,
)
from tests.conftest import FakeDirectoryFetcher
from tests.test_directory_sites import TGME_HTML
from tests.test_directory_sync_service import _combot_fetcher


async def test_telegram_channel_stores_and_counts_quota(
    db,
    fake_resource_client,
    probe_account,
) -> None:
    db.resource.directory_request_interval = 0
    first = fake_resource_client.add_chat(3001, "求职群一", username="job_one")
    second = fake_resource_client.add_chat(3002, "求职群二", username="job_two")
    fake_resource_client.add_search("求职", [first, second])

    async with session_scope() as session:
        result = await resource_discover_service.search_online(
            session,
            db,
            fake_resource_client,
            keywords=["求职"],
            sites=["telegram"],
            account_id=probe_account,
        )
        quota = await resource_quota_service.get_quota(session, probe_account)
        rows = list(await session.scalars(select(TgResource)))

    assert result["results"] == [
        {
            "site": "telegram",
            "status": "ok",
            "hits": 2,
            "new_resources": 2,
            "error": None,
        }
    ]
    assert result["new_total"] == 2
    assert {row.username for row in rows} == {"job_one", "job_two"}
    assert all(row.discovered_from == "临时搜索：求职" for row in rows)
    assert quota is not None and quota.searches == 1


async def test_combot_channel_only_matches_local_catalog(db) -> None:
    db.resource.directory_request_interval = 0
    # 先把 combot 中文榜同步进本地库
    async with session_scope() as session:
        await directory_sync_service.sync_once(
            session,
            db,
            COMBOT,
            "zh",
            fetcher=_combot_fetcher(),
        )

    async with session_scope() as session:
        result = await resource_discover_service.search_online(
            session,
            db,
            None,
            keywords=["成都"],
            sites=["combot"],
        )

    # combot 没有关键词接口，只按本地目录匹配，且不出网
    assert result["results"][0]["site"] == "combot"
    assert result["results"][0]["status"] == "local"
    assert result["results"][0]["hits"] == 1
    assert result["results"][0]["new_resources"] == 0


async def test_failure_isolation_between_channels(db) -> None:
    db.resource.directory_request_interval = 0
    fetcher = FakeDirectoryFetcher()
    fetcher.add_page("/telegram-group/车友/1.html", TGME_HTML)
    fetcher.add_page("tgoop.com/telegram-group/车友/1.html", TGME_HTML)

    async with session_scope() as session:
        # client=None 模拟"没有可用执行账号"：Telegram 那一路失败，tg-me 照常出结果
        result = await resource_discover_service.search_online(
            session,
            db,
            None,
            keywords=["车友"],
            sites=["telegram", "tgme"],
            directory_fetcher=fetcher,
        )
        rows = list(await session.scalars(select(TgResource)))

    by_site = {item["site"]: item for item in result["results"]}
    assert by_site["telegram"]["status"] == "error"
    assert by_site["telegram"]["error"]
    assert by_site["tgme"]["status"] == "ok"
    assert by_site["tgme"]["new_resources"] == 2
    assert result["new_total"] == 2
    assert len(rows) == 2


async def test_tgme_channel_reports_fetch_error(db) -> None:
    db.resource.directory_request_interval = 0
    fetcher = FakeDirectoryFetcher()
    # 主站与镜像都被拦（真实场景里 tg-me 与 tgoop 同源）
    fetcher.add_error("www.tg-me.com", "HTTP 403（站点要求人机校验，按合规约定不绕过）")
    fetcher.add_error("tgoop.com", "HTTP 403（站点要求人机校验，按合规约定不绕过）")

    async with session_scope() as session:
        result = await resource_discover_service.search_online(
            session,
            db,
            None,
            keywords=["车友"],
            sites=["tgme"],
            directory_fetcher=fetcher,
        )

    assert result["results"][0]["status"] == "error"
    assert "人机校验" in result["results"][0]["error"]
    assert result["new_total"] == 0
