"""卡片墙相关接口：在线补搜、chips / 快捷榜计数、敏感内容默认过滤（F-R19/F-R22/F-R23）。"""

from __future__ import annotations

from tests.conftest import auth_header, login
from tests.test_directory_sites import TGME_HTML
from tests.test_directory_sync_service import _item, _payload


async def _token(client) -> dict[str, str]:
    response = await login(client)
    assert response.status_code == 200, response.text
    return auth_header(response.json()["token"])


def _seed(fake_directory_fetcher) -> None:
    fake_directory_fetcher.add_page(
        "offset=0",
        _payload(
            _item("搜群神器", "qqpp", -1001764441693, 172156, rank=1),
            _item("成都车友会", "rongcheng_travel", -1002150667587, 13797, rank=2),
            _item("某某娱乐城返水群", "casino_water", -1003333333333, 900, rank=3),
        ),
    )
    fake_directory_fetcher.add_page("/telegram-group/车友/1.html", TGME_HTML)


async def _store_combot(api_config, fake_directory_fetcher) -> None:
    from app.core.directory_sites import COMBOT
    from app.db.session import session_scope
    from app.services import directory_sync_service

    async with session_scope() as session:
        await directory_sync_service.sync_once(
            session,
            api_config,
            COMBOT,
            "zh",
            fetcher=fake_directory_fetcher,
            max_pages=1,
        )


async def test_counts_endpoint_gives_chips_and_quick(
    directory_api_client,
    api_config,
    fake_directory_fetcher,
) -> None:
    headers = await _token(directory_api_client)
    _seed(fake_directory_fetcher)
    await _store_combot(api_config, fake_directory_fetcher)

    response = await directory_api_client.get("/api/resources/counts", headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    languages = {item["value"]: item["count"] for item in payload["languages"]}
    assert languages == {"zh": 3}
    chat_types = {item["value"]: item["count"] for item in payload["chat_types"]}
    assert chat_types == {"supergroup": 3}
    sources = {item["value"]: item["count"] for item in payload["sources"]}
    assert sources == {"combot": 3}
    ratings = {item["value"]: item["count"] for item in payload["ratings"]}
    # 标题带"返水"的那条被粗判成敏感
    assert ratings == {"normal": 2, "sensitive": 1}
    assert payload["quick"]["new"] == 3
    assert payload["quick"]["sensitive"] == 1
    assert payload["quick"]["adopted"] == 0
    # 计数不出网
    assert len(fake_directory_fetcher.requests) == 1


async def test_sensitive_is_hidden_by_default(
    directory_api_client,
    api_config,
    fake_directory_fetcher,
) -> None:
    headers = await _token(directory_api_client)
    _seed(fake_directory_fetcher)
    await _store_combot(api_config, fake_directory_fetcher)

    default = await directory_api_client.get("/api/resources", headers=headers)
    shown = await directory_api_client.get(
        "/api/resources",
        headers=headers,
        params={"include_sensitive": True},
    )
    only_sensitive = await directory_api_client.get(
        "/api/resources",
        headers=headers,
        params={"content_rating": "sensitive"},
    )
    exported = await directory_api_client.get("/api/resources/export.csv", headers=headers)

    assert default.json()["total"] == 2
    assert shown.json()["total"] == 3
    # 显式筛敏感时以调用方意图为准
    assert only_sensitive.json()["total"] == 1
    assert "某某娱乐城返水群" in exported.text


async def test_discover_online_endpoint_aggregates_channels(
    directory_api_client,
    api_config,
    fake_directory_fetcher,
    fake_resource_client,
) -> None:
    headers = await _token(directory_api_client)
    _seed(fake_directory_fetcher)
    # 本地先有一批 combot 目录，供 combot 渠道匹配
    await _store_combot(api_config, fake_directory_fetcher)
    chat = fake_resource_client.add_chat(4100, "车友交流群", username="car_chat")
    fake_resource_client.add_search("车友", [chat])

    response = await directory_api_client.post(
        "/api/resources/discover-online",
        headers=headers,
        json={"keywords": ["车友"], "sites": ["telegram", "combot", "tgme"], "limit": 20},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    by_site = {item["site"]: item for item in payload["results"]}
    assert by_site["telegram"]["status"] == "ok"
    assert by_site["telegram"]["new_resources"] == 1
    # combot 只匹配本地目录，命中的是标题里带"车友"的那条
    assert by_site["combot"]["status"] == "local"
    assert by_site["combot"]["hits"] == 1
    assert by_site["combot"]["new_resources"] == 0
    assert by_site["tgme"]["status"] == "ok"
    assert by_site["tgme"]["new_resources"] == 2
    assert payload["new_total"] == 3

    # 补搜出来的资源都进了本地库，来源可溯
    listing = await directory_api_client.get(
        "/api/resources",
        headers=headers,
        params={"include_sensitive": True},
    )
    names = {item["name"] for item in listing.json()["items"]}
    assert "车友交流群" in names
    assert "Wealth & Crypto" in names
    assert "搜群神器" in names


async def test_discover_online_rejects_empty_keywords(directory_api_client) -> None:
    headers = await _token(directory_api_client)

    response = await directory_api_client.post(
        "/api/resources/discover-online",
        headers=headers,
        json={"keywords": [], "sites": ["combot"]},
    )

    assert response.status_code == 422


async def test_due_refresh_filter(
    directory_api_client,
    api_config,
    fake_directory_fetcher,
) -> None:
    headers = await _token(directory_api_client)
    _seed(fake_directory_fetcher)
    await _store_combot(api_config, fake_directory_fetcher)

    due = await directory_api_client.get(
        "/api/resources",
        headers=headers,
        params={"due_refresh": True},
    )

    # 刚同步进来的资源都没排过刷新时间，属于"该刷新"
    assert due.json()["total"] == 2
