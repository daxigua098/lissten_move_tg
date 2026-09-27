"""运行时：实时监听源消息 + 串行投递队列。

一个进程内只跑一个实例（靠 RuntimeLock 兜底），所有投递严格串行，
与《需求说明书》的"单账号串行、不并发发送"一致。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import select

from app.core.bot_api import bot_api_chat_id
from app.core.config import AppConfig
from app.core.content_cleaner import (
    KIND_SERVICE,
    KIND_TEXT,
    CleanRules,
    MessageView,
    clean_text,
    filter_reason,
    resolve_caption,
)
from app.core.heartbeat import heartbeat_age_seconds, is_running, read_status, write_status
from app.core.keyword_matcher import is_excluded, match_text
from app.core.lead_extractor import extract_contacts, render_lead_card, sender_info
from app.core.route_config import load_a_config, load_b_config
from app.core.runtime_control import is_paused, is_stop_requested, set_paused, set_stop_requested
from app.core.runtime_lock import RuntimeLock, RuntimeLockError
from app.core.telegram_client import message_view_from_telethon, resolve_entity
from app.db.models import (
    ACCOUNT_ACTIVE,
    BUSINESS_CARRY,
    BUSINESS_MONITOR,
    DISCOVER_LINK,
    SENDER_MODE_BOT,
    OutreachContact,
    OutreachSettings,
    Route,
    TenantChat,
)
from app.db.session import session_scope
from app.services import (
    bot_service,
    delivery_service,
    directory_sync_service,
    history_service,
    hot_keyword_service,
    keyword_service,
    lead_service,
    outreach_handoff_service,
    outreach_metrics_service,
    outreach_queue_service,
    outreach_reply_service,
    outreach_sender_service,
    outreach_settings_service,
    resource_join_service,
    resource_probe_service,
    resource_service,
    tenant_runtime_service,
    tg_account_service,
)

# 资源发现的节奏：加群 / 搜索属于会被风控的动作，别跟着投递循环每 2 秒跑一次
RESOURCE_TICK_SECONDS = 30.0

# 承接 Bot 的 /start 轮询间隔
HANDOFF_TICK_SECONDS = 5.0

# 相册收齐窗口：同一条帖子的多张图/视频是逐条到达的，等这一批安静下来再入队
ALBUM_WINDOW_SECONDS = 2.5


@dataclass
class _AlbumBuffer:
    """一条正在收集中的相册帖子。"""

    views: dict[int, MessageView] = field(default_factory=dict)
    routes: list[Any] = field(default_factory=list)
    # 收齐截止时间（事件循环时钟）。每来一条新成员就往后推。
    deadline: float = 0.0
    task: Any = None


class RuntimeService:
    """A 线搬运运行时。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        client_factory: Any = None,
        bot_api_factory: Any = None,
        poll_interval: float = 2.0,
        heartbeat_seconds: int | None = None,
    ) -> None:
        self.config = config
        self.client_factory = client_factory
        # 机器人客户端工厂（测试可注入替身；默认按 Token 真连 Bot API）
        self.bot_api_factory = bot_api_factory
        self.poll_interval = poll_interval
        self.heartbeat_seconds = heartbeat_seconds or config.runtime.heartbeat_seconds
        self._lock = RuntimeLock(config.path(config.runtime.lock_file))
        self._stop = False
        # 管理员 ID 缓存：跳过管理员这条规则不该每条消息都去查一次群成员
        self._admin_cache: dict[int, set[int]] = {}
        self._last_purge_at = float("-inf")
        # 资源发现（加群 / 搜索 / 探测）上次执行的时间
        self._last_resource_tick = float("-inf")
        # 相册缓冲：grouped_id → 还没入队的帖子
        self._album_buffer: dict[int, _AlbumBuffer] = {}
        # 本次启动注册了哪些线路（写进心跳，界面上用来发现"改了但没重启"）
        self._registered: dict[str, Any] = {}
        # P1-06：执行连接按租户各一条（tenant_id → client），用到哪个租户才建哪个
        self._clients: dict[int, Any] = {}
        # 测试 / 演练注入的连接：给了就顶替所有租户的连接（生产为 None）
        self._fallback_client: Any = None
        # 机器人（Bot API）客户端缓存：sender_mode=bot 的线路用它们发言
        self._bot_apis: dict[int, Any] = {}
        # 目录站抓取器（不需要 Telegram 账号，用到时才建）
        self._directory_fetcher: Any = None
        # 发信息账号连接（account_id → client）：冷触达自己一组，与监听账号分开
        self._outreach_clients: dict[int, Any] = {}
        self._outreach_reply_handlers: set[int] = set()
        # 承接 Bot 的 getUpdates 游标（bot_id → offset）
        self._handoff_offsets: dict[int, int] = {}
        self._last_handoff_tick = float("-inf")

    @property
    def control_path(self):
        """控制文件路径。"""
        return self.config.path(self.config.runtime.control_file)

    @property
    def status_path(self):
        """状态文件路径。"""
        return self.config.path(self.config.runtime.status_file)

    @property
    def outreach_control_path(self):
        """冷触达的独立控制文件：熔断 / 暂停只影响冷触达。"""
        return self.config.path(self.config.runtime.outreach_control_file)

    async def run(self, *, client: Any = None) -> int:
        """启动运行：获取锁、按租户注册监听、进入心跳与队列循环。

        ``client`` 只在测试与演练里注入：给了它就顶替所有租户的执行连接。
        生产路径（``client=None``）按线路所属租户各开一条连接（P1-06），
        某个租户没有可用账号时只跳过它，不影响其他租户。
        """
        try:
            self._lock.acquire()
        except RuntimeLockError as exc:
            logger.error("启动失败：{}", exc)
            return 1

        self._fallback_client = client
        try:
            handlers = await self._register_handlers()
            self._registered = handlers
            logger.info(
                "实时监听已启动：{} 个源（A 线 {} 条 / B 线 {} 条，覆盖 {} 个租户）",
                handlers["sources"],
                handlers["carry"],
                handlers["monitor"],
                len(handlers["tenants"]),
            )
            await self._publish(status="running", extra={"routes": handlers})
            # 上次异常退出可能留下「处理中」的任务，先放回队列再开工
            async with session_scope() as session:
                requeued = await delivery_service.requeue_stale_jobs(session)
            if requeued:
                logger.warning("上次中断留下 {} 条处理中任务，已重新入队", requeued)
            # 心跳独立跑：投递循环里在下载大文件时，界面也不会显示成掉线
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            outreach_task = asyncio.create_task(self._outreach_loop())
            try:
                await self._loop()
            finally:
                # 停止前把还没入队的相册冲出去，别丢掉刚发的帖子
                with contextlib.suppress(Exception):
                    await self._flush_pending_albums()
                outreach_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await outreach_task
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
        finally:
            await self._close_outreach_clients()
            await self._close_clients()
            await self._close_bot_apis()
            self._lock.release()
            await self._publish(status="stopped", extra={})
        return 0

    async def _outreach_loop(self) -> None:
        """冷触达循环：独立节奏，失败不影响搬运 / 监听。"""
        while not self._stop:
            if is_paused(self.outreach_control_path):
                await asyncio.sleep(self.poll_interval)
                continue
            try:
                await self._outreach_tick()
            except Exception as exc:  # noqa: BLE001 - 单轮失败不能拖垮运行时
                logger.warning("冷触达本轮执行失败：{}", exc)
            await asyncio.sleep(max(2.0, self.poll_interval * 2))

    async def _outreach_tick(self) -> int:
        """处理一轮冷触达任务：闸门由发送服务负责，这里只做串行调度。"""
        async with session_scope() as session:
            tenant_ids = await outreach_queue_service.pending_tenants(session)
        sent = 0
        for tenant_id in tenant_ids:
            async with session_scope() as session:
                settings = await outreach_settings_service.read_settings(session, tenant_id)
                if settings.get("kill_switch"):
                    continue
                task = await outreach_queue_service.next_ready_task(session, tenant_id)
                if task is None:
                    continue
                account = await outreach_sender_service.pick_account(session, tenant_id)
                if account is None:
                    continue
                contact = await session.get(OutreachContact, task.contact_id)
                if contact is None:
                    continue
                client = await self._outreach_client(session, account)
                result = await outreach_sender_service.send_task(
                    session,
                    client=client,
                    account=account,
                    task=task,
                    contact=contact,
                )
                if result.get("status") == "SENT":
                    sent += 1
                    await outreach_metrics_service.refresh(session, account)
            await asyncio.sleep(1.0)

        now = asyncio.get_running_loop().time()
        if now - self._last_handoff_tick >= HANDOFF_TICK_SECONDS:
            self._last_handoff_tick = now
            await self._poll_handoff_updates()
        return sent

    async def _poll_handoff_updates(self) -> None:
        """轮询承接 Bot 的 /start，把会话归属从账号转给 Bot。"""
        async with session_scope() as session:
            rows = list(
                await session.scalars(
                    select(OutreachSettings).where(
                        OutreachSettings.handoff_bot_enabled.is_(True),
                        OutreachSettings.handoff_bot_id.is_not(None),
                    )
                )
            )
            targets = [(row.tenant_id, int(row.handoff_bot_id or 0)) for row in rows]

        for _tenant_id, bot_id in targets:
            if not bot_id:
                continue
            try:
                api = await self._bot_api(bot_id)
                updates = await api.get_updates(offset=self._handoff_offsets.get(bot_id, 0))
            except Exception as exc:  # noqa: BLE001 - 拉取失败只跳过这一轮
                logger.debug("拉取承接 Bot 更新失败（bot {}）：{}", bot_id, exc)
                continue
            for update in updates:
                self._handoff_offsets[bot_id] = int(update.get("update_id", 0)) + 1
                message = update.get("message") or {}
                text = str(message.get("text") or "").strip()
                if not text.startswith("/start"):
                    continue
                parts = text.split(maxsplit=1)
                if len(parts) < 2:
                    continue
                from_user = (message.get("from") or {}).get("id")
                async with session_scope() as session:
                    contact = await outreach_handoff_service.consume(
                        session,
                        parts[1].strip(),
                        tg_user_id=int(from_user) if from_user else None,
                    )
                if contact is not None:
                    logger.info("会话已转交给 Bot：联系人 #{}", contact.id)

    async def _outreach_client(self, session: Any, account: Any) -> Any:
        """取发信息账号的连接；第一次用到时才建，并挂上回复监听。"""
        if self._fallback_client is not None:
            return self._fallback_client
        cached = self._outreach_clients.get(account.id)
        if cached is not None:
            return cached
        from app.core.telegram_client import connect_user_client, session_file_path

        phone, api_id, api_hash = tg_account_service.decrypt_credentials(self.config, account)
        session_path = session_file_path(self.config, account.session_name)
        if self.config.app.demo_mode:
            from app.core.demo_client import DemoAccountClient

            client = DemoAccountClient()
        elif self.client_factory is not None:
            client = await self.client_factory(
                self.config,
                api_id=api_id,
                api_hash=api_hash,
                session_path=session_path,
            )
        else:
            client = await connect_user_client(
                self.config,
                api_id=api_id,
                api_hash=api_hash,
                session_path=session_path,
            )
        self._register_outreach_handler(client, account)
        self._outreach_clients[account.id] = client
        return client

    def _register_outreach_handler(self, client: Any, account: Any) -> None:
        """听这个发信息账号收到的私聊回复（只认私聊）。"""
        if account.id in self._outreach_reply_handlers:
            return
        try:
            from telethon import events
        except ImportError:  # pragma: no cover - 演练 / 测试环境没有 telethon
            return

        async def reply_handler(event: Any) -> None:  # noqa: ANN001
            await self._on_outreach_reply(account, event, client)

        client.add_event_handler(
            reply_handler,
            events.NewMessage(incoming=True, func=lambda event: event.is_private),
        )
        self._outreach_reply_handlers.add(account.id)

    async def _on_outreach_reply(self, account: Any, event: Any, client: Any) -> None:
        """入站回复：归属锁定 / 拒绝拉黑；按 B 模式决定要不要自动回一句。"""
        message = getattr(event, "message", None)
        sender_id = getattr(event, "sender_id", None) or getattr(message, "sender_id", None)
        if not sender_id:
            return
        text = getattr(message, "message", None)
        async with session_scope() as session:
            contact = await outreach_sender_service.handle_incoming(
                session,
                tenant_id=account.tenant_id,
                account_id=account.id,
                sender_tg_id=int(sender_id),
                text=text,
                tg_message_id=int(getattr(message, "id", 0) or 0),
            )
        if contact is None:
            return
        logger.info("冷触达收到回复：联系人 #{}（账号 {}）", contact.id, account.name)
        async with session_scope() as session:
            fresh = await session.get(OutreachContact, contact.id)
            if fresh is None:
                return
            try:
                await outreach_reply_service.auto_reply(
                    session,
                    client=client,
                    account=account,
                    contact=fresh,
                    incoming_text=text,
                )
            except Exception as exc:  # noqa: BLE001 - 自动回复失败不能影响监听
                logger.warning("冷触达自动回复失败：{}", exc)

    async def _close_outreach_clients(self) -> None:
        """断开冷触达连接（注入的连接由注入方负责）。"""
        if self._fallback_client is not None:
            return
        for account_id, client in list(self._outreach_clients.items()):
            with contextlib.suppress(Exception):
                await client.disconnect()
            self._outreach_clients.pop(account_id, None)
        self._outreach_reply_handlers.clear()

    async def _heartbeat_loop(self) -> None:
        """只负责定期写心跳，与投递循环解耦。"""
        while True:
            await asyncio.sleep(max(2, self.heartbeat_seconds // 2))
            with contextlib.suppress(Exception):
                await self._publish(status="running", extra={})

    async def _client_for(self, tenant_id: int, session: Any = None) -> Any:
        """取这个租户的执行连接（P1-06）。

        顺序：注入的连接 > 已有缓存 > 现开一条。懒建立，所以某个租户没有
        线路（或者没配账号）时根本不会去连它。
        """
        if self._fallback_client is not None:
            return self._fallback_client
        cached = self._clients.get(tenant_id)
        if cached is not None:
            return cached
        if session is None:
            async with session_scope() as own_session:
                client = await self._open_client(tenant_id, own_session)
        else:
            client = await self._open_client(tenant_id, session)
        self._clients[tenant_id] = client
        return client

    async def _close_clients(self) -> None:
        """断开本进程自己开的执行连接（注入的连接由注入方负责）。"""
        for tenant_id, client in list(self._clients.items()):
            with contextlib.suppress(Exception):
                await client.disconnect()
            self._clients.pop(tenant_id, None)

    async def _account_for(self, session: Any, tenant_id: int) -> Any:
        """该租户的默认执行账号；没有就报错（带上租户号，便于排查）。"""
        account = await tg_account_service.get_default_account(session, tenant_id=tenant_id)
        if account is None:
            raise RuntimeError(
                f"租户 #{tenant_id} 没有可用的执行账号，请先在「执行账号池」登记并登录"
            )
        return account

    async def _open_client(self, tenant_id: int, session: Any) -> Any:
        """给一个租户开一条执行连接（账号取该租户自己的默认账号）。"""
        from app.core.demo_client import DemoAccountClient

        account = await self._account_for(session, tenant_id)
        if self.config.app.demo_mode:
            logger.warning("本地演练模式：使用模拟客户端，不会连接 Telegram")
            return DemoAccountClient()
        if account.status != ACCOUNT_ACTIVE:
            raise RuntimeError(
                f"执行账号「{account.name}」状态为 {account.status}，"
                "请先执行 python main.py account-login 完成登录"
            )
        phone, api_id, api_hash = tg_account_service.decrypt_credentials(
            self.config,
            account,
        )
        from app.core.telegram_client import session_file_path

        session_path = session_file_path(self.config, account.session_name)

        factory = self.client_factory
        if factory is not None:
            return await factory(
                self.config,
                api_id=api_id,
                api_hash=api_hash,
                session_path=session_path,
            )

        from app.core.telegram_client import connect_user_client

        return await connect_user_client(
            self.config,
            api_id=api_id,
            api_hash=api_hash,
            session_path=session_path,
        )

    async def _bot_api(self, bot_id: int | None) -> Any:
        """取（并缓存）用于发言的机器人客户端（Bot API / HTTP）。

        机器人不需要在源群：内容由执行账号下载、机器人负责上传，
        所以发言风控落在机器人身上。Bot API 只认 chat_id，不用解析实体。
        """
        if not bot_id:
            raise RuntimeError("线路选了「用机器人发送」，但没有指定机器人")
        cached = self._bot_apis.get(bot_id)
        if cached is not None:
            return cached
        async with session_scope() as session:
            bot = await bot_service.get_bot(session, bot_id)
            if bot is None or not bot.enabled:
                raise RuntimeError("指定的机器人不存在或已停用")
            token = bot_service.decrypt_token(self.config, bot)
            bot_name = bot.name
        factory = self.bot_api_factory
        if factory is not None:
            api = await factory(self.config, token)
        else:
            from app.core.bot_api import BotApiClient

            api = BotApiClient(token)
        self._bot_apis[bot_id] = api
        logger.info("已连接发送机器人「{}」（线路用它发言）", bot_name)
        return api

    async def _close_bot_apis(self) -> None:
        for bot_id, api in list(self._bot_apis.items()):
            with contextlib.suppress(Exception):
                await api.close()
            self._bot_apis.pop(bot_id, None)
        if self._directory_fetcher is not None:
            with contextlib.suppress(Exception):
                await self._directory_fetcher.aclose()
            self._directory_fetcher = None

    def _directory_fetcher_for_tick(self) -> Any:
        """目录抓取器（懒建；目录同步不需要执行账号）。"""
        if self._directory_fetcher is None:
            from app.core.directory_client import DirectoryFetcher

            self._directory_fetcher = DirectoryFetcher()
        return self._directory_fetcher

    async def _register_handlers(self) -> dict[str, Any]:
        """给每条启用的线路源注册新消息监听（A 线搬运 + B 线监听共用一次注册）。

        P1-06：先按线路所属租户分组，再用**该租户自己的连接**去监听它的源——
        会员的线路只会用会员自己的账号看群。某个租户没有可用账号时只跳过它，
        其他租户照常起（单租户异常不拖垮其他租户）。
        """
        async with session_scope() as session:
            records = list(await session.scalars(select(Route).where(Route.enabled.is_(True))))
            by_tenant: dict[int, list[Route]] = {}
            for route in records:
                by_tenant.setdefault(route.tenant_id, []).append(route)
            chat_tg_ids: dict[int, int] = {}
            for route in records:
                if route.source_chat_id in chat_tg_ids:
                    continue
                chat = await session.get(TenantChat, route.source_chat_id)
                if chat is not None and chat.tg_id:
                    chat_tg_ids[route.source_chat_id] = int(chat.tg_id)

        counts: dict[str, Any] = {
            "sources": 0,
            "carry": 0,
            "monitor": 0,
            "ids": [],
            "tenants": [],
        }
        for tenant_id, routes in by_tenant.items():
            try:
                client = await self._client_for(tenant_id)
            except Exception as exc:  # noqa: BLE001 - 单个租户没号不该拖垮其他租户
                logger.warning(
                    "租户 #{} 的执行账号不可用，跳过它的 {} 条线路：{}",
                    tenant_id,
                    len(routes),
                    exc,
                )
                continue
            counts["tenants"].append(tenant_id)
            await self._register_tenant_handlers(client, routes, chat_tg_ids, counts)
        return counts

    async def _register_tenant_handlers(
        self,
        client: Any,
        routes: list[Route],
        chat_tg_ids: dict[int, int],
        counts: dict[str, Any],
    ) -> None:
        """注册一个租户的监听源（用这个租户自己的连接）。"""
        from telethon import events

        sources: dict[int, dict[str, list[Route]]] = {}
        for route in routes:
            bucket = sources.setdefault(route.source_chat_id, {"A": [], "B": []})
            bucket.setdefault(route.business_type, []).append(route)

        for chat_id, buckets in sources.items():
            tg_id = chat_tg_ids.get(chat_id)
            if not tg_id:
                continue
            try:
                entity = await resolve_entity(client, tg_id)
            except Exception as exc:  # noqa: BLE001 - 单个源解析失败跳过
                logger.warning("监听源 {} 解析失败：{}", chat_id, exc)
                continue

            carry_routes = buckets.get(BUSINESS_CARRY) or []
            monitor_routes = buckets.get(BUSINESS_MONITOR) or []

            if carry_routes:

                async def carry_handler(event: Any, routes=carry_routes) -> None:  # noqa: ANN001
                    await self._on_new_message(event, routes)

                client.add_event_handler(carry_handler, events.NewMessage(chats=entity))
                counts["carry"] += len(carry_routes)
                counts["ids"].extend(route.id for route in carry_routes)

            if monitor_routes:

                async def monitor_handler(event: Any, routes=monitor_routes) -> None:  # noqa: ANN001
                    await self._on_monitor_message(client, event, routes)

                client.add_event_handler(monitor_handler, events.NewMessage(chats=entity))
                counts["monitor"] += len(monitor_routes)
                counts["ids"].extend(route.id for route in monitor_routes)

            counts["sources"] += 1

    async def _on_new_message(self, event: Any, routes: list[Route]) -> None:
        """实时消息入队；相册先攒一下，收齐后当成一条帖子处理。"""
        view = message_view_from_telethon(event.message)
        if view.grouped_id is not None:
            self._buffer_album_item(view=view, routes=routes)
            return
        async with session_scope() as session:
            await self._enqueue_carry_group(session, routes=routes, views=[view])

    def _buffer_album_item(self, *, view: MessageView, routes: list[Route]) -> None:
        """把相册里的一条媒体攒进缓冲区，等这一批发完再一起入队。

        实时接收时，一条帖子的多张图/视频是**一条一条**到达的，不能像单条消息
        那样立刻入队——否则就会逐张发出去（这正是要修的问题）。每来一条就重置
        计时，安静 ``ALBUM_WINDOW_SECONDS`` 秒后合并成一个任务。
        """
        grouped_id = view.grouped_id
        if grouped_id is None:  # pragma: no cover - 调用方已判空
            return
        entry = self._album_buffer.get(grouped_id)
        if entry is None:
            entry = _AlbumBuffer()
            self._album_buffer[grouped_id] = entry
        entry.views[view.message_id] = view
        for route in routes:
            if all(item.id != route.id for item in entry.routes):
                entry.routes.append(route)
        # 只是把截止时间往后推，不重建任务：避免频繁 cancel 留下悬空协程
        entry.deadline = asyncio.get_running_loop().time() + ALBUM_WINDOW_SECONDS
        if entry.task is None or entry.task.done():
            entry.task = asyncio.create_task(self._flush_album_later(grouped_id))

    async def _flush_album_later(self, grouped_id: int) -> None:
        """窗口期内没有新成员到达时，把相册入队。"""
        loop = asyncio.get_running_loop()
        while True:
            entry = self._album_buffer.get(grouped_id)
            if entry is None:
                return  # 已经被别处冲刷掉了
            remaining = entry.deadline - loop.time()
            if remaining <= 0:
                break
            await asyncio.sleep(remaining)
        try:
            await self._flush_album(grouped_id)
        except Exception as exc:  # noqa: BLE001 - 单条帖子失败不影响监听
            logger.warning("相册 {} 入队失败：{}", grouped_id, exc)

    async def _flush_album(self, grouped_id: int) -> int:
        """把收齐的相册合并成一个投递任务。"""
        entry = self._album_buffer.pop(grouped_id, None)
        if entry is None:
            return 0
        task = entry.task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        views = [entry.views[key] for key in sorted(entry.views)]
        if not views:
            return 0
        async with session_scope() as session:
            return await self._enqueue_carry_group(
                session,
                routes=entry.routes,
                views=views,
                media_group_id=grouped_id,
            )

    async def _flush_pending_albums(self) -> None:
        """停止运行时前，把缓冲区里还没入队的相册全部冲刷出去。"""
        for grouped_id in list(self._album_buffer):
            try:
                await self._flush_album(grouped_id)
            except Exception as exc:  # noqa: BLE001 - 退出流程不因单条失败中断
                logger.warning("退出前冲刷相册 {} 失败：{}", grouped_id, exc)

    async def _enqueue_carry_group(
        self,
        session: Any,
        *,
        routes: list[Route],
        views: list[MessageView],
        media_group_id: int | None = None,
    ) -> int:
        """把「一条帖子」入队到各条 A 线，返回入队的线路数。

        ``views`` 是这条帖子包含的全部媒体：单条消息传一个，相册传整组。
        相册只创建**一个**任务（``source_message_ids`` 带上全部消息 ID），
        投递时作为一条消息发出，不再逐张刷屏。
        """
        enqueued = 0
        for route in routes:
            fresh = await session.get(Route, route.id)
            if fresh is None or not fresh.enabled:
                continue
            # P4-03：租户到期 / 停用后连"监听入库"也一起停，不是只停投递
            if not await tenant_runtime_service.is_runtime_allowed(session, fresh.tenant_id):
                continue
            config = load_a_config(fresh.a_config)
            kept = [
                view
                for view in views
                if filter_reason(view, content_types=config.content_types) is None
            ]
            if not kept:
                continue
            # 文案通常挂在这条帖子的第一张上；找第一条有正文的当文案来源
            text = next((view.text for view in kept if (view.text or "").strip()), "")
            _cleaned, keep = resolve_caption(
                clean_text(text, CleanRules.from_config(config.model_dump())),
                empty_text_policy=config.empty_text_policy,
                has_media=any(view.kind != KIND_TEXT for view in kept),
            )
            if not keep:
                continue
            target_ids = await history_service.route_target_chat_ids(session, fresh.id)
            if not target_ids:
                continue
            message_ids = [view.message_id for view in kept]
            last_message_id = max(message_ids)
            await delivery_service.enqueue_message(
                session,
                route=fresh,
                target_chat_ids=target_ids,
                source_chat_id=fresh.source_chat_id,
                source_message_id=last_message_id,
                media_group_id=media_group_id,
                source_message_ids=message_ids,
            )
            for chat_id in target_ids:
                await delivery_service.advance_progress(
                    session,
                    route_id=fresh.id,
                    target_chat_id=chat_id,
                    source_message_id=last_message_id,
                )
            enqueued += 1
            logger.info(
                "源消息 {} 已入队（线路 {}，{} 条媒体）",
                last_message_id,
                fresh.name,
                len(message_ids),
            )
        return enqueued

    async def _on_monitor_message(
        self,
        client: Any,
        event: Any,
        routes: list[Route],
    ) -> None:
        """B 线：监听会员发言 → 命中关键词就落线索（按配置决定是否推卡片）。"""
        message = event.message
        view = message_view_from_telethon(message)
        if view.kind == KIND_SERVICE:
            return
        text = (view.text or "").strip()
        if not text:
            return
        raw_sender = getattr(message, "sender", None)
        if raw_sender is None:
            # 事件里的 message.sender 有时是空的（尤其是频道匿名发言），
            # 再问一次客户端，能拿到就带上发言人信息
            with contextlib.suppress(Exception):
                raw_sender = await message.get_sender()
        sender = sender_info(raw_sender)
        if view.is_post and sender.tg_user_id is None:
            # 频道以频道身份发帖：不是会员发言，B 线不适用
            logger.debug("跳过频道匿名发言：{}", text[:30])
            return

        async with session_scope() as session:
            # 同一条源消息会关联多条线路：先一次性取回线路和来源群，避免循环里逐条查库。
            route_ids = [route.id for route in routes]
            fresh_routes = (
                list(
                    await session.scalars(
                        select(Route).where(Route.id.in_(route_ids), Route.enabled.is_(True))
                    )
                )
                if route_ids
                else []
            )
            fresh_by_id = {row.id: row for row in fresh_routes}
            source_ids = {row.source_chat_id for row in fresh_routes}
            source_rows = (
                list(await session.scalars(select(TenantChat).where(TenantChat.id.in_(source_ids))))
                if source_ids
                else []
            )
            source_by_id = {row.id: row for row in source_rows}
            runtime_allowed: dict[int, bool] = {}
            entries_cache: dict[tuple[int, ...], Any] = {}
            exclude_cache: dict[tuple[int, ...], tuple[str, ...]] = {}

            # 热门词采集一条消息只做一次（同一条消息不因多条线路重复计数）
            collected = False
            # 链接滚雪球同样一条消息只做一次
            link_absorbed = False
            for route in routes:
                fresh = fresh_by_id.get(route.id)
                if fresh is None:
                    continue
                # P4-03：同上，租户停了就不再采集线索
                allowed = runtime_allowed.get(fresh.tenant_id)
                if allowed is None:
                    allowed = await tenant_runtime_service.is_runtime_allowed(
                        session, fresh.tenant_id
                    )
                    runtime_allowed[fresh.tenant_id] = allowed
                if not allowed:
                    continue
                config = load_b_config(fresh.b_config)
                if config.skip_bots and sender.is_bot:
                    continue
                if sender.tg_user_id and config.sender_blacklist:
                    if sender.tg_user_id in config.sender_blacklist:
                        continue
                if config.sender_whitelist and sender.tg_user_id not in config.sender_whitelist:
                    continue
                if len(text) < config.min_text_length:
                    continue

                source_chat = source_by_id.get(fresh.source_chat_id)
                if source_chat is None or not source_chat.tg_id:
                    continue
                if config.skip_admins and await self._is_admin(
                    client,
                    chat_id=fresh.source_chat_id,
                    tg_id=int(source_chat.tg_id),
                    user_id=sender.tg_user_id,
                ):
                    continue

                keyword_group_key = tuple(config.keyword_group_ids)
                entries = entries_cache.get(keyword_group_key)
                if entries is None:
                    entries = await keyword_service.load_entries(
                        session,
                        config.keyword_group_ids or None,
                    )
                    entries_cache[keyword_group_key] = entries
                # 排除词在最前面判断：命中就整条忽略（全量模式同样生效）
                exclude_group_key = tuple(config.exclude_group_ids)
                group_exclude_words = exclude_cache.get(exclude_group_key)
                if group_exclude_words is None:
                    group_exclude_words = tuple(
                        await keyword_service.load_exclude_words(
                            session,
                            config.exclude_group_ids,
                        )
                    )
                    exclude_cache[exclude_group_key] = group_exclude_words
                exclude_words = tuple(
                    dict.fromkeys([*config.exclude_keywords, *group_exclude_words])
                )
                if is_excluded(text, exclude_words):
                    logger.debug("被排除词挡住：{}", text[:30])
                    continue
                # F-R03 链接滚雪球：监听中的实时消息里贴的群链接，顺手收进候选池
                # （只做一次，同一条消息不因多条线路重复入库）
                if not link_absorbed:
                    link_absorbed = True
                    await resource_probe_service.absorb_links(
                        session,
                        self.config,
                        text=text,
                        source_title=(
                            source_chat.display_name
                            or source_chat.title
                            or source_chat.username
                            or ""
                        ),
                        discovered_by=DISCOVER_LINK,
                    )
                # 热门词采集：只做一次（同一条消息不因多条线路重复计数）
                if not collected:
                    collected = True
                    await hot_keyword_service.collect_message(
                        session,
                        text=text,
                        source_chat_id=fresh.source_chat_id,
                        source_title=(
                            source_chat.display_name
                            or source_chat.title
                            or source_chat.username
                            or ""
                        ),
                        seen_at=view.date,
                    )
                hits = match_text(
                    text,
                    entries,
                    sensitivity=config.sensitivity,
                    match_contains=config.match_contains,
                    match_fuzzy=config.match_fuzzy,
                    exclude=(),
                )
                hit = hits[0] if hits else None
                # 全量监听要把未命中的发言也落进线索池；关键词模式只处理命中项。
                if hit is None and config.listen_mode != "all":
                    continue

                # 硬性准入：必须留有可用于冷触达的直接字段。
                # 除发送者用户名 / 手机号外，正文里明确留下的手机号、微信、@用户名也算。
                # 全量监听也不能绕过，否则卡片会出现“无用户名 + 未提供联系方式”的无效线索。
                contacts = extract_contacts(
                    text,
                    capture_phone=config.capture_phone,
                    capture_contact=config.capture_contact,
                )
                decision = await lead_service.evaluate_capture_eligibility(
                    session,
                    tenant_id=fresh.tenant_id,
                    sender=sender,
                    text=text,
                    capture_mode=config.capture_mode,
                    source_account_id=fresh.exec_account_id,
                    contacts=contacts,
                )
                if not decision.allowed:
                    logger.debug(
                        "跳过不可冷触达用户：{}（线路 {}，原因 {}）",
                        sender.display_name or sender.tg_user_id,
                        fresh.name,
                        decision.reason,
                    )
                    continue
                if hit is not None and await lead_service.recent_lead_exists(
                    session,
                    route_id=fresh.id,
                    sender_tg_id=sender.tg_user_id,
                    keyword=hit.keyword,
                    minutes=config.hit_cooldown_minutes,
                ):
                    continue

                source_title = (
                    source_chat.display_name or source_chat.title or source_chat.username or ""
                )
                lead = await lead_service.record_lead(
                    session,
                    route_id=fresh.id,
                    source_chat_id=fresh.source_chat_id,
                    message_id=view.message_id,
                    message_at=view.date,
                    sender=sender,
                    contacts=contacts,
                    keyword=hit.keyword if hit else None,
                    keyword_group_id=hit.group_id if hit else None,
                    matched_mode=hit.mode if hit else "",
                    score=hit.score if hit else 0.0,
                    text=text,
                    source_title=source_title,
                    tenant_id=fresh.tenant_id,
                    reachable_routes=decision.reachable_routes,
                    consent_type=decision.consent_type,
                    capture_reason=decision.reason,
                    route_owner_account_id=decision.route_owner_account_id,
                )
                await lead_service.upsert_member(
                    session,
                    sender,
                    seen_at=view.date,
                    hit=hit is not None,
                    tenant_id=fresh.tenant_id,
                    reachable_routes=decision.reachable_routes,
                    consent_type=decision.consent_type,
                )
                logger.info(
                    "监听到发言：{}（线路 {}，命中 {}）",
                    sender.display_name or sender.tg_user_id,
                    fresh.name,
                    hit.keyword if hit else "无（全量入库）",
                )

                # 全量监听默认只入库不刷屏；命中关键词或显式打开时才推卡片。
                if hit is None and not config.push_card_on_all:
                    continue
                await self._push_lead_card(
                    client,
                    session,
                    route=fresh,
                    config=config,
                    lead=lead,
                    sender=sender,
                    contacts=contacts,
                    keyword=hit.keyword if hit else "",
                    source_title=source_title,
                    message_at=view.date,
                    text=text,
                )

    async def _is_admin(
        self,
        client: Any,
        *,
        chat_id: int,
        tg_id: int,
        user_id: int | None,
    ) -> bool:
        """发送者是不是这个群的管理员（按源缓存成员列表，避免每条消息都查）。"""
        if not user_id:
            return False
        cached = self._admin_cache.get(chat_id)
        if cached is None:
            cached = await self._load_admin_ids(client, chat_id=chat_id, tg_id=tg_id)
        return user_id in cached

    async def _load_admin_ids(self, client: Any, *, chat_id: int, tg_id: int) -> set[int]:
        ids: set[int] = set()
        try:
            from telethon.tl.types import ChannelParticipantsAdmins

            entity = await resolve_entity(client, tg_id)
            async for participant in client.iter_participants(
                entity,
                filter=ChannelParticipantsAdmins,
            ):
                participant_id = getattr(participant, "id", None)
                if participant_id:
                    ids.add(int(participant_id))
        except Exception as exc:  # noqa: BLE001 - 拿不到就当没有管理员规则
            logger.debug("读取管理员列表失败（本次不跳过管理员）：{}", exc)
        self._admin_cache[chat_id] = ids
        return ids

    async def _push_lead_card(
        self,
        client: Any,
        session: Any,
        *,
        route: Route,
        config: Any,
        lead: Any,
        sender: Any,
        contacts: Any,
        keyword: str,
        source_title: str,
        message_at: Any,
        text: str,
    ) -> None:
        """把线索卡片推到这条线路的接收目标。"""
        target_ids = await history_service.route_target_chat_ids(session, route.id)
        if not target_ids:
            logger.warning("线索 #{} 没有接收目标，先在线路里加一个", lead.id)
            return
        bot_api = None
        if route.sender_mode == SENDER_MODE_BOT:
            # 线索卡片也交给机器人发，风控不落在账号上
            try:
                bot_api = await self._bot_api(route.notify_bot_id)
            except Exception as exc:  # noqa: BLE001 - 拿不到机器人就退回账号
                logger.warning("取发送机器人失败，改用执行账号：{}", exc)
                bot_api = None
        card = render_lead_card(
            config.lead_template,
            sender=sender,
            contacts=contacts,
            keyword=keyword,
            text=text,
            source_title=source_title,
            message_at=message_at,
            capture_phone=config.capture_phone,
            capture_contact=config.capture_contact,
        )
        pushed = 0
        for chat_id in target_ids:
            target_chat = await session.get(TenantChat, chat_id)
            if target_chat is None or not target_chat.tg_id:
                continue
            try:
                if bot_api is not None:
                    result = await bot_api.send_message(
                        bot_api_chat_id(target_chat.tg_id, target_chat.chat_type),
                        card,
                    )
                    message_id = (result or {}).get("message_id")
                else:
                    target_entity = await resolve_entity(client, int(target_chat.tg_id))
                    sent = await client.send_message(target_entity, card)
                    message_id = getattr(sent, "id", None)
            except Exception as exc:  # noqa: BLE001 - 单个目标失败不影响其他目标
                logger.warning("线索卡片推送失败（目标 {}）：{}", chat_id, exc)
                continue
            await lead_service.mark_delivered(
                session,
                lead,
                target_chat_id=chat_id,
                target_message_id=message_id,
            )
            pushed += 1
        if pushed:
            logger.info("线索 #{} 已推送到 {} 个目标", lead.id, pushed)

    async def _purge(self) -> None:
        """按保留策略清理到期线索与会员档案（线索删除前先归档）。"""
        try:
            async with session_scope() as session:
                result = await lead_service.purge_expired(
                    session,
                    leads_days=self.config.retention.leads_days,
                    profiles_days=self.config.retention.member_profiles_days,
                    archive_dir=self.config.path("data/archive"),
                )
            if result["leads"] or result["profiles"]:
                logger.info(
                    "保留策略清理：线索 {} 条（归档 {} 条）、会员档案 {} 条",
                    result["leads"],
                    result["archived"],
                    result["profiles"],
                )
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响监听
            logger.warning("保留策略清理失败：{}", exc)

    async def _loop(self) -> None:
        """心跳 + 串行投递循环，直到收到停止请求。

        投递与资源发现的连接都按"这条任务属于哪个租户"现取（P1-06）。
        """
        last_heartbeat = 0.0
        while not self._stop:
            if is_stop_requested(self.control_path):
                logger.info("收到停止请求，正在退出…")
                break
            if is_paused(self.control_path):
                await asyncio.sleep(self.poll_interval)
                continue

            now = asyncio.get_running_loop().time()
            if now - self._last_purge_at >= 3600:
                self._last_purge_at = now
                await self._purge()

            try:
                delivered = await self._deliver_once()
            except Exception as exc:  # noqa: BLE001 - 单次循环异常不应终止进程
                logger.exception("投递循环异常：{}", exc)
                delivered = 0

            if now - self._last_resource_tick >= RESOURCE_TICK_SECONDS:
                self._last_resource_tick = now
                try:
                    await self._resource_tick()
                except Exception as exc:  # noqa: BLE001 - 资源发现失败不影响搬运
                    logger.warning("资源发现循环异常：{}", exc)

            now = asyncio.get_running_loop().time()
            if now - last_heartbeat >= self.heartbeat_seconds or delivered == 0:
                await self._publish(status="running", extra={"last_tick_delivered": delivered})
                last_heartbeat = now
            if delivered == 0:
                await asyncio.sleep(self.poll_interval)

    async def _resource_tick(self, client: Any = None) -> None:
        """资源发现的节奏：加群队列 → 目录同步 → 探测候选，一轮只做一件。

        每轮只做一件事是刻意设计的：加群、搜索、探测都要消耗账号的请求额度，
        串行执行时"是哪一步在触发风控"一目了然，也天然满足 F-R12 的"同账号串行"。

        P1-06：加群与探测都用**这条任务所属租户**的账号和连接（账号 ID 也跟着走），
        不是全库第一个可用账号。
        """
        async with session_scope() as session:
            task = await resource_join_service.next_due_task(session)
            if task is not None:
                connection = client or await self._client_for(task.tenant_id, session)
                await resource_join_service.run_task(
                    session,
                    self.config,
                    connection,
                    task,
                    actor="runtime",
                )
                return

            directory = await directory_sync_service.next_due_task(session)
            if directory is not None:
                outcome = await directory_sync_service.run_task(
                    session,
                    self.config,
                    directory,
                    fetcher=self._directory_fetcher_for_tick(),
                )
                run = outcome.get("run") or {}
                if run.get("items_added"):
                    logger.info(
                        "目录同步 {}/{} 新增 {} 条候选",
                        outcome.get("source"),
                        outcome.get("scope"),
                        run.get("items_added"),
                    )
                return

            candidates = await resource_service.next_probe_candidates(session, limit=1)
            if not candidates:
                return
            resource = candidates[0]
            connection = client or await self._client_for(resource.tenant_id, session)
            account = await tg_account_service.get_default_account(
                session,
                tenant_id=resource.tenant_id,
            )
            outcome = await resource_probe_service.probe_resource(
                session,
                self.config,
                connection,
                resource,
                account_id=account.id if account else None,
            )
            if outcome.new_resources:
                logger.info(
                    "探测资源 {} 时滚出 {} 条新候选",
                    resource.id,
                    outcome.new_resources,
                )

    async def _deliver_once(self, client: Any = None) -> int:
        """投递一条就绪任务；返回 1 表示有投递，0 表示队列空。

        P1-06：用**线路所属租户**的执行账号投递。该租户没有可用账号时跳过这条
        任务并记下原因——不能让一条取不到号的任务卡住其他租户的队列。
        """
        from app.core.route_config import load_a_config as _load_a

        async with session_scope() as session:
            job = await delivery_service.next_ready_job(session)
            if job is None:
                return 0
            route = await session.get(Route, job.route_id) if job.route_id else None
            if route is None:
                await delivery_service.skip_job(session, job, reason="线路已删除")
                return 1
            # P4-03 第一层：投递前再判一次租户开关与到期状态。
            # 必须是"每次投递都算"，不能挂在启动注册那一步（否则到期后还能发）。
            if not await tenant_runtime_service.is_runtime_allowed(session, route.tenant_id):
                await delivery_service.cancel_job(
                    session,
                    job,
                    reason="账号已停止运行，投递任务已取消",
                )
                return 1
            try:
                connection = client or await self._client_for(route.tenant_id, session)
            except Exception as exc:  # noqa: BLE001 - 该租户没号：跳过这条，别卡住队列
                await delivery_service.skip_job(
                    session,
                    job,
                    reason=f"线路所属租户没有可用的执行账号：{exc}",
                )
                return 1
            source_chat = await session.get(TenantChat, route.source_chat_id)
            target_chat = await session.get(TenantChat, job.target_chat_id)
            if source_chat is None or target_chat is None:
                await delivery_service.skip_job(session, job, reason="源或目标已删除")
                return 1

            a_config = _load_a(route.a_config)
            ad_asset = None
            if a_config.ad_asset_id:
                from app.db.models import AdAsset

                ad_asset = await session.get(AdAsset, a_config.ad_asset_id)

            try:
                source_entity = await resolve_entity(connection, int(source_chat.tg_id))
            except Exception as exc:  # noqa: BLE001 - 解析失败按投递失败处理
                await delivery_service.mark_failure(session, job, error=str(exc))
                return 1

            try:
                target_entity = await resolve_entity(connection, int(target_chat.tg_id))
            except Exception as exc:  # noqa: BLE001 - 解析失败按投递失败处理
                await delivery_service.mark_failure(session, job, error=str(exc))
                return 1

            source_messages = None
            caption = None
            if a_config.text_mode == "clean":
                source_messages, caption = await self._load_clean_source(
                    connection,
                    source_entity=source_entity,
                    job=job,
                    a_config=a_config,
                )

            await delivery_service.deliver_job(
                session,
                self.config,
                job=job,
                route=route,
                client=connection,
                source_chat=source_chat,
                target_chat=target_chat,
                a_config=a_config,
                ad_asset=ad_asset,
                source_entity=source_entity,
                target_entity=target_entity,
                source_messages=source_messages,
                caption=caption,
            )
            return 1

    async def _load_clean_source(
        self,
        client: Any,
        *,
        source_entity: Any,
        job: Any,
        a_config: Any,
    ) -> tuple[list[Any] | None, str | None]:
        """净化模式：取回原消息并算出净化后的文案（相册整组一起取回）。"""
        message_ids = delivery_service.job_message_ids(job)
        try:
            fetched = await client.get_messages(source_entity, ids=message_ids)
        except Exception as exc:  # noqa: BLE001 - 取不到原消息就退回转发
            logger.warning("取原消息失败，退回转发：{}", exc)
            return None, None

        messages = fetched if isinstance(fetched, list) else [fetched]
        messages = [item for item in messages if item is not None]
        if not messages:
            return None, None
        views = [message_view_from_telethon(item) for item in messages]
        text = next((view.text for view in views if (view.text or "").strip()), "")
        caption = clean_text(text, CleanRules.from_config(a_config.model_dump()))
        return messages, caption

    async def stop(self) -> None:
        """请求停止循环。"""
        self._stop = True

    async def _publish(self, *, status: str, extra: dict[str, Any]) -> None:
        async with session_scope() as session:
            counts = await delivery_service.job_stats(session)
            write_status(
                self.status_path,
                {
                    "status": status,
                    "pid": os.getpid(),
                    "paused": is_paused(self.control_path),
                    "queue": counts,
                    "routes": self._registered,
                    **extra,
                },
            )


async def runtime_status(config: AppConfig) -> dict[str, Any]:
    """读取运行时状态（供 CLI status 使用）。"""
    status = read_status(config.path(config.runtime.status_file))
    return {
        "status": (status or {}).get("status", "stopped"),
        "heartbeat_age_seconds": heartbeat_age_seconds(status),
        "raw": status,
    }


def _runtime_is_alive(config: AppConfig) -> bool:
    """心跳还新鲜、且记录到的 PID 仍存活时，运行时才算活着。"""
    status = read_status(config.path(config.runtime.status_file))
    if not is_running(status):
        return False
    pid = status.get("pid")
    if pid is None:
        return True
    try:
        return _pid_alive(int(pid))
    except (TypeError, ValueError):
        return False


def _pid_alive(pid: int | None) -> bool:
    """进程是否还活着（用 psutil，避免把已退出的 PID 当成占用）。"""
    if not pid:
        return False
    try:
        import psutil

        return bool(psutil.pid_exists(int(pid)))
    except Exception:  # noqa: BLE001 - 判断不了就当它不在
        return False


def _lock_holder_pid(lock_path: Any) -> int | None:
    """运行时锁里记的 PID（没有锁或进程已退出时返回 None）。"""
    from app.core.paths import read_json

    payload = read_json(lock_path) or {}
    try:
        value = int(payload.get("pid"))
    except (TypeError, ValueError):
        return None
    return value if _pid_alive(value) else None


async def pending_route_ids(
    session: Any,
    config: AppConfig,
    *,
    tenant_id: int | None = None,
) -> list[int] | None:
    """启用中、但没被正在运行的运行时接管的线路 ID。

    None 表示判断不出来（运行时没在跑，或心跳还是旧格式）——界面据此不做提醒。
    ``tenant_id`` 非空时只算该租户的线路（会员不该看到别人的线路号）。
    """
    from app.db.models import Route

    status = read_status(config.path(config.runtime.status_file))
    routes = (status or {}).get("routes") or {}
    registered = routes.get("ids")
    if not _runtime_is_alive(config) or registered is None:
        return None
    statement = select(Route.id).where(Route.enabled.is_(True))
    if tenant_id is not None:
        statement = statement.where(Route.tenant_id == tenant_id)
    enabled = list(await session.scalars(statement))
    return sorted(set(enabled) - set(registered))


async def start_runtime_process(
    config: AppConfig,
    *,
    wait_seconds: float = 10.0,
) -> dict[str, Any]:
    """在后台拉起搬运运行时（等价于 `python main.py run`）。

    两个关键点：
    1. 启动前必须清掉上一次的停止/暂停标记，否则新进程会立刻退出；
    2. 启动后等心跳出现再返回，避免界面显示"已启动"但进程其实起不来。
    """
    status_path = config.path(config.runtime.status_file)
    control_path = config.path(config.runtime.control_file)
    if _runtime_is_alive(config):
        status = read_status(status_path) or {}
        return {"started": False, "reason": "already_running", "pid": status.get("pid")}

    # 卡死的旧进程可能还占着运行时锁（心跳过期但进程还在），先请它退出再启动，
    # 否则新进程会因抢不到锁而静默失败——界面看起来就是"点了启动但一直没起来"。
    stale_pid = _lock_holder_pid(config.path(config.runtime.lock_file))
    if stale_pid:
        set_stop_requested(control_path, True)
        for _ in range(16):
            await asyncio.sleep(0.5)
            if not _pid_alive(stale_pid):
                break
        else:
            return {
                "started": False,
                "reason": "locked",
                "pid": stale_pid,
                "hint": (
                    f"上一次的运行时进程（PID {stale_pid}）还在运行且没响应停止请求，"
                    "请先结束它，或在任务管理器里结束对应 python 进程后重试"
                ),
            }

    set_stop_requested(control_path, False)
    set_paused(control_path, False)

    err_path = Path(config.project_root) / "data" / "runtime.stderr.log"
    # Popen 是阻塞调用，丢到线程里执行，别卡住事件循环
    process = await asyncio.to_thread(_spawn_runtime, config)

    deadline = asyncio.get_running_loop().time() + wait_seconds
    while asyncio.get_running_loop().time() < deadline:
        if _runtime_is_alive(config):
            return {"started": True, "pid": process.pid}
        if process.poll() is not None:
            tail = _tail_text(err_path)
            return {
                "started": False,
                "reason": "exited",
                "pid": process.pid,
                "hint": tail or "运行时启动后立刻退出，请查看 data/runtime.stderr.log",
            }
        await asyncio.sleep(0.5)

    return {
        "started": False,
        "reason": "timeout",
        "pid": process.pid,
        "hint": "运行时进程已拉起，但还没写出心跳；稍等几秒刷新看看",
    }


async def restart_runtime_process(
    config: AppConfig,
    *,
    wait_seconds: float = 15.0,
) -> dict[str, Any]:
    """重启运行时：改完线路/目标后用它让配置生效。"""
    control_path = config.path(config.runtime.control_file)
    if not _runtime_is_alive(config):
        return await start_runtime_process(config, wait_seconds=wait_seconds)

    set_stop_requested(control_path, True)
    set_paused(control_path, True)
    deadline = asyncio.get_running_loop().time() + wait_seconds
    while asyncio.get_running_loop().time() < deadline:
        if not _runtime_is_alive(config):
            break
        await asyncio.sleep(0.5)
    return await start_runtime_process(config)


def _tail_text(path: Path, limit: int = 400) -> str:
    """读取文件末尾内容，用于把启动失败原因带回界面。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = text.strip()
    return text[-limit:] if text else ""


def _runtime_executable() -> str:
    """Windows 优先用 pythonw.exe，确保后台运行时连控制台句柄都不创建。"""
    executable = Path(sys.executable)
    if executable.name.lower() == "python.exe":
        pythonw = executable.with_name("pythonw.exe")
        if pythonw.is_file():
            return str(pythonw)
    return str(executable)


def _spawn_runtime(config: AppConfig) -> subprocess.Popen:
    """同步拉起运行时进程（由 start_runtime_process 放进线程执行）。"""
    root = Path(config.project_root)
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / "runtime.stdout.log"
    err_path = data_dir / "runtime.stderr.log"

    creationflags = 0
    new_session = True
    startupinfo = None
    if os.name == "nt":  # pragma: no cover - Windows 分支
        # 用户点「启动」时不许弹黑色控制台窗口：
        #   DETACHED_PROCESS 不继承父进程控制台，CREATE_NO_WINDOW 连新控制台也不建，
        #   STARTF_USESHOWWINDOW + SW_HIDE 再兜一层（venv 的 python.exe 会再拉子进程）。
        creationflags = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
        new_session = False
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE

    with out_path.open("ab") as out, err_path.open("ab") as err:
        process = subprocess.Popen(  # noqa: S603 - 命令固定，参数不来自外部输入
            [_runtime_executable(), "main.py", "run"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=creationflags,
            start_new_session=new_session,
            startupinfo=startupinfo,
        )
    (data_dir / "runtime.pid").write_text(str(process.pid), encoding="utf-8")
    return process
