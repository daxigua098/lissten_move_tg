"""后台可视化登录流程测试（发送验证码 → 验证码 → 两步验证密码）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest
from conftest import ADMIN_API_TOKEN, auth_header, login
from httpx import ASGITransport, AsyncClient


# 故意用同名异常类：服务层按类名识别"需要两步验证"，测试无需构造 Telethon 异常
class SessionPasswordNeededError(Exception):
    """模拟 Telethon 的两步验证异常。"""


class PhoneCodeInvalidError(Exception):
    """模拟验证码错误。"""


class PasswordHashInvalidError(Exception):
    """模拟两步验证密码错误。"""


class FakeLoginClient:
    """登录用 Telethon 替身。"""

    def __init__(self, *, code: str = "12345", password: str = "two-step-pw") -> None:
        self.code = code
        self.password = password
        self.need_password = False
        self.connected = False
        self.disconnected = False
        self.signed_in = False
        self.code_requests = 0

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def send_code_request(self, phone: str, force_sms: bool = False):
        self.code_requests += 1
        return SimpleNamespace(phone_code_hash="demo-code-hash", timeout=60)

    async def sign_in(self, **kwargs):
        password = kwargs.get("password")
        if password is not None:
            if password != self.password:
                raise PasswordHashInvalidError()
            self.signed_in = True
            return SimpleNamespace(id=1)
        if kwargs.get("code") != self.code:
            raise PhoneCodeInvalidError()
        if self.need_password:
            raise SessionPasswordNeededError()
        self.signed_in = True
        return SimpleNamespace(id=1)

    async def get_me(self):
        return SimpleNamespace(
            id=8888001,
            username="demo_login_user",
            first_name="演示账号",
            last_name=None,
            phone=None,
            bot=False,
        )

    async def disconnect(self) -> None:
        self.disconnected = True


@pytest.fixture
def fake_login_client() -> FakeLoginClient:
    """登录替身；测试可先改 need_password 等属性。"""
    return FakeLoginClient()


@pytest.fixture
async def login_client(api_config, fake_login_client) -> AsyncIterator[AsyncClient]:
    """预置一个待登录账号 + 登录替身的接口客户端。"""
    from app.api.app import create_app
    from app.db.session import (
        create_schema,
        dispose_database,
        init_database,
        session_scope,
    )
    from app.services import tg_account_service

    await init_database(api_config)
    await create_schema()
    async with session_scope() as session:
        await tg_account_service.create_account(
            session,
            api_config,
            name="主号机器人",
            phone="+8613800001111",
            is_default=True,
        )

    async def factory(_config, **_kwargs):
        return fake_login_client

    app = create_app(api_config, account_client_factory=factory)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client
    finally:
        await dispose_database()


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def _account_id(login_client) -> int:
    listing = await login_client.get("/api/accounts", headers=_headers())
    return listing.json()["items"][0]["id"]


async def test_web_login_without_two_factor(login_client, fake_login_client, api_config) -> None:
    account_id = await _account_id(login_client)

    started = await login_client.post(
        f"/api/accounts/{account_id}/login/start",
        headers=_headers(),
        json={},
    )
    assert started.status_code == 200
    assert started.json()["status"] == "code_sent"
    assert started.json()["phone_masked"] == "+86***1111"
    assert fake_login_client.connected is True

    status = await login_client.get(
        f"/api/accounts/{account_id}/login/status",
        headers=_headers(),
    )
    assert status.json()["pending"] is True
    assert status.json()["stage"] == "code_sent"

    wrong = await login_client.post(
        f"/api/accounts/{account_id}/login/verify",
        headers=_headers(),
        json={"code": "00000"},
    )
    assert wrong.status_code == 400
    assert "验证码不正确" in wrong.json()["detail"]

    ok = await login_client.post(
        f"/api/accounts/{account_id}/login/verify",
        headers=_headers(),
        json={"code": "12345"},
    )
    body = ok.json()
    assert ok.status_code == 200
    assert body["status"] == "active"
    assert body["username"] == "demo_login_user"
    assert fake_login_client.disconnected is True

    # 账号状态与资料已更新
    listing = await login_client.get("/api/accounts", headers=_headers())
    item = listing.json()["items"][0]
    assert item["status"] == "active"
    assert item["status_label"] == "正常"
    assert item["username"] == "demo_login_user"

    # 登录会话已清理
    after = await login_client.get(
        f"/api/accounts/{account_id}/login/status",
        headers=_headers(),
    )
    assert after.json()["pending"] is False


async def test_web_login_with_two_factor(login_client, fake_login_client) -> None:
    account_id = await _account_id(login_client)
    fake_login_client.need_password = True

    await login_client.post(
        f"/api/accounts/{account_id}/login/start",
        headers=_headers(),
        json={},
    )
    step = await login_client.post(
        f"/api/accounts/{account_id}/login/verify",
        headers=_headers(),
        json={"code": "12345"},
    )
    assert step.status_code == 200
    assert step.json()["status"] == "password_required"

    status = await login_client.get(
        f"/api/accounts/{account_id}/login/status",
        headers=_headers(),
    )
    assert status.json()["stage"] == "password_required"

    wrong = await login_client.post(
        f"/api/accounts/{account_id}/login/password",
        headers=_headers(),
        json={"password": "wrong-password"},
    )
    assert wrong.status_code == 400
    assert "两步验证密码错误" in wrong.json()["detail"]

    done = await login_client.post(
        f"/api/accounts/{account_id}/login/password",
        headers=_headers(),
        json={"password": "two-step-pw"},
    )
    assert done.status_code == 200
    assert done.json()["status"] == "active"


async def test_verify_without_start_is_rejected(login_client) -> None:
    account_id = await _account_id(login_client)

    response = await login_client.post(
        f"/api/accounts/{account_id}/login/verify",
        headers=_headers(),
        json={"code": "12345"},
    )

    assert response.status_code == 400
    assert "登录会话已过期" in response.json()["detail"]


async def test_cancel_login_clears_session(login_client, fake_login_client) -> None:
    account_id = await _account_id(login_client)
    await login_client.post(
        f"/api/accounts/{account_id}/login/start",
        headers=_headers(),
        json={},
    )

    cancelled = await login_client.post(
        f"/api/accounts/{account_id}/login/cancel",
        headers=_headers(),
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["cancelled"] is True
    assert fake_login_client.disconnected is True

    again = await login_client.post(
        f"/api/accounts/{account_id}/login/verify",
        headers=_headers(),
        json={"code": "12345"},
    )
    assert again.status_code == 400


async def test_start_login_twice_resends_code(login_client, fake_login_client) -> None:
    account_id = await _account_id(login_client)

    await login_client.post(
        f"/api/accounts/{account_id}/login/start",
        headers=_headers(),
        json={},
    )
    await login_client.post(
        f"/api/accounts/{account_id}/login/start",
        headers=_headers(),
        json={"force_sms": True},
    )

    assert fake_login_client.code_requests == 2


async def test_login_endpoints_require_super_admin(login_client, api_config) -> None:
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
    token = (await login(login_client, username="dage", password="Pass1234")).json()["token"]
    account_id = await _account_id(login_client)

    response = await login_client.post(
        f"/api/accounts/{account_id}/login/start",
        headers=auth_header(token),
        json={},
    )

    assert response.status_code == 403


async def test_outreach_account_needs_only_phone_code_password(
    login_client, fake_login_client
) -> None:
    """发信息账号：不填 API 凭据（走 .env），只用手机号 + 验证码 + 二级密码登录。"""
    created = await login_client.post(
        "/api/accounts",
        headers=_headers(),
        json={
            "name": "冷聊号",
            "phone": "+8613800002222",
            "purpose": "outreach",
            "owner_confirmed": True,
        },
    )
    assert created.status_code == 201
    account_id = created.json()["id"]
    assert created.json()["purpose"] == "outreach"

    fake_login_client.need_password = True
    started = await login_client.post(
        f"/api/accounts/{account_id}/login/start",
        headers=_headers(),
        json={},
    )
    assert started.status_code == 200
    assert started.json()["status"] == "code_sent"

    step = await login_client.post(
        f"/api/accounts/{account_id}/login/verify",
        headers=_headers(),
        json={"code": "12345"},
    )
    assert step.json()["status"] == "password_required"

    done = await login_client.post(
        f"/api/accounts/{account_id}/login/password",
        headers=_headers(),
        json={"password": "two-step-pw"},
    )
    assert done.status_code == 200
    assert done.json()["status"] == "active"


async def test_two_accounts_can_login_in_parallel(login_client) -> None:
    """批量登录的前提：多个账号可以同时挂着待登录会话，各自提交互不影响。"""
    second = await login_client.post(
        "/api/accounts",
        headers=_headers(),
        json={
            "name": "冷聊号2",
            "phone": "+8613800003333",
            "purpose": "outreach",
            "owner_confirmed": True,
        },
    )
    assert second.status_code == 201
    second_id = second.json()["id"]

    listing = await login_client.get("/api/accounts", headers=_headers())
    ids = [item["id"] for item in listing.json()["items"]]
    assert second_id in ids
    others = [item for item in ids if item != second_id]
    assert others
    first_id = others[0]

    for account_id in (first_id, second_id):
        started = await login_client.post(
            f"/api/accounts/{account_id}/login/start",
            headers=_headers(),
            json={},
        )
        assert started.status_code == 200

    for account_id in (first_id, second_id):
        status = await login_client.get(
            f"/api/accounts/{account_id}/login/status",
            headers=_headers(),
        )
        assert status.json()["pending"] is True
        assert status.json()["stage"] == "code_sent"

    for account_id in (first_id, second_id):
        done = await login_client.post(
            f"/api/accounts/{account_id}/login/verify",
            headers=_headers(),
            json={"code": "12345"},
        )
        assert done.status_code == 200
        assert done.json()["status"] == "active"

    after = await login_client.get("/api/accounts", headers=_headers())
    active = [item for item in after.json()["items"] if item["status"] == "active"]
    assert {item["id"] for item in active} == {first_id, second_id}


async def test_cancel_returns_even_when_disconnect_hangs(
    login_client, fake_login_client, monkeypatch
) -> None:
    """取消登录不能被"断开连接卡住"拖死：超时就返回，窗口才能立刻关掉。"""
    import asyncio
    import time

    from app.services import tg_login_service

    monkeypatch.setattr(tg_login_service, "DISCONNECT_TIMEOUT_SECONDS", 0.2)
    account_id = await _account_id(login_client)
    await login_client.post(
        f"/api/accounts/{account_id}/login/start",
        headers=_headers(),
        json={},
    )

    async def hanging_disconnect() -> None:
        await asyncio.sleep(30)

    fake_login_client.disconnect = hanging_disconnect

    started = time.monotonic()
    response = await login_client.post(
        f"/api/accounts/{account_id}/login/cancel",
        headers=_headers(),
    )
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert response.json()["cancelled"] is True
    assert elapsed < 2, f"取消等了 {elapsed:.2f}s，太慢"
