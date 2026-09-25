"""pytest 公共夹具。"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def reset_logger():
    """每个用例后清空 loguru sink，避免写入已关闭的捕获流。"""
    yield
    from loguru import logger

    logger.remove()


@pytest.fixture
def valid_secret_key() -> str:
    """形态合法的 Fernet 密钥（43 位 urlsafe base64 + 等号）。"""
    return "A" * 43 + "="


ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "admin123"
ADMIN_API_TOKEN = "test-api-token-please-change"


def database_url(project_root: Path) -> str:
    """临时项目根目录下的绝对 SQLite 地址。"""
    return "sqlite+aiosqlite:///" + (project_root / "data" / "app.db").as_posix()


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    """临时的项目根目录，含 configs 目录。"""
    root = tmp_path / "project"
    (root / "configs").mkdir(parents=True)
    return root


@pytest.fixture
def write_config(project_root: Path):
    """写入 configs/config.yaml 的辅助函数。"""

    def _write(text: str) -> Path:
        path = project_root / "configs" / "config.yaml"
        path.write_text(text, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def write_env(project_root: Path):
    """写入 .env 的辅助函数。"""

    def _write(lines: list[str]) -> Path:
        path = project_root / ".env"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    return _write


@pytest.fixture
def api_config(project_root: Path, valid_secret_key: str):
    """用于接口测试的配置（独立临时数据库）。"""
    from app.core.config import load_config

    return load_config(
        project_root=project_root,
        environ={
            "SECRET_KEY": valid_secret_key,
            "ADMIN_PASSWORD": "custom-pass1",
            "ADMIN_API_TOKEN": ADMIN_API_TOKEN,
            "DATABASE_URL": database_url(project_root),
            "TG_API_ID": "1234567",
            "TG_API_HASH": "0123456789abcdef0123456789abcdef",
        },
    )


@pytest.fixture
async def client(api_config) -> AsyncIterator[AsyncClient]:
    """已建表、未建账号的接口客户端。"""
    from app.api.app import create_app
    from app.db.session import create_schema, dispose_database, init_database

    await init_database(api_config)
    await create_schema()
    app = create_app(api_config)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        await dispose_database()


async def _create_admin(
    api_config,
    *,
    must_change_password: bool,
    is_builtin: bool = True,
) -> None:
    from app.db.models import ROLE_SUPER_ADMIN
    from app.db.session import session_scope
    from app.services import user_service

    async with session_scope() as session:
        await user_service.create_user(
            session,
            api_config,
            username=ADMIN_USERNAME,
            password=ADMIN_PASSWORD,
            role=ROLE_SUPER_ADMIN,
            display_name="超级管理员",
            is_builtin=is_builtin,
            must_change_password=must_change_password,
        )


@pytest.fixture
async def admin_client(api_config) -> AsyncIterator[AsyncClient]:
    """已存在可直接使用的超级管理员（无需强制改密）的客户端。"""
    from app.api.app import create_app
    from app.db.session import create_schema, dispose_database, init_database

    await init_database(api_config)
    await create_schema()
    await _create_admin(api_config, must_change_password=False, is_builtin=True)
    app = create_app(api_config)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        await dispose_database()


@pytest.fixture
async def fresh_admin_client(api_config) -> AsyncIterator[AsyncClient]:
    """内置管理员处于"首次登录必须改密"状态的客户端。"""
    from app.api.app import create_app
    from app.db.session import create_schema, dispose_database, init_database

    await init_database(api_config)
    await create_schema()
    # 用非内置账号验证"首次登录必须改密"的门槛（内置账号的密码在 .env 管理，不设门槛）
    await _create_admin(api_config, must_change_password=True, is_builtin=False)
    app = create_app(api_config)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        await dispose_database()


async def login(
    client: AsyncClient, username: str = ADMIN_USERNAME, password: str = ADMIN_PASSWORD
):
    """登录并返回响应。"""
    return await client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )


def auth_header(token: str) -> dict[str, str]:
    """构造 Bearer 头。"""
    return {"Authorization": f"Bearer {token}"}


class FakeBotClient:
    """Telethon 替身：Token 以 `valid-` 开头视为有效。"""

    def __init__(self, token: str) -> None:
        self.token = token
        self.disconnected = False

    async def get_me(self) -> SimpleNamespace:
        if not self.token.startswith("valid-"):
            raise RuntimeError("AccessTokenInvalidError: token 无效")
        stem = self.token.split("-", 1)[1] or "bot"
        return SimpleNamespace(
            id=7000000 + len(self.token),
            username=f"{stem}_bot",
            first_name="测试机器人",
            last_name=None,
            phone=None,
            bot=True,
        )

    async def disconnect(self) -> None:
        self.disconnected = True


def make_entity(
    tg_id: int,
    title: str,
    *,
    broadcast: bool = False,
    megagroup: bool = True,
    username: str | None = None,
    participants_count: int | None = None,
):
    """构造 Telethon 实体替身。"""
    from types import SimpleNamespace

    return SimpleNamespace(
        id=tg_id,
        title=title,
        broadcast=broadcast,
        megagroup=megagroup,
        username=username,
        participants_count=participants_count,
    )


class FakeAccountClient:
    """Telethon 账号客户端替身：提供 dialogs / get_entity / get_permissions。"""

    def __init__(self) -> None:
        from types import SimpleNamespace

        self.dialogs: list = []
        self.entities: dict = {}
        self.permissions: dict = {}
        self.imported = None
        self.disconnected = False
        self.SimpleNamespace = SimpleNamespace

    async def iter_dialogs(self, limit: int | None = None):
        for entity in self.dialogs[: limit or len(self.dialogs)]:
            yield self.SimpleNamespace(entity=entity)

    async def get_entity(self, identifier):
        key = identifier.lstrip("@") if isinstance(identifier, str) else identifier
        if key in self.entities:
            return self.entities[key]
        raise ValueError(f"找不到实体：{identifier}")

    async def get_permissions(self, entity, user=None):
        """替身：语义对齐 Telethon。

        不带 user 时返回这个群的默认限制；带 user 时返回「我在这个群的权限」。
        `self.permissions[tg_id]` 表示"执行账号能否在该群发言"：
        True 建模成群主/管理员，False 建模成广播频道的普通订阅者。
        """
        if user is None:
            return self.SimpleNamespace(send_messages=False, post_messages=False)
        allowed = self.permissions.get(getattr(entity, "id", None))
        if allowed is None:
            return None
        is_broadcast = bool(getattr(entity, "broadcast", False))
        return self.SimpleNamespace(
            is_creator=bool(allowed),
            is_admin=bool(allowed),
            is_banned=not allowed and not is_broadcast,
            has_left=False,
            post_messages=bool(allowed),
        )

    async def get_me(self, input_peer: bool = False):
        if input_peer:
            return self.SimpleNamespace(user_id=8000001, _="InputPeerSelf")
        return self.SimpleNamespace(
            id=8000001,
            username="fake_account",
            first_name="替身账号",
            last_name=None,
            phone=None,
            bot=False,
        )

    async def is_user_authorized(self) -> bool:
        return True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def __call__(self, request):
        name = type(request).__name__
        if name == "ImportChatInviteRequest":
            return self.SimpleNamespace(chats=[self.imported] if self.imported else [])
        raise AssertionError(f"未预期的请求：{name}")


def fake_message(
    message_id: int,
    text: str = "",
    *,
    photo: bool = False,
    video: bool = False,
    pinned: bool = False,
    grouped_id: int | None = None,
):
    """构造 Telethon 消息替身（含净化引擎需要的字段）。"""
    from datetime import UTC, datetime
    from types import SimpleNamespace

    return SimpleNamespace(
        id=message_id,
        message=text,
        photo=object() if photo else None,
        video=object() if video else None,
        document=None,
        poll=None,
        action=None,
        pinned=pinned,
        grouped_id=grouped_id,
        post=False,
        sender_id=555,
        sender=SimpleNamespace(username="seller", bot=False),
        fwd_from=None,
        date=datetime(2026, 9, 25, 10, 0, tzinfo=UTC),
    )


class FakeDeliveryClient:
    """投递与历史同步用的 Telethon 替身。"""

    def __init__(self, history: list | None = None) -> None:
        self.history = list(history or [])
        self.forwarded: list[dict] = []
        self.sent: list[dict] = []
        self.fail_times = 0
        self.fail_with = "模拟发送失败"

    async def get_entity(self, identifier):
        return identifier

    async def get_messages(self, entity, *, ids=None, min_id=0, limit=None, reverse=False):
        if ids is not None:
            wanted = {int(item) for item in ids}
            found = [item for item in self.history if item.id in wanted]
            return found[0] if len(wanted) == 1 and found else found
        selected = [item for item in self.history if item.id > int(min_id)]
        selected.sort(key=lambda item: item.id)
        if limit:
            selected = selected[: int(limit)]
        return selected

    async def forward_messages(self, entity, ids, from_peer, drop_author=False):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise RuntimeError(self.fail_with)
        self.forwarded.append(
            {
                "target": entity,
                "ids": list(ids),
                "source": from_peer,
                "drop_author": drop_author,
            }
        )
        return SimpleNamespace(id=9000 + len(self.forwarded))

    async def send_message(self, entity, text, buttons=None):
        self.sent.append({"target": entity, "text": text, "buttons": buttons})
        return SimpleNamespace(id=8000 + len(self.sent))

    async def send_file(self, entity, file=None, caption=None, buttons=None):
        self.sent.append({"target": entity, "file": file, "caption": caption, "buttons": buttons})
        return SimpleNamespace(id=8500 + len(self.sent))

    async def disconnect(self) -> None:
        return None


@pytest.fixture
def fake_delivery_client() -> FakeDeliveryClient:
    """投递替身。"""
    return FakeDeliveryClient()


@pytest.fixture
async def db(project_root, valid_secret_key) -> AsyncIterator[object]:
    """已建表的临时数据库（服务层测试用）。"""
    from app.core.config import load_config
    from app.db.session import create_schema, dispose_database, init_database

    config = load_config(
        project_root=project_root,
        environ={
            "SECRET_KEY": valid_secret_key,
            "ADMIN_PASSWORD": "custom-pass1",
            "DATABASE_URL": database_url(project_root),
        },
    )
    await init_database(config)
    await create_schema()
    try:
        yield config
    finally:
        await dispose_database()


@pytest.fixture
def fake_account_client() -> FakeAccountClient:
    """账号客户端替身，测试可先配置 dialogs/entities/permissions。"""
    return FakeAccountClient()


@pytest.fixture
async def chat_client(api_config, fake_account_client) -> AsyncIterator[AsyncClient]:
    """带假账号客户端的客户端，并预置一个已登记的执行账号。"""
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
    await _create_admin(api_config, must_change_password=False, is_builtin=True)
    async with session_scope() as session:
        await tg_account_service.create_account(
            session,
            api_config,
            name="主号",
            phone="+8613800001111",
            api_id=123456,
            api_hash="abcdef0123456789abcdef0123456789",
            is_default=True,
        )

    async def factory(_config, **_kwargs):
        return fake_account_client

    app = create_app(api_config, account_client_factory=factory)
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        await dispose_database()


@pytest.fixture
async def bot_client(api_config) -> AsyncIterator[AsyncClient]:
    """带假 Telethon 工厂的客户端，用于控制 Bot 接口测试。"""
    from app.api.app import create_app
    from app.db.session import create_schema, dispose_database, init_database

    await init_database(api_config)
    await create_schema()
    await _create_admin(api_config, must_change_password=False, is_builtin=True)

    clients: list[FakeBotClient] = []

    async def factory(_config, token: str) -> FakeBotClient:
        client = FakeBotClient(token)
        clients.append(client)
        return client

    app = create_app(api_config, bot_client_factory=factory)
    app.state.fake_bot_clients = clients
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as http_client:
            yield http_client
    finally:
        await dispose_database()
