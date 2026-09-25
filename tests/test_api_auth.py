"""登录、会话、改密与锁定接口测试。"""

from __future__ import annotations

from conftest import ADMIN_PASSWORD, ADMIN_USERNAME, auth_header, login


async def test_health_is_public(client) -> None:
    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert "X-Request-Id" in response.headers


async def test_login_success_returns_token(admin_client) -> None:
    response = await login(admin_client)

    body = response.json()
    assert response.status_code == 200
    assert body["username"] == ADMIN_USERNAME
    assert body["role"] == "super_admin"
    assert body["must_change_password"] is False
    assert body["is_builtin"] is True
    assert len(body["token"]) > 20
    assert body["expires_at"].endswith("+00:00")


async def test_login_with_wrong_password_uses_uniform_message(admin_client) -> None:
    failed = await login(admin_client, password="wrong-password1")
    unknown = await login(admin_client, username="nobody", password="wrong-password1")

    assert failed.status_code == 401
    assert unknown.status_code == 401
    assert failed.json() == unknown.json()
    assert failed.json()["code"] == "AUTH_INVALID_CREDENTIALS"


async def test_check_requires_token(admin_client) -> None:
    anonymous = await admin_client.get("/api/auth/check")
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "AUTH_REQUIRED"

    token = (await login(admin_client)).json()["token"]
    authenticated = await admin_client.get("/api/auth/check", headers=auth_header(token))
    assert authenticated.status_code == 200
    assert authenticated.json()["username"] == ADMIN_USERNAME


async def test_logout_revokes_current_session(admin_client) -> None:
    token = (await login(admin_client)).json()["token"]

    logged_out = await admin_client.post("/api/auth/logout", headers=auth_header(token))
    assert logged_out.status_code == 200
    assert logged_out.json() == {"logged_out": True}

    after = await admin_client.get("/api/auth/check", headers=auth_header(token))
    assert after.status_code == 401


async def test_logout_all_revokes_every_session(admin_client) -> None:
    first = (await login(admin_client)).json()["token"]
    second = (await login(admin_client)).json()["token"]

    response = await admin_client.post("/api/auth/logout-all", headers=auth_header(second))
    assert response.status_code == 200
    assert response.json()["revoked"] >= 2

    assert (
        await admin_client.get("/api/auth/check", headers=auth_header(first))
    ).status_code == 401
    assert (
        await admin_client.get("/api/auth/check", headers=auth_header(second))
    ).status_code == 401


async def test_first_login_must_change_password(fresh_admin_client) -> None:
    body = (await login(fresh_admin_client)).json()
    assert body["must_change_password"] is True
    token = body["token"]

    blocked = await fresh_admin_client.get("/api/audit", headers=auth_header(token))
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "AUTH_PASSWORD_CHANGE_REQUIRED"

    allowed = await fresh_admin_client.get("/api/auth/check", headers=auth_header(token))
    assert allowed.status_code == 200


async def test_builtin_admin_password_must_change_in_env(admin_client) -> None:
    token = (await login(admin_client)).json()["token"]

    response = await admin_client.patch(
        "/api/auth/password",
        headers=auth_header(token),
        json={"current_password": ADMIN_PASSWORD, "new_password": "NewPass1234"},
    )

    assert response.status_code == 400
    assert response.json()["code"] == "AUTH_BUILTIN_PASSWORD_ENV"


async def test_builtin_admin_is_not_blocked_by_first_login_gate(admin_client, api_config) -> None:
    from app.db.session import session_scope
    from app.services import user_service

    # 内置账号即使被标记为需改密，也不应该被门槛拦住
    async with session_scope() as session:
        user = await user_service.get_user_by_username(session, ADMIN_USERNAME)
        assert user is not None
        user.must_change_password = True
        await session.commit()

    token = (await login(admin_client)).json()["token"]

    response = await admin_client.get("/api/audit", headers=auth_header(token))

    assert response.status_code == 200


async def test_password_change_rejects_weak_password(admin_client, api_config) -> None:
    from app.db.session import session_scope
    from app.services import user_service

    async with session_scope() as session:
        user = await user_service.get_user_by_username(session, ADMIN_USERNAME)
        assert user is not None
        user.is_builtin = False
        await session.commit()

    token = (await login(admin_client)).json()["token"]

    weak = await admin_client.patch(
        "/api/auth/password",
        headers=auth_header(token),
        json={"current_password": ADMIN_PASSWORD, "new_password": "short"},
    )
    assert weak.status_code == 400
    assert weak.json()["code"] == "AUTH_PASSWORD_WEAK"

    wrong_current = await admin_client.patch(
        "/api/auth/password",
        headers=auth_header(token),
        json={"current_password": "not-the-password", "new_password": "NewPass1234"},
    )
    assert wrong_current.status_code == 401

    ok = await admin_client.patch(
        "/api/auth/password",
        headers=auth_header(token),
        json={"current_password": ADMIN_PASSWORD, "new_password": "NewPass1234"},
    )
    assert ok.status_code == 200
    assert ok.json()["changed"] is True

    again = await login(admin_client, password="NewPass1234")
    assert again.status_code == 200


async def test_login_locks_after_max_failures(admin_client, api_config) -> None:
    for _ in range(api_config.security.max_login_failures):
        response = await login(admin_client, password="wrong-password1")
        assert response.status_code == 401

    locked = await login(admin_client)

    assert locked.status_code == 423
    body = locked.json()
    assert body["code"] == "AUTH_LOCKED"
    assert "locked_until" in body
