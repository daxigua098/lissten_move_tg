"""pytest 公共夹具。"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


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
