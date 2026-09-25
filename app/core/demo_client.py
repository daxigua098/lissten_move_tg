"""本地演练模式用的模拟 Telegram 客户端。

没有真实账号时，用它在本地把「同步群组 → 建源/接收组 → 建线路 → 补齐历史 →
投递 → 查看任务」整条链路跑通。**只用于本地演练，绝不能在生产环境启用**：
它不连接 Telegram，所有发送动作只写日志。
"""

from __future__ import annotations

import hashlib
import itertools
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from loguru import logger

DEMO_BASE_TIME = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)
DEMO_MESSAGE_SPAN = 12


def _entity(
    tg_id: int,
    title: str,
    *,
    username: str | None = None,
    broadcast: bool = False,
    megagroup: bool = True,
    participants: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=tg_id,
        title=title,
        username=username,
        broadcast=broadcast,
        megagroup=megagroup,
        participants_count=participants,
    )


DEMO_DIALOGS = [
    _entity(910001, "演示素材频道", username="demo_material", broadcast=True),
    _entity(910002, "演示福利频道", username="demo_welfare", broadcast=True),
    _entity(910003, "演示交流群", participants=1280),
    _entity(910004, "演示线索群", participants=36),
]


class DemoAccountClient:
    """执行账号替身：资料查询、历史消息、转发与发送都只记录日志。"""

    def __init__(self, *, log_actions: bool = True) -> None:
        self.log_actions = log_actions
        self.entities: dict[int, Any] = {item.id: item for item in DEMO_DIALOGS}
        self.usernames: dict[str, Any] = {
            item.username: item for item in DEMO_DIALOGS if item.username
        }
        self.sent_counter = itertools.count(100000)
        self.actions: list[dict[str, Any]] = []
        self.handlers: list[tuple[Any, Any]] = []

    # ---- 连接与账号 ----
    async def is_user_authorized(self) -> bool:
        return True

    async def get_me(self, input_peer: bool = False) -> Any:
        if input_peer:
            return SimpleNamespace(user_id=9000001, _="InputPeerSelf")
        return SimpleNamespace(
            id=9000001,
            username="demo_account",
            first_name="演示账号",
            last_name=None,
            phone=None,
            bot=False,
        )

    async def connect(self) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    def add_event_handler(self, callback: Any, event: Any = None) -> None:
        self.handlers.append((callback, event))

    # ---- 实体查询 ----
    async def iter_dialogs(self, limit: int | None = None):
        for item in list(self.entities.values())[: limit or len(self.entities)]:
            yield SimpleNamespace(entity=item)

    async def get_entity(self, identifier: Any) -> Any:
        key = identifier
        if isinstance(key, str):
            username = key.lstrip("@").lower()
            if username in self.usernames:
                return self.usernames[username]
            return self._synthetic(username, username)
        tg_id = int(key)
        if tg_id in self.entities:
            return self.entities[tg_id]
        return self._synthetic(f"chat{tg_id}", f"演示聊天 {tg_id}", tg_id=tg_id)

    async def get_permissions(self, entity: Any, user: Any = None) -> Any:
        """对齐 Telethon 语义：不带 user 拿的是群默认限制，带 user 才是"我的权限"。"""
        if user is None:
            return SimpleNamespace(send_messages=False, post_messages=False)
        return SimpleNamespace(
            is_creator=True,
            is_admin=True,
            is_banned=False,
            has_left=False,
            post_messages=True,
        )

    async def __call__(self, request: Any) -> Any:
        name = type(request).__name__
        if name == "ImportChatInviteRequest":
            target = self._invite_entity(f"invite_{getattr(request, 'hash', 'demo')}")
            return SimpleNamespace(chats=[target])
        if name == "CheckChatInviteRequest":
            target = self._invite_entity(f"invite_{getattr(request, 'hash', 'demo')}")
            return SimpleNamespace(chat=target, title=target.title, participants_count=12)
        raise AssertionError(f"演示客户端未实现的请求：{name}")

    # ---- 消息 ----
    async def get_messages(
        self,
        entity: Any,
        *,
        ids: Any = None,
        min_id: int = 0,
        limit: int | None = None,
        reverse: bool = False,
    ) -> Any:
        tg_id = int(getattr(entity, "id", 0) or 0)
        if ids is not None:
            wanted = [int(item) for item in (ids if isinstance(ids, (list, tuple)) else [ids])]
            return [self._message(tg_id, item) for item in wanted]

        latest = DEMO_MESSAGE_SPAN
        start = max(int(min_id) + 1, 1)
        if start > latest:
            return []  # 已补到最新：不再返回新消息，用于验证水位线去重
        sequence = list(range(start, latest + 1))
        if limit:
            sequence = sequence[: int(limit)]
        return [self._message(tg_id, item) for item in sequence]

    def _message(self, chat_id: int, message_id: int) -> Any:
        # 第 3、7 条带图；第 5 条带对方广告与推广词，用来验证净化
        has_photo = message_id in {3, 7, 11}
        if message_id == 5:
            # 广告单独占一行：净化后整行删除，正文保留
            text = "演示广告帖 精华素材\n加我微信领取福利 https://ad.example.com 联系 @demo_seller"
        elif message_id == 9:
            text = "纯文本消息，不应被媒体模式搬运"
        else:
            text = f"演示内容 #{message_id} 正常素材"
        return SimpleNamespace(
            id=message_id,
            message=text,
            photo=object() if has_photo else None,
            video=None,
            document=None,
            poll=None,
            action=None,
            pinned=message_id == 1,
            grouped_id=None,
            post=False,
            sender_id=800001,
            sender=SimpleNamespace(username="demo_sender", bot=False),
            fwd_from=None,
            date=DEMO_BASE_TIME + timedelta(minutes=message_id),
        )

    # ---- 发送 ----
    async def forward_messages(
        self,
        entity: Any,
        ids: Any,
        from_peer: Any,
        drop_author: bool = False,
    ) -> Any:
        message_ids = list(ids) if isinstance(ids, (list, tuple)) else [ids]
        record = {
            "action": "forward",
            "target": getattr(entity, "title", entity),
            "source": getattr(from_peer, "title", from_peer),
            "message_ids": message_ids,
            "drop_author": drop_author,
        }
        self.actions.append(record)
        self._log(record)
        return SimpleNamespace(id=next(self.sent_counter))

    async def send_message(self, entity: Any, text: str, buttons: Any = None) -> Any:
        record = {
            "action": "ad_text",
            "target": getattr(entity, "title", entity),
            "text": text,
            "buttons": buttons,
        }
        self.actions.append(record)
        self._log(record)
        return SimpleNamespace(id=next(self.sent_counter))

    async def send_file(
        self,
        entity: Any,
        file: Any = None,
        caption: str | None = None,
        buttons: Any = None,
    ) -> Any:
        is_repost = hasattr(file, "id") and not isinstance(file, (str, bytes))
        record = {
            "action": "repost" if is_repost else "ad_file",
            "target": getattr(entity, "title", entity),
            "caption": caption,
            "buttons": buttons,
        }
        self.actions.append(record)
        self._log(record)
        return SimpleNamespace(id=next(self.sent_counter))

    # ---- 工具 ----
    def _synthetic(
        self,
        name: str,
        title: str,
        *,
        tg_id: int | None = None,
        private: bool = False,
    ) -> Any:
        if tg_id is None:
            digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
            tg_id = 920000 + int(digest, 16) % 10000
        username = None if private else (name if name.isascii() else None)
        entity = _entity(tg_id, title, username=username)
        self.entities[tg_id] = entity
        if entity.username:
            self.usernames[entity.username] = entity
        return entity

    def _invite_entity(self, name: str) -> Any:
        """邀请链接解析出的私有群：没有公开用户名，因此属于私有。"""
        return self._synthetic(name, "演示私有群", private=True)

    def _log(self, record: dict[str, Any]) -> None:
        if not self.log_actions:
            return
        if record["action"] == "forward":
            logger.info(
                "[演练] 转发 {} 条消息：{} → {}{}",
                len(record["message_ids"]),
                record["source"],
                record["target"],
                "（去来源标记）" if record["drop_author"] else "",
            )
        else:
            content = record.get("text") or record.get("caption") or ""
            logger.info(
                "[演练] 发送广告到 {}：{}",
                record["target"],
                content,
            )


class DemoBotClient:
    """控制 Bot 替身：只做 Token 校验。"""

    def __init__(self, token: str) -> None:
        self.token = token

    async def get_me(self) -> Any:
        return SimpleNamespace(
            id=9300001,
            username="demo_control_bot",
            first_name="演示机器人",
            last_name=None,
            phone=None,
            bot=True,
        )

    async def disconnect(self) -> None:
        return None


def demo_account_client_factory(config: Any = None) -> Any:
    """返回一个可被 await 的工厂（签名与真实工厂一致）。"""
    client = DemoAccountClient()

    async def factory(*_args: Any, **_kwargs: Any) -> DemoAccountClient:
        return client

    return factory


def demo_bot_client_factory(config: Any = None) -> Any:
    """Bot 校验用的演示工厂。"""

    async def factory(_config: Any, token: str) -> DemoBotClient:
        return DemoBotClient(token)

    return factory
