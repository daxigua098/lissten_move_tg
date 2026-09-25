"""前端静态托管与 SPA 回退测试。"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.fixture
async def spa_client(api_config) -> AsyncIterator[AsyncClient]:
    """构造带假前端产物的项目，验证静态托管与深链接回退。"""
    from app.api.app import create_app
    from app.db.session import create_schema, dispose_database, init_database

    dist = api_config.project_root / "frontend" / "dist"
    (dist / "assets").mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text("<!doctype html><title>SPA</title>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("console.log(1);", encoding="utf-8")

    await init_database(api_config)
    await create_schema()
    app = create_app(api_config)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        await dispose_database()


async def test_root_serves_index(spa_client) -> None:
    response = await spa_client.get("/")

    assert response.status_code == 200
    assert "SPA" in response.text


async def test_deep_link_falls_back_to_index(spa_client) -> None:
    """刷新 /routes 这类前端路由不应再返回 {"detail":"Not Found"}。"""
    for path in ("/routes", "/sources", "/ad-assets", "/login"):
        response = await spa_client.get(path)
        assert response.status_code == 200, path
        assert "SPA" in response.text


async def test_static_asset_is_served(spa_client) -> None:
    response = await spa_client.get("/assets/app.js")

    assert response.status_code == 200
    assert "console.log" in response.text


async def test_unknown_api_path_is_json_404(spa_client) -> None:
    response = await spa_client.get("/api/not-exist")

    assert response.status_code == 404
    assert response.json()["detail"] == "Not Found"
    assert "SPA" not in response.text


async def test_health_still_served(spa_client) -> None:
    response = await spa_client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
