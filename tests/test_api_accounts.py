"""执行账号池接口测试。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, auth_header


def _payload(**overrides) -> dict:
    payload = {
        "name": "主号",
        "phone": "+8613800001111",
        "api_id": 123456,
        "api_hash": "abcdef0123456789abcdef0123456789",
        "note": "自备主账号",
    }
    payload.update(overrides)
    return payload


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def test_create_account_masks_phone_and_hides_credentials(admin_client) -> None:
    response = await admin_client.post("/api/accounts", headers=_headers(), json=_payload())

    body = response.json()
    assert response.status_code == 201
    assert body["name"] == "主号"
    assert body["phone_masked"] == "+86***1111"
    assert body["status"] == "pending_login"
    assert body["status_label"] == "待登录"
    assert body["health_score"] == 100
    assert body["is_default"] is False
    assert body["session_file"].endswith("主号.session")
    for leaked in ("phone", "api_hash", "api_id", "phone_enc", "api_hash_enc"):
        assert leaked not in body


async def test_credentials_are_encrypted_in_database(admin_client, api_config) -> None:
    await admin_client.post("/api/accounts", headers=_headers(), json=_payload())

    from app.core.security import FieldCipher
    from app.db.session import session_scope
    from app.services import tg_account_service

    async with session_scope() as session:
        account = await tg_account_service.get_account_by_name(session, "主号")
        assert account is not None
        assert "+8613800001111" not in account.phone_enc
        assert "abcdef0123456789" not in account.api_hash_enc

    # 能正确解密回原文
    async with session_scope() as session:
        account = await tg_account_service.get_account_by_name(session, "主号")
        phone, api_id, api_hash = tg_account_service.decrypt_credentials(api_config, account)

    assert phone == "+8613800001111"
    assert api_id == 123456
    assert api_hash.startswith("abcdef")
    assert FieldCipher.from_config(api_config).decrypt(account.phone_enc) == phone


async def test_duplicate_name_and_bad_phone_are_rejected(admin_client) -> None:
    await admin_client.post("/api/accounts", headers=_headers(), json=_payload())

    duplicate = await admin_client.post("/api/accounts", headers=_headers(), json=_payload())
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "USER_EXISTS"

    bad_phone = await admin_client.post(
        "/api/accounts",
        headers=_headers(),
        json=_payload(name="备用1", phone="13800001111"),
    )
    assert bad_phone.status_code == 400
    assert bad_phone.json()["code"] == "VALIDATION_ERROR"


async def test_only_one_default_account(admin_client) -> None:
    first = await admin_client.post(
        "/api/accounts",
        headers=_headers(),
        json=_payload(is_default=True),
    )
    second = await admin_client.post(
        "/api/accounts",
        headers=_headers(),
        json=_payload(name="备用1", phone="+8613900002222", is_default=True),
    )

    assert first.status_code == second.status_code == 201
    listing = await admin_client.get("/api/accounts", headers=_headers())
    items = listing.json()["items"]
    defaults = [item for item in items if item["is_default"]]
    assert len(defaults) == 1
    assert defaults[0]["name"] == "备用1"


async def test_update_status_and_delete(admin_client) -> None:
    created = await admin_client.post("/api/accounts", headers=_headers(), json=_payload())
    account_id = created.json()["id"]

    updated = await admin_client.patch(
        f"/api/accounts/{account_id}",
        headers=_headers(),
        json={"status": "disabled", "note": "暂停使用"},
    )
    assert updated.status_code == 200
    assert updated.json()["status_label"] == "已停用"
    assert updated.json()["note"] == "暂停使用"

    invalid = await admin_client.patch(
        f"/api/accounts/{account_id}",
        headers=_headers(),
        json={"status": "unknown"},
    )
    assert invalid.status_code == 422
    # 校验错误也要走统一的 {detail, code} 结构，前端才能正常提示
    assert invalid.json()["code"] == "VALIDATION_ERROR"
    assert "参数校验失败" in invalid.json()["detail"]

    deleted = await admin_client.delete(f"/api/accounts/{account_id}", headers=_headers())
    assert deleted.status_code == 200
    assert deleted.json() == {"id": account_id, "name": "主号", "deleted": True}

    listing = await admin_client.get("/api/accounts", headers=_headers())
    assert listing.json()["total"] == 0


async def test_accounts_require_super_admin(admin_client, api_config) -> None:
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

    from conftest import login

    token = (await login(admin_client, username="dage", password="Pass1234")).json()["token"]
    response = await admin_client.get("/api/accounts", headers=auth_header(token))

    assert response.status_code == 403


async def test_account_can_inherit_api_credentials_from_env(admin_client, api_config) -> None:
    """账号里不填 API ID/Hash 时，自动用 .env 的默认凭据。"""
    payload = _payload()
    payload.pop("api_id")
    payload.pop("api_hash")

    response = await admin_client.post("/api/accounts", headers=_headers(), json=payload)

    assert response.status_code == 201
    assert response.json()["name"] == "主号"

    from app.db.session import session_scope
    from app.services import tg_account_service

    async with session_scope() as session:
        account = await tg_account_service.get_account_by_name(session, "主号")
        assert account is not None
        phone, api_id, api_hash = tg_account_service.decrypt_credentials(api_config, account)

    assert phone == "+8613800001111"
    assert api_id == api_config.telegram.api_id
    assert api_hash == api_config.telegram.api_hash


async def test_account_rejects_out_of_range_api_id(admin_client) -> None:
    """把用户 ID 填到 API ID 的位置时应给出明确提示，而不是 500。"""
    response = await admin_client.post(
        "/api/accounts",
        headers=_headers(),
        json=_payload(api_id=7506007396, api_hash="a" * 32),
    )

    assert response.status_code == 400
    assert "超出范围" in response.json()["detail"]


async def test_account_rejects_bad_api_hash(admin_client) -> None:
    response = await admin_client.post(
        "/api/accounts",
        headers=_headers(),
        json=_payload(api_id=1234567, api_hash="not-a-valid-api-hash-just-some-text"),
    )

    assert response.status_code == 400
    assert "API Hash 形态不对" in response.json()["detail"]


async def test_refresh_credentials_from_env(admin_client, api_config) -> None:
    """把账号里存的旧凭据换成 .env 里的默认凭据。"""
    created = await admin_client.post(
        "/api/accounts",
        headers=_headers(),
        json=_payload(api_id=1234567, api_hash="a" * 32),
    )
    account_id = created.json()["id"]

    response = await admin_client.post(
        f"/api/accounts/{account_id}/credentials/refresh",
        headers=_headers(),
    )

    assert response.status_code == 200

    from app.db.session import session_scope
    from app.services import tg_account_service

    async with session_scope() as session:
        account = await tg_account_service.get_account(session, account_id)
        _phone, api_id, api_hash = tg_account_service.decrypt_credentials(
            api_config,
            account,
        )

    assert api_id == api_config.telegram.api_id
    assert api_hash == api_config.telegram.api_hash
