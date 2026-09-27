"""目录状态接口与目录候选的资源库筛选。"""

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


async def test_directory_status_is_simplified_and_read_only(
    directory_api_client,
    api_config,
    fake_directory_fetcher,
) -> None:
    from app.db.session import session_scope
    from app.services import directory_sync_service

    headers = await _token(directory_api_client)
    async with session_scope() as session:
        await directory_sync_service.ensure_default_task(session, tenant_id=1)

    response = await directory_api_client.get("/api/resources/directory/sources", headers=headers)

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["source"] == "combot"
    assert payload["scope"] == "zh"
    assert payload["pages_total"] == 24
    assert payload["auto"] is True
    assert payload["last_run"] is None
    # 看状态不出网
    assert fake_directory_fetcher.requests == []


async def test_directory_candidates_are_filterable_and_exportable(
    directory_api_client,
    api_config,
    fake_directory_fetcher,
) -> None:
    headers = await _token(directory_api_client)
    _seed_combot(fake_directory_fetcher)
    await _store_combot(api_config, fake_directory_fetcher)

    overview = await directory_api_client.get("/api/resources/directory/sources", headers=headers)
    assert overview.json()["last_run"]["items_added"] == 2
    assert overview.json()["last_run"]["result"] == "partial"

    listing = await directory_api_client.get(
        "/api/resources",
        headers=headers,
        params={"source_site": "combot"},
    )
    assert listing.json()["total"] == 2
    first = listing.json()["items"][0]
    assert first["source_site"] == "combot"
    assert first["directory_member_count"] == 172156
    assert first["member_count"] is None
    assert first["discovered_by"] == "directory"

    exported = await directory_api_client.get("/api/resources/export.csv", headers=headers)
    assert exported.status_code == 200
    header = exported.text.splitlines()[0]
    assert "来源站点" in header
    assert "目录成员数" in header
    assert "内容分级" in header


async def test_manual_content_rating_is_locked(
    directory_api_client,
    api_config,
    fake_directory_fetcher,
) -> None:
    headers = await _token(directory_api_client)
    _seed_combot(fake_directory_fetcher)
    await _store_combot(api_config, fake_directory_fetcher)
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


def test_combot_payload_shape_is_json() -> None:
    """兜底：测试数据本身必须是合法 JSON（防止改 _item 时手滑）。"""

    assert json.loads(_payload(_item("群", "g1", -100111, 1)))[0]["u"] == "g1"
