"""Telegram 实体识别与解析：基础群必须被同步，用户/机器人必须被排除。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telethon.tl import types

from app.core.errors import NotFoundError
from app.core.telegram_client import (
    check_can_post,
    fetch_dialog_entities,
    fetch_dialogs,
    is_group_or_channel,
    resolve_entity,
    warm_entity_cache,
)


def basic_group(tg_id: int, title: str, **kwargs: object) -> types.Chat:
    """Telegram 早期的小群：没有 broadcast / megagroup 标志。"""
    return types.Chat(
        id=tg_id,
        title=title,
        photo=None,
        participants_count=7,
        date=None,
        version=0,
        **kwargs,
    )


class DialogClient:
    """最小替身：只实现 fetch/resolve 用到的方法。"""

    def __init__(self, entities: list[object], *, get_entity_fails: bool = False) -> None:
        self.entities = entities
        self.get_entity_fails = get_entity_fails
        self.dialog_passes = 0

    async def iter_dialogs(self, limit: int | None = None):
        self.dialog_passes += 1
        items = self.entities[: limit or len(self.entities)]
        for entity in items:
            yield SimpleNamespace(entity=entity)

    async def get_entity(self, identifier: object):
        if self.get_entity_fails:
            raise ValueError(f"找不到实体：{identifier}")
        for entity in self.entities:
            if int(entity.id) == int(identifier):  # type: ignore[attr-defined]
                return entity
        raise ValueError(f"找不到实体：{identifier}")


def test_is_group_or_channel_covers_all_three_shapes() -> None:
    assert is_group_or_channel(SimpleNamespace(id=1, broadcast=True, megagroup=False)) is True
    assert is_group_or_channel(SimpleNamespace(id=2, broadcast=False, megagroup=True)) is True
    # 基础群：没有广播/超级群标志，正是以前被整批漏掉的那一类
    assert is_group_or_channel(basic_group(3, "飞机镜像")) is True


def test_is_group_or_channel_excludes_users_and_stale_groups() -> None:
    assert is_group_or_channel(SimpleNamespace(id=4, first_name="某人")) is False
    assert is_group_or_channel(types.User(id=5, first_name="某人")) is False
    # 已升级为超级群的旧群：内容在新群里，不要重复登记
    migrated = basic_group(6, "旧群", migrated_to=types.InputChannel(channel_id=99, access_hash=1))
    assert is_group_or_channel(migrated) is False
    assert is_group_or_channel(basic_group(7, "已注销", deactivated=True)) is False


async def test_fetch_dialogs_keeps_basic_group_and_drops_users() -> None:
    channel = SimpleNamespace(
        id=101,
        title="素材频道",
        username="mat",
        broadcast=True,
        megagroup=False,
        participants_count=9,
    )
    group = basic_group(102, "飞机镜像")
    user = types.User(id=103, first_name="某人")
    client = DialogClient([channel, group, user])

    entities = await fetch_dialog_entities(client)
    assert [entity.id for entity in entities] == [101, 102]

    profiles = await fetch_dialogs(client)
    assert [profile.tg_id for profile in profiles] == [101, 102]
    assert profiles[1].chat_type == "group"
    assert profiles[1].title == "飞机镜像"
    assert profiles[1].is_private is True


async def test_warm_entity_cache_counts_every_dialog() -> None:
    client = DialogClient([basic_group(201, "甲"), basic_group(202, "乙")])

    assert await warm_entity_cache(client) == 2
    assert client.dialog_passes == 1


async def test_resolve_entity_prefers_direct_lookup() -> None:
    group = basic_group(301, "飞机镜像")
    client = DialogClient([group])

    assert await resolve_entity(client, 301) is group
    assert client.dialog_passes == 0


async def test_resolve_entity_falls_back_to_dialog_scan() -> None:
    group = basic_group(401, "飞机镜像")
    client = DialogClient([group], get_entity_fails=True)

    assert await resolve_entity(client, 401) is group
    assert client.dialog_passes == 1


async def test_resolve_entity_raises_when_absent() -> None:
    client = DialogClient([basic_group(501, "甲")], get_entity_fails=True)

    with pytest.raises(NotFoundError):
        await resolve_entity(client, 599)


def permissions(**kwargs: object) -> SimpleNamespace:
    """Telethon ParticipantPermissions 的替身（只保留我们会读的字段）。"""
    base: dict[str, object] = {
        "is_creator": False,
        "is_admin": False,
        "is_banned": False,
        "has_left": False,
        "post_messages": False,
    }
    base.update(kwargs)
    return SimpleNamespace(**base)


class PermissionClient:
    """只实现 check_can_post 用到的部分。"""

    def __init__(self, entity: object, mine: object) -> None:
        self.entity = entity
        self.mine = mine
        self.user_args: list[object] = []

    async def get_entity(self, identifier: object):
        return self.entity

    async def get_me(self, input_peer: bool = False):
        return SimpleNamespace(user_id=9000001, _="InputPeerSelf")

    async def get_permissions(self, entity: object, user: object = None):
        self.user_args.append(user)
        if user is None:
            # Telethon 不带 user 时返回的是群默认限制：send_messages=False 表示“没限制”。
            # 旧实现把这个 False 当成“不能发帖”，于是普通成员全被误判。
            return SimpleNamespace(send_messages=False, post_messages=False)
        if isinstance(self.mine, Exception):
            raise self.mine
        return self.mine


def megagroup(tg_id: int, *, members_can_send: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        id=tg_id,
        broadcast=False,
        megagroup=True,
        default_banned_rights=SimpleNamespace(send_messages=not members_can_send),
    )


async def test_check_can_post_for_plain_member_of_group() -> None:
    """普通群成员能发言——这正是原来被误判成「无发帖权限」的情形。"""
    client = PermissionClient(megagroup(6001), permissions())

    assert await check_can_post(client, 6001) is True
    # 必须带上当前用户，否则拿到的只是群默认限制
    assert client.user_args and client.user_args[0] is not None


async def test_check_can_post_for_group_with_members_muted() -> None:
    client = PermissionClient(megagroup(6002, members_can_send=False), permissions())

    assert await check_can_post(client, 6002) is False


async def test_check_can_post_for_channel_subscriber_and_admin() -> None:
    channel = SimpleNamespace(id=6003, broadcast=True, megagroup=False)

    subscriber = PermissionClient(channel, permissions())
    assert await check_can_post(subscriber, 6003) is False

    admin = PermissionClient(channel, permissions(is_admin=True, post_messages=True))
    assert await check_can_post(admin, 6003) is True

    admin_without_post = PermissionClient(channel, permissions(is_admin=True))
    assert await check_can_post(admin_without_post, 6003) is False


async def test_check_can_post_for_basic_group_creator_and_banned() -> None:
    group = SimpleNamespace(id=6004, megagroup=False)

    creator = PermissionClient(group, permissions(is_creator=True, is_admin=True))
    assert await check_can_post(creator, 6004) is True

    banned = PermissionClient(group, permissions(is_banned=True))
    assert await check_can_post(banned, 6004) is False


async def test_check_can_post_returns_none_when_unknown() -> None:
    client = PermissionClient(megagroup(6005), RuntimeError("取权限失败"))

    assert await check_can_post(client, 6005) is None
