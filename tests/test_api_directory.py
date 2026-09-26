"""目录同步接口：状态、同步一次、历史与列表筛选（P-R05）。"""

from __future__ import annotations

import json

from tests.conftest import auth_header, login
from tests.test_directory_sync_service import _item, _payload


async def _token(client) -> dict[str, str]:
    response = await login(client)
    assert response.status_code == 200, response.text
    return auth_header(response.json()["token"])


def _seed_combot(fake_directory_fetcher) -> None:
    fake_directory_fetcher.add_page(
        "offset=0",
        _payload(
            _item("搜群神器", "qqpp", -1001764441693, 172156, rank=1),
            _item("成都车友会", "rongcheng_travel", -1002150667587, 13797, rank=2),
        ),
    )


async def test_directory_sources_reads_local_db_only(
    directory_api_client,
    fake_directory_fetcher,
) -> None:
    headers = await _token(directory_api_client)

    response = await directory_api_client.get("/api/resources/directory/sources", headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["daily_requests_used"] == 0
    assert payload["daily_requests_limit"] == 700
    sources = {item["source"]: item for item in payload["sites"]}
    assert set(sources) == {"combot", "tgme"}
    assert sources["combot"]["enabled"] is True
    assert sources["combot"]["scopes"][0]["scope"] == "zh"
    assert sources["combot"]["scopes"][0]["pages_total"] == 24
    assert sources["combot"]["scopes"][0]["last_run"] is None
    # 看状态不出网
    assert fake_directory_fetcher.requests == []


async def test_directory_sync_endpoint_then_list_and_runs(
    directory_api_client,
    fake_directory_fetcher,
) -> None:
    headers = await _token(directory_api_client)
    _seed_combot(fake_directory_fetcher)

    response = await directory_api_client.post(
        "/api/resources/directory/sync",
        headers=headers,
        json={"source": "combot", "scope": "zh", "max_pages": 1},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["result"] == "partial"
    assert payload["run"]["items_added"] == 2
    assert payload["run"]["requests_used"] == 1
    assert payload["run"]["pages_done"] == 1

    runs = await directory_api_client.get("/api/resources/directory/runs", headers=headers)
    assert runs.status_code == 200
    items = runs.json()["items"]
    assert len(items) == 1
    assert items[0]["source"] == "combot"
    assert items[0]["scope"] == "zh"
    assert items[0]["result"] == "partial"

    overview = await directory_api_client.get("/api/resources/directory/sources", headers=headers)
    assert overview.json()["daily_requests_used"] == 1
    assert overview.json()["sites"][0]["scopes"][0]["last_run"]["items_added"] == 2

    # 同步出来的候选进了本地库，列表按来源站点与内容分级都能筛
    listing = await directory_api_client.get(
        "/api/resources",
        headers=headers,
        params={"source_site": "combot"},
    )
    assert listing.json()["total"] == 2
    by_rating = await directory_api_client.get(
        "/api/resources",
        headers=headers,
        params={"content_rating": "normal"},
    )
    assert by_rating.json()["total"] == 2
    first = listing.json()["items"][0]
    assert first["source_site"] == "combot"
    assert first["directory_member_count"] == 172156
    assert first["member_count"] is None
    assert first["discovered_by"] == "directory"


async def test_directory_sync_rejects_unknown_site(
    directory_api_client,
    fake_directory_fetcher,
) -> None:
    headers = await _token(directory_api_client)

    response = await directory_api_client.post(
        "/api/resources/directory/sync",
        headers=headers,
        json={"source": "telegram", "scope": "zh"},
    )

    assert response.status_code == 422
    assert fake_directory_fetcher.requests == []


async def test_directory_task_endpoint_is_idempotent(
    directory_api_client,
) -> None:
    headers = await _token(directory_api_client)

    first = await directory_api_client.post(
        "/api/resources/directory/tasks",
        headers=headers,
        json={"source": "combot", "scope": "zh"},
    )
    assert first.status_code == 201, first.text
    payload = first.json()
    assert payload["source"] == "combot"
    assert payload["kind"] == "directory"
    assert payload["category"] == "zh"
    assert payload["due"] is True

    # 同站同范围幂等：不会攒出第二条定时任务
    again = await directory_api_client.post(
        "/api/resources/directory/tasks",
        headers=headers,
        json={"source": "combot", "scope": "zh"},
    )
    assert again.status_code == 201
    assert again.json()["id"] == payload["id"]

    bad = await directory_api_client.post(
        "/api/resources/directory/tasks",
        headers=headers,
        json={"source": "telegram", "scope": "zh"},
    )
    assert bad.status_code == 422


async def test_manual_content_rating_is_locked(
    directory_api_client,
    fake_directory_fetcher,
) -> None:
    headers = await _token(directory_api_client)
    _seed_combot(fake_directory_fetcher)
    await directory_api_client.post(
        "/api/resources/directory/sync",
        headers=headers,
        json={"source": "combot", "scope": "zh", "max_pages": 1},
    )
    listing = await directory_api_client.get("/api/resources", headers=headers)
    resource_id = listing.json()["items"][0]["id"]

    response = await directory_api_client.patch(
        f"/api/resources/{resource_id}",
        headers=headers,
        json={"content_rating": "sensitive"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["content_rating"] == "sensitive"
    assert "content_rating" in response.json()["manual_locked"]

    filtered = await directory_api_client.get(
        "/api/resources",
        headers=headers,
        params={"content_rating": "sensitive"},
    )
    assert filtered.json()["total"] == 1
    # 筛选是真的在过滤：换个站点就搜不到
    other_site = await directory_api_client.get(
        "/api/resources",
        headers=headers,
        params={"source_site": "tgme"},
    )
    assert other_site.json()["total"] == 0


async def test_directory_export_includes_source_columns(
    directory_api_client,
    fake_directory_fetcher,
) -> None:
    headers = await _token(directory_api_client)
    _seed_combot(fake_directory_fetcher)
    await directory_api_client.post(
        "/api/resources/directory/sync",
        headers=headers,
        json={"source": "combot", "scope": "zh", "max_pages": 1},
    )

    response = await directory_api_client.get("/api/resources/export.csv", headers=headers)

    assert response.status_code == 200
    header = response.text.splitlines()[0]
    assert "来源站点" in header
    assert "目录成员数" in header
    assert "内容分级" in header


def test_combot_payload_shape_is_json() -> None:
    """兜底：测试数据本身必须是合法 JSON（防止改 _item 时手滑）。"""

    assert json.loads(_payload(_item("群", "g1", -100111, 1)))[0]["u"] == "g1"
