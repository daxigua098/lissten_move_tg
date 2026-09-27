"""登录窗口的自动取码：接码平台（logincode）与 2925 邮箱。"""

from __future__ import annotations

import pytest
from conftest import ADMIN_API_TOKEN, auth_header


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def _make_account(
    admin_client, api_config, *, name: str, phone: str, code_url: str | None = None
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


def test_mail2925_alias_and_code_extraction() -> None:
    from app.core import mail2925
    from app.core.errors import ValidationFailedError

    assert mail2925.derive_alias("Box@2925.com", "+14135030718") == "box_14135030718@2925.com"
    assert mail2925.extract_telegram_code("Your login code: 98765") == "98765"
    assert mail2925.extract_telegram_code("Telegram 验证码：54321") == "54321"

    with pytest.raises(ValidationFailedError):
        mail2925.derive_alias("box@gmail.com", "+14135030718")
    with pytest.raises(ValidationFailedError):
        mail2925.normalize_config("box@gmail.com", "pw")


async def test_fetch_code_from_logincode(admin_client, api_config, monkeypatch) -> None:
    from app.api.routers import accounts as accounts_router
    from app.core import logincode

    monkeypatch.setattr(accounts_router, "CODE_FETCH_FIRST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(accounts_router, "CODE_FETCH_POLL_SECONDS", 0.01)

    async def fake_get(url: str, *, timeout: float = 20.0) -> tuple[int, str]:
        if "config.js" in url:
            return 200, 'VITE_API_BASE_URL: "https://api.test"'
        if "/getCode/" in url:
            return 200, '{"code":200,"data":{}}'
        return 200, (
            '{"code":200,"status":"success","data":'
            '{"loginCode":"24680","password":"two-step-pw",'
            '"account":"+14135030718","successTimes":1}}'
        )

    monkeypatch.setattr(logincode, "default_http_get", fake_get)

    account_id = await _make_account(
        admin_client,
        api_config,
        name="+14135030718",
        phone="+14135030718",
        code_url="https://logincode.test/?token=abc",
    )

    response = await admin_client.post(
        f"/api/accounts/{account_id}/code/fetch",
        headers=_headers(),
        json={"source": "logincode", "timeout_seconds": 30},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "24680"
    assert body["password"] == "two-step-pw"
    assert body["source"] == "logincode"


async def test_fetch_code_requires_a_code_url(admin_client, api_config) -> None:
    account_id = await _make_account(
        admin_client,
        api_config,
        name="没接码地址",
        phone="+17347499052",
    )

    response = await admin_client.post(
        f"/api/accounts/{account_id}/code/fetch",
        headers=_headers(),
        json={"source": "logincode", "timeout_seconds": 10},
    )

    assert response.status_code == 400
    assert "接码地址" in response.json()["detail"]


async def test_fetch_code_from_2925_mailbox(admin_client, api_config, monkeypatch) -> None:
    from app.api.routers import accounts as accounts_router

    monkeypatch.setattr(accounts_router, "CODE_FETCH_MAIL_POLL_SECONDS", 0.0)
    seen: dict = {}

    async def fetcher(config, *, alias):
        seen["main_email"] = config.main_email
        seen["alias"] = alias
        return {"code": "13579", "alias": alias, "subject": "Telegram login code"}

    admin_client._transport.app.state.mail2925_fetcher = fetcher

    account_id = await _make_account(
        admin_client,
        api_config,
        name="+14135030718",
        phone="+14135030718",
    )

    response = await admin_client.post(
        f"/api/accounts/{account_id}/code/fetch",
        headers=_headers(),
        json={
            "source": "mail2925",
            "mail_user": "Box@2925.com",
            "mail_pass": "mail-pw",
            "timeout_seconds": 10,
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "13579"
    assert body["alias"] == "box_14135030718@2925.com"
    assert seen["main_email"] == "box@2925.com"
    assert seen["alias"] == "box_14135030718@2925.com"


async def test_fetch_code_from_2925_rejects_bad_mail(admin_client, api_config) -> None:
    account_id = await _make_account(
        admin_client,
        api_config,
        name="+17347499052",
        phone="+17347499052",
    )

    response = await admin_client.post(
        f"/api/accounts/{account_id}/code/fetch",
        headers=_headers(),
        json={
            "source": "mail2925",
            "mail_user": "box@gmail.com",
            "mail_pass": "x",
            "timeout_seconds": 10,
        },
    )

    assert response.status_code == 400
    assert "2925" in response.json()["detail"]
