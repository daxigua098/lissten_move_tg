"""发信息账号导入（手机号 + 接码地址）与接码平台取码自动登录。"""

from __future__ import annotations

import asyncio

import pytest
from conftest import ADMIN_API_TOKEN, auth_header

SAMPLE_LINES = (
    "+14135030718 https://logincode.add4533.com/?token=da792cfe-a2ed-4445-a92f-8735e2f445dc\n"
    "+17347499052 https://logincode.add4533.com/?token=40cce5cf-8eee-4fbb-a524-64b4c646c739"
)


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


def test_parse_account_lines_reads_phone_and_code_url() -> None:
    from app.core.account_import import parse_account_lines

    accounts, errors = parse_account_lines(SAMPLE_LINES)

    assert errors == []
    assert [item.phone for item in accounts] == ["+14135030718", "+17347499052"]
    assert accounts[0].code_host == "logincode.add4533.com"
    assert accounts[0].code_url.endswith("token=da792cfe-a2ed-4445-a92f-8735e2f445dc")


def test_parse_account_lines_reports_bad_rows() -> None:
    from app.core.account_import import parse_account_lines

    accounts, errors = parse_account_lines(
        "\n".join(
            [
                "14135030718 https://logincode.test/?token=a",  # 缺区号
                "+17347499052",  # 缺接码地址
                "+14135030718 https://logincode.test/?token=a",  # 正常
                "+14135030718 https://logincode.test/?token=b",  # 与本批次重复
                "随便一行",
            ]
        )
    )

    assert [item.phone for item in accounts] == ["+14135030718"]
    assert len(errors) == 4
    assert "区号" in errors[0]["message"]


