"""监听源接口测试：同步、穿梭、标签与移出。"""

from __future__ import annotations

from conftest import ADMIN_API_TOKEN, auth_header, login, make_entity
from telethon.tl import types


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def test_sync_migrates_upgraded_group_to_new_id(chat_client, fake_account_client) -> None:
    """基础群升级成超级群后，同步要把本地记录迁到新 id（引用与水位线保留）。"""
    from app.db.models import TenantChat
    from app.db.session import session_scope
    from app.services import chat_service

    old_group = types.Chat(
        id=6001,
        title="旧群",
        photo=None,
        participants_count=5,
        date=None,
        version=0,
    )
    fake_account_client.dialogs = [old_group]
    await chat_client.post("/api/sources/sync", headers=_headers())

    listing = (await chat_client.get("/api/sources/available", headers=_headers())).json()["items"]
    chat_id = next(item["id"] for item in listing if item["tg_id"] == 6001)
    async with session_scope() as session:
        chat = await session.get(TenantChat, chat_id)
        await chat_service.set_target(session, chat, role="lead")

    # SimpleNamespace 不可哈希，用一个小对象当 migrated_to（替身按对象查表）
    class _MigratedTo:
        def __init__(self, channel_id: int) -> None:
            self.channel_id = channel_id

    migrated_to = _MigratedTo(7001)
    old_group.migrated_to = migrated_to
    upgraded = make_entity(7001, "升级后的群", broadcast=False, megagroup=True)
    fake_account_client.dialogs = [old_group, upgraded]
    fake_account_client.entities = {migrated_to: upgraded}

    response = await chat_client.post("/api/sources/sync", headers=_headers())

    assert response.status_code == 200
    assert response.json()["migrated"] == 1
    async with session_scope() as session:
        moved = await session.get(TenantChat, chat_id)
    assert moved.tg_id == 7001
    assert moved.chat_type == "supergroup"
    assert moved.title == "升级后的群"
    assert moved.is_target is True


def _fill(client) -> None:
    """给替身客户端塞入两个群组与一个频道。"""
    material = make_entity(1001, "短剧素材频道", broadcast=True, username="duanju_material")
    sports = make_entity(1002, "体育交流群", participants_count=1200)
    client.dialogs = [material, sports]
    client.entities = {
        "duanju_material": material,
        1002: sports,
        "new_channel": make_entity(1003, "新频道", broadcast=True, username="new_channel"),
    }
    client.imported = make_entity(1004, "私有素材群", username=None)


def _basic_group(tg_id: int, title: str) -> types.Chat:
    """基础群（Telegram 早期小群）：没有广播/超级群标志。"""
    return types.Chat(
        id=tg_id,
        title=title,
        photo=None,
        participants_count=5,
        date=None,
        version=0,
    )


async def test_sync_dialogs_populates_pool(chat_client, fake_account_client) -> None:
    _fill(fake_account_client)

    response = await chat_client.post("/api/sources/sync", headers=_headers())

    body = response.json()
    assert response.status_code == 200
    assert body == {"account": "主号", "fetched": 2, "created": 2, "updated": 0, "migrated": 0}

    available = await chat_client.get("/api/sources/available", headers=_headers())
    titles = [item["title"] for item in available.json()["items"]]
    assert titles == ["体育交流群", "短剧素材频道"]

    second = await chat_client.post("/api/sources/sync", headers=_headers())
    assert second.json()["updated"] == 2
    assert second.json()["created"] == 0


async def test_sync_keeps_basic_group_and_drops_users(chat_client, fake_account_client) -> None:
    """基础群要同步进来，私聊用户要排除：只按 broadcast/megagroup 判断会漏群。"""
    channel = make_entity(2001, "素材频道", broadcast=True, username="mat2001")
    basic = _basic_group(2002, "飞机镜像")
    user = types.User(id=2003, first_name="某人")
    fake_account_client.dialogs = [channel, basic, user]

    response = await chat_client.post("/api/sources/sync", headers=_headers())

    assert response.status_code == 200
    assert response.json()["fetched"] == 2

    available = await chat_client.get("/api/sources/available", headers=_headers())
    items = {item["title"]: item for item in available.json()["items"]}
    assert set(items) == {"素材频道", "飞机镜像"}
    assert items["飞机镜像"]["chat_type"] == "group"
    assert items["飞机镜像"]["chat_type_label"] == "群组"


