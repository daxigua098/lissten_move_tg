"""审计中间件与运行状态接口测试。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, auth_header, login


async def test_write_operation_is_audited(admin_client) -> None:
    # 登录也是写操作，此刻尚未认证 → 审计用户名为 anonymous
    assert (await login(admin_client)).status_code == 200

    token = (await login(admin_client)).json()["token"]
    headers = auth_header(token)
    await admin_client.post(
        "/api/users",
        headers=headers,
        json={"username": "dage", "password": "Pass1234", "role": "viewer"},
    )

    response = await admin_client.get("/api/audit", headers=headers, params={"limit": 50})
    items = response.json()["items"]

    assert response.status_code == 200
    assert any(item["path"] == "/api/users" and item["username"] == "admin" for item in items)
    assert any(item["path"] == "/api/auth/login" for item in items)
    assert all(item["method"] in {"POST", "PUT", "PATCH", "DELETE"} for item in items)


async def test_read_operations_are_not_audited(admin_client) -> None:
    headers = auth_header(ADMIN_API_TOKEN)

    await admin_client.get("/api/users", headers=headers)
    response = await admin_client.get("/api/audit", headers=headers)

    assert response.json()["total"] == 0


async def test_failed_login_is_recorded_in_history(admin_client) -> None:
    await login(admin_client, password="wrong-password1")

    headers = auth_header(ADMIN_API_TOKEN)
    response = await admin_client.get("/api/login-history", headers=headers)

    items = response.json()["items"]
    assert response.status_code == 200
    assert items[0]["success"] is False
    assert items[0]["reason"] == "用户名或密码错误"


async def test_login_history_requires_super_admin(admin_client, api_config) -> None:
    from app.db.session import session_scope
    from app.services import user_service

    async with session_scope() as session:
        await user_service.create_user(
            session,
            api_config,
            username="erge",
            password="Pass1234",
            role="viewer",
            must_change_password=False,
        )

    token = (await login(admin_client, username="erge", password="Pass1234")).json()["token"]

    response = await admin_client.get("/api/login-history", headers=auth_header(token))

    assert response.status_code == 403


async def test_system_status_reports_database_and_counts(admin_client) -> None:
    response = await admin_client.get("/api/system/status", headers=auth_header(ADMIN_API_TOKEN))

    body = response.json()
    assert response.status_code == 200
    assert body["database"]["status"] == "ok"
    assert body["counts"]["users"] == 1
    assert body["counts"]["active_super_admins"] == 1
    assert body["retention"]["leads_days"] == 3
    assert body["access_mode"] == "local"