async def test_import_api_creates_outreach_accounts(admin_client) -> None:
    response = await admin_client.post(
        "/api/accounts/import",
        headers=_headers(),
        json={"text": SAMPLE_LINES, "owner_confirmed": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["errors"] == []

    first = body["created"][0]
    assert first["purpose"] == "outreach"
    assert first["has_code_url"] is True
    assert first["code_host"] == "logincode.add4533.com"
    # 接码地址里的 token 属凭据，不能出现在响应里
    assert "da792cfe" not in response.text
    assert "logincode.add4533.com/?token" not in response.text
    assert "code_url" not in first

    listing = await admin_client.get(
        "/api/accounts",
        headers=_headers(),
        params={"purpose": "outreach"},
    )
    assert listing.json()["total"] == 2


async def test_import_requires_confirmation_and_reports_duplicates(admin_client) -> None:
    blocked = await admin_client.post(
        "/api/accounts/import",
        headers=_headers(),
        json={"text": SAMPLE_LINES},
    )
    assert blocked.status_code == 400
    assert "确认" in blocked.json()["detail"]

    first = await admin_client.post(
        "/api/accounts/import",
        headers=_headers(),
        json={"text": SAMPLE_LINES, "owner_confirmed": True},
    )
    assert first.status_code == 200

    again = await admin_client.post(
        "/api/accounts/import",
        headers=_headers(),
        json={"text": SAMPLE_LINES, "owner_confirmed": True},
    )
    body = again.json()
    assert body["total"] == 0
    assert len(body["errors"]) == 2
    assert "已存在" in body["errors"][0]["message"]


def _fake_logincode(code: str = "12345", password: str = "two-step-pw"):
    async def http_get(url: str, *, timeout: float = 20.0) -> tuple[int, str]:
        if "config.js" in url:
            return 200, 'VITE_API_BASE_URL: "https://api.test"'
        if "/getCode/" in url:
            return 200, '{"code":200,"data":{}}'
        if "/getMsg/" in url:
            return 200, (
                '{"code":200,"status":"success","data":'
                f'{{"loginCode":"{code}","password":"{password}",'
                '"account":"+14135030718","successTimes":1}}'
            )
        return 404, ""

    return http_get


class FakeLoginClient:
    """Telethon 登录替身（与 test_api_account_login 同口径）。"""

    def __init__(self, *, need_password: bool = False) -> None:
        self.need_password = need_password
        self.connected = False
        self.disconnected = False

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def send_code_request(self, phone: str, force_sms: bool = False):
        from types import SimpleNamespace

        return SimpleNamespace(phone_code_hash="hash", timeout=60)

    async def sign_in(self, **kwargs):
        from types import SimpleNamespace

        if kwargs.get("password") is not None:
            if kwargs["password"] != "two-step-pw":
                raise PasswordHashInvalidError()
            return SimpleNamespace(id=1)
        if kwargs.get("code") != "12345":
            raise PhoneCodeInvalidError()
        if self.need_password:
            raise SessionPasswordNeededError()
        return SimpleNamespace(id=1)

    async def get_me(self):
        from types import SimpleNamespace

        return SimpleNamespace(
            id=7777001,
            username="auto_login_user",
            first_name="自动登录",
            last_name=None,
            phone=None,
            bot=False,
        )

    async def disconnect(self) -> None:
        self.disconnected = True


class SessionPasswordNeededError(Exception):
    pass


class PhoneCodeInvalidError(Exception):
    pass


class PasswordHashInvalidError(Exception):
    pass


async def _wait_done(account_id: int, *, timeout: float = 5.0) -> dict:
    from app.services import outreach_auto_login_service as service

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        items = {item["account_id"]: item for item in service.to_payload()}
        current = items.get(account_id)
        if current and current["status"] in {"success", "failed", "stopped"}:
            return current
        await asyncio.sleep(0.02)
    raise AssertionError("自动登录没有在超时内结束")


async def test_auto_login_fetches_code_and_password(admin_client, api_config, monkeypatch) -> None:
    from app.db.session import session_scope
    from app.services import outreach_auto_login_service as service
    from app.services import tg_account_service

    monkeypatch.setattr(service, "FIRST_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(service, "POLL_SECONDS", 0.01)

    async with session_scope() as session:
        account = await tg_account_service.create_account(
            session,
            api_config,
            name="+14135030718",
            phone="+14135030718",
            purpose="outreach",
            owner_confirmed=True,
            code_url="https://logincode.test/?token=abc",
        )
        account_id = account.id

    client = FakeLoginClient(need_password=True)

    async def factory(_config, **_kwargs):
        return client

    await service.start(
        api_config,
        [account_id],
        http_get=_fake_logincode(),
        client_factory=factory,
    )
    result = await _wait_done(account_id)

    assert result["status"] == "success", result["message"]
    assert "auto_login_user" in result["message"]

    async with session_scope() as session:
        fresh = await tg_account_service.get_account(session, account_id)
        assert fresh is not None and fresh.status == "active"


async def test_auto_login_without_code_url_fails_clearly(admin_client, api_config) -> None:
    from app.db.session import session_scope
    from app.services import outreach_auto_login_service as service
    from app.services import tg_account_service

    async with session_scope() as session:
        account = await tg_account_service.create_account(
            session,
            api_config,
            name="没接码地址",
            phone="+17347499052",
            purpose="outreach",
            owner_confirmed=True,
        )
        account_id = account.id

    await service.start(api_config, [account_id])
    result = await _wait_done(account_id)

    assert result["status"] == "failed"
    assert "接码地址" in result["message"]


async def test_auto_login_endpoints_and_stop(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import tg_account_service

    async with session_scope() as session:
        account = await tg_account_service.create_account(
            session,
            admin_client._transport.app.state.config,
            name="接口自动登录",
            phone="+14135030718",
            purpose="outreach",
            owner_confirmed=True,
        )
        account_id = account.id

    started = await admin_client.post(
        "/api/accounts/auto-login",
        headers=_headers(),
        json={"account_ids": [account_id]},
    )
    assert started.status_code == 200
    assert account_id in started.json()["started"]

    await _wait_done(account_id)

    listing = await admin_client.get("/api/accounts/auto-login/status", headers=_headers())
    assert listing.status_code == 200
    assert any(item["account_id"] == account_id for item in listing.json()["items"])

    stopped = await admin_client.post(
        "/api/accounts/auto-login/stop",
        headers=_headers(),
        json={"account_ids": [account_id]},
    )
    assert stopped.status_code == 200

    empty = await admin_client.post(
        "/api/accounts/auto-login",
        headers=_headers(),
        json={"account_ids": []},
    )
    assert empty.status_code == 400


def test_logincode_normalize_reads_code_and_password() -> None:
    from app.core.logincode import normalize

    result = normalize(
        {
            "code": 200,
            "status": "success",
            "data": {"loginCode": "97605", "password": "8899", "account": "+1_413_503_0718"},
        }
    )

    assert result["login_code"] == "97605"
    assert result["password"] == "8899"
    assert result["account"] == "+14135030718"


async def test_logincode_service_rejects_bad_url() -> None:
    from app.core.errors import ValidationFailedError
    from app.core.logincode import parse_login_url

    with pytest.raises(ValidationFailedError):
        parse_login_url("https://logincode.test/")  # 缺 token
    with pytest.raises(ValidationFailedError):
        parse_login_url("ftp://logincode.test/?token=a")