async def test_add_sources_from_pool_then_remove(chat_client, fake_account_client) -> None:
    _fill(fake_account_client)
    await chat_client.post("/api/sources/sync", headers=_headers())
    pool = (await chat_client.get("/api/sources/available", headers=_headers())).json()["items"]
    sports = next(item for item in pool if item["title"] == "体育交流群")

    added = await chat_client.post(
        "/api/sources",
        headers=_headers(),
        json={"chat_ids": [sports["id"]], "tags": ["体育类"]},
    )
    body = added.json()
    assert added.status_code == 201
    assert len(body["added"]) == 1
    assert body["added"][0]["is_source"] is True
    assert body["added"][0]["tags"] == ["体育类"]
    assert body["failures"] == []

    listed = (await chat_client.get("/api/sources", headers=_headers())).json()
    assert listed["total"] == 1
    assert listed["items"][0]["title"] == "体育交流群"

    # 移出后回到可选池
    removed = await chat_client.delete(f"/api/sources/{sports['id']}", headers=_headers())
    assert removed.status_code == 200
    assert (await chat_client.get("/api/sources", headers=_headers())).json()["total"] == 0
    assert (await chat_client.get("/api/sources/available", headers=_headers())).json()[
        "total"
    ] == 2


async def test_add_source_from_link_input(chat_client, fake_account_client) -> None:
    _fill(fake_account_client)

    response = await chat_client.post(
        "/api/sources",
        headers=_headers(),
        json={"inputs": ["t.me/new_channel", "@sports_group"]},
    )

    body = response.json()
    assert response.status_code == 201
    assert len(body["added"]) == 1
    assert body["added"][0]["tg_id"] == 1003
    # 第二个标识在替身里没有对应实体，应按项失败而不是整单失败
    assert len(body["failures"]) == 1
    assert body["failures"][0]["input"] == "@sports_group"


async def test_private_invite_requires_join_flag(chat_client, fake_account_client) -> None:
    _fill(fake_account_client)

    not_joined = await chat_client.post(
        "/api/sources",
        headers=_headers(),
        json={"inputs": ["t.me/+AbCdEfGh123456"]},
    )
    assert not_joined.json()["added"] == []
    assert "加入" in not_joined.json()["failures"][0]["reason"]

    joined = await chat_client.post(
        "/api/sources",
        headers=_headers(),
        json={"inputs": ["t.me/+AbCdEfGh123456"], "join": True},
    )
    body = joined.json()
    assert len(body["added"]) == 1
    assert body["added"][0]["tg_id"] == 1004
    assert body["added"][0]["is_private"] is True


async def test_update_source_and_batch_tags(chat_client, fake_account_client) -> None:
    _fill(fake_account_client)
    await chat_client.post("/api/sources/sync", headers=_headers())
    pool = (await chat_client.get("/api/sources/available", headers=_headers())).json()["items"]
    chat_id = pool[0]["id"]
    await chat_client.post("/api/sources", headers=_headers(), json={"chat_ids": [chat_id]})

    disabled = await chat_client.patch(
        f"/api/sources/{chat_id}",
        headers=_headers(),
        json={"enabled": False, "note": "暂停观察"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["source_enabled"] is False
    assert disabled.json()["note"] == "暂停观察"

    added = await chat_client.post(
        "/api/sources/tags/batch",
        headers=_headers(),
        json={"chat_ids": [chat_id], "tags": ["短剧类", "待观察"], "mode": "add"},
    )
    assert added.json()["updated"] == 1

    replaced = await chat_client.post(
        "/api/sources/tags/batch",
        headers=_headers(),
        json={"chat_ids": [chat_id], "tags": ["正式源"], "mode": "replace"},
    )
    assert replaced.json()["updated"] == 1

    detail = await chat_client.patch(
        f"/api/sources/{chat_id}",
        headers=_headers(),
        json={},
    )
    assert detail.json()["tags"] == ["正式源"]

    tags = await chat_client.get("/api/sources/tags", headers=_headers())
    assert "正式源" in tags.json()["items"]


async def test_source_endpoints_require_sub_admin(chat_client, api_config) -> None:
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
    token = (await login(chat_client, username="erge", password="Pass1234")).json()["token"]

    forbidden = await chat_client.get("/api/sources", headers=auth_header(token))
    assert forbidden.status_code == 403

    anonymous = await chat_client.get("/api/sources")
    assert anonymous.status_code == 401
