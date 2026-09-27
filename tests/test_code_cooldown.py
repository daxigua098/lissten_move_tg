"""取码结果保存、30 分钟冷却与已登录跳过。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, auth_header

CODE_URL = "https://logincode.test/?token=abc"


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def _account(
    admin_client, api_config, *, name: str, phone: str, code_url: str | None = CODE_URL
):
    from app.db.session import session_scope
    from app.services import tg_account_service

    async with session_scope() as session:
        account = await tg_account_service.create_account(
            session,
            api_config,
            name=name,
            phone=phone,
            purpose="outreach",
            owner_confirmed=True,
            code_url=code_url,
        )
        return account.id


def _fake_logincode(status: str, *, code: str = "", password: str = ""):
    async def http_get(url: str, *, timeout: float = 20.0) -> tuple[int, str]:
        if "config.js" in url:
            return 200, 'VITE_API_BASE_URL: "https://api.test"'
        if "/getCode/" in url:
            return 200, '{"code":200,"data":{}}'
        return 200, (
            '{"code":200,"status":"' + status + '","message":"没有三十分钟内的登入验证码消息！",'
            '"data":{"loginCode":"' + code + '","password":"' + password + '","successTimes":1}}'
        )

    return http_get


async def test_fetched_code_is_saved_on_account(admin_client, api_config) -> None:
    from app.db.session import session_scope
    from app.services import tg_account_service

    account_id = await _account(admin_client, api_config, name="+14135030718", phone="+14135030718")

    async with session_scope() as session:
        account = await tg_account_service.get_account(session, account_id)
        await tg_account_service.save_fetched_code(
            session,
            api_config,
            account,
            code="24680",
            password="two-step-pw",
        )

    listing = await admin_client.get(
        "/api/accounts", headers=_headers(), params={"purpose": "outreach"}
    )
    item = next(row for row in listing.json()["items"] if row["id"] == account_id)
    assert item["has_code"] is True
    assert item["has_2fa"] is True
    # 明文不能出现在响应里
    assert "24680" not in listing.text
    assert "two-step-pw" not in listing.text


async def test_active_account_is_skipped_by_auto_login(admin_client, api_config) -> None:
    from app.db.session import session_scope
    from app.services import outreach_auto_login_service as service
    from app.services import tg_account_service

    account_id = await _account(admin_client, api_config, name="已登录号", phone="+14135030718")
    async with session_scope() as session:
        account = await tg_account_service.get_account(session, account_id)
        account.status = "active"
        await session.commit()

    result = await service.start(api_config, [account_id])

    assert result["started"] == []
    assert result["skipped"][0]["account_id"] == account_id
    assert "已登录" in result["skipped"][0]["reason"]


async def test_code_cooldown_marks_account_and_skips(admin_client, api_config) -> None:
    from app.db.session import session_scope
    from app.services import outreach_auto_login_service as service
    from app.services import tg_account_service

    account_id = await _account(admin_client, api_config, name="冷却号", phone="+17347499052")
    async with session_scope() as session:
        account = await tg_account_service.get_account(session, account_id)
        await tg_account_service.mark_code_cooldown(session, account)
        assert tg_account_service.code_cooldown_remaining(account) > 0

    result = await service.start(api_config, [account_id])

    assert result["started"] == []
    assert "冷却" in result["skipped"][0]["reason"]

    listing = await admin_client.get(
        "/api/accounts", headers=_headers(), params={"purpose": "outreach"}
    )
    item = next(row for row in listing.json()["items"] if row["id"] == account_id)
    assert item["code_cooldown_remaining"] > 0
    assert item["code_cooldown_until"] is not None


async def test_logincode_failed_status_raises_cooldown() -> None:
    import pytest

    from app.core import logincode

    service = logincode.LogincodeService(token="abc", api_base="https://api.test/verification")
    with pytest.raises(logincode.LogincodeCooldown):
        await logincode.wait_for_code(
            service,
            http_get=_fake_logincode("failed"),
            timeout=1,
            poll=0,
            first_delay=0,
        )


async def test_code_fetch_marks_cooldown_via_api(admin_client, api_config, monkeypatch) -> None:
    from app.api.routers import accounts as accounts_router
    from app.core import logincode
    from app.db.session import session_scope
    from app.services import tg_account_service

    monkeypatch.setattr(accounts_router, "CODE_FETCH_FIRST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(accounts_router, "CODE_FETCH_POLL_SECONDS", 0.0)
    monkeypatch.setattr(logincode, "default_http_get", _fake_logincode("failed"))

    account_id = await _account(admin_client, api_config, name="平台冷却号", phone="+14135030718")

    response = await admin_client.post(
        f"/api/accounts/{account_id}/code/fetch",
        headers=_headers(),
        json={"source": "logincode", "timeout_seconds": 10},
    )

    assert response.status_code == 400
    assert "30 分钟" in response.json()["detail"] or "验证码" in response.json()["detail"]

    async with session_scope() as session:
        account = await tg_account_service.get_account(session, account_id)
        assert account is not None
        assert tg_account_service.code_cooldown_remaining(account) > 0


async def test_code_url_endpoint_returns_saved_url(admin_client, api_config) -> None:
    account_id = await _account(admin_client, api_config, name="有接码地址", phone="+14135030718")

    response = await admin_client.get(f"/api/accounts/{account_id}/code-url", headers=_headers())

    assert response.status_code == 200
    assert response.json()["code_url"] == CODE_URL

    without = await _account(
        admin_client,
        api_config,
        name="没接码地址",
        phone="+17347499052",
        code_url=None,
    )
    missing = await admin_client.get(f"/api/accounts/{without}/code-url", headers=_headers())
    assert missing.status_code == 400
