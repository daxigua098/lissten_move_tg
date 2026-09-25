"""账户管理接口测试：CRUD、保护规则与角色边界。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, ADMIN_PASSWORD, ADMIN_USERNAME, auth_header, login


def _payload(**overrides) -> dict:
    payload = {
        "username": "dage",
        "password": "Pass1234",
        "role": "sub_admin",
        "display_name": "运营A",
    }
    payload.update(overrides)
    return payload


async def _token(client, username: str = ADMIN_USERNAME, password: str = ADMIN_PASSWORD) -> str:
    response = await login(client, username=username, password=password)
    assert response.status_code == 200, response.text
    return response.json()["token"]


async def test_list_users_returns_single_admin(admin_client) -> None:
    response = await admin_client.get("/api/users", headers=auth_header(await _token(admin_client)))

    body = response.json()
    assert response.status_code == 200
    assert body["total"] == 1
    assert body["items"][0]["username"] == ADMIN_USERNAME
    assert "password_hash" not in body["items"][0]


async def test_create_update_delete_user(admin_client) -> None:
    headers = auth_header(await _token(admin_client))

    created = await admin_client.post("/api/users", headers=headers, json=_payload())
    assert created.status_code == 201
    user_id = created.json()["id"]
    assert created.json()["role"] == "sub_admin"
    # 新建账号默认要求首次登录改密
    assert created.json()["must_change_password"] is True
    assert "password" not in created.json()

    duplicate = await admin_client.post("/api/users", headers=headers, json=_payload())
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "USER_EXISTS"

    updated = await admin_client.patch(
        f"/api/users/{user_id}",
        headers=headers,
        json={"role": "viewer", "display_name": "只读同事"},
    )
    assert updated.status_code == 200
    assert updated.json()["role"] == "viewer"
    assert updated.json()["display_name"] == "只读同事"

    disabled = await admin_client.patch(
        f"/api/users/{user_id}",
        headers=headers,
        json={"enabled": False},
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["revoked_sessions"] == 0

    deleted = await admin_client.delete(f"/api/users/{user_id}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json() == {"id": user_id, "username": "dage", "deleted": True}

    missing = await admin_client.patch(
        "/api/users/9999",
        headers=headers,
        json={"role": "viewer"},
    )
    assert missing.status_code == 404


async def test_cannot_operate_on_self(admin_client, api_config) -> None:
    from app.db.session import session_scope
    from app.services import user_service

    # 用一个非内置的超管来验证自我保护（内置账号另有"不可删除"保护）
    async with session_scope() as session:
        await user_service.create_user(
            session,
            api_config,
            username="sange",
            password="Pass12345",
            role="super_admin",
            must_change_password=False,
        )

    headers = auth_header(await _token(admin_client, username="sange", password="Pass12345"))
    users = (await admin_client.get("/api/users", headers=headers)).json()["items"]
    me_id = next(item["id"] for item in users if item["username"] == "sange")

    self_delete = await admin_client.delete(f"/api/users/{me_id}", headers=headers)
    assert self_delete.status_code == 409
    assert self_delete.json()["code"] == "USER_SELF_DELETE"

    self_disable = await admin_client.patch(
        f"/api/users/{me_id}",
        headers=headers,
        json={"enabled": False},
    )
    assert self_disable.status_code == 409
    assert self_disable.json()["code"] == "USER_SELF_DELETE"


async def test_builtin_admin_cannot_be_deleted(admin_client) -> None:
    headers = auth_header(ADMIN_API_TOKEN)
    me = (await admin_client.get("/api/users", headers=headers)).json()["items"][0]

    response = await admin_client.delete(f"/api/users/{me['id']}", headers=headers)

    assert response.status_code == 409
    assert response.json()["code"] == "CONFLICT"


async def test_last_super_admin_is_protected(admin_client) -> None:
    api_headers = auth_header(ADMIN_API_TOKEN)

    created = await admin_client.post(
        "/api/users",
        headers=api_headers,
        json=_payload(username="sange", password="Pass12345", role="super_admin"),
    )
    assert created.status_code == 201
    sange_id = created.json()["id"]
    admin_id = (await admin_client.get("/api/users", headers=api_headers)).json()["items"][0]["id"]

    # 停用内置管理员：此时 sange 仍是启用的超管，允许
    disabled = await admin_client.patch(
        f"/api/users/{admin_id}",
        headers=api_headers,
        json={"enabled": False},
    )
    assert disabled.status_code == 200

    # 现在 sange 是唯一启用的超管，删除必须被拒绝
    blocked = await admin_client.delete(f"/api/users/{sange_id}", headers=api_headers)

    assert blocked.status_code == 409
    assert blocked.json()["code"] == "USER_LAST_SUPER_ADMIN"


async def test_api_token_is_accepted(admin_client) -> None:
    response = await admin_client.get("/api/users", headers=auth_header(ADMIN_API_TOKEN))

    assert response.status_code == 200


async def test_sub_admin_cannot_manage_users(admin_client, api_config) -> None:
    from app.db.session import session_scope
    from app.services import user_service

    async with session_scope() as session:
        await user_service.create_user(
            session,
            api_config,
            username="dage",
            password="Pass1234",
            role="sub_admin",
            must_change_password=False,
        )

    token = await _token(admin_client, username="dage", password="Pass1234")
    headers = auth_header(token)

    assert (await admin_client.get("/api/audit", headers=headers)).status_code == 200

    forbidden = await admin_client.get("/api/users", headers=headers)
    assert forbidden.status_code == 403
    assert forbidden.json()["code"] == "PERMISSION_DENIED"

    assert (
        await admin_client.post("/api/users", headers=headers, json=_payload())
    ).status_code == 403


async def test_viewer_cannot_write(admin_client, api_config) -> None:
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

    token = await _token(admin_client, username="erge", password="Pass1234")
    headers = auth_header(token)

    assert (await admin_client.get("/api/audit", headers=headers)).status_code == 200
    assert (await admin_client.get("/api/login-history", headers=headers)).status_code == 403
    assert (
        await admin_client.post("/api/users", headers=headers, json=_payload())
    ).status_code == 403
