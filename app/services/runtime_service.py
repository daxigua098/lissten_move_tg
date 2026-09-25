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
from pathlib import Path
from typing import Any

from loguru import logger

from app.core.bot_api import MEDIA_DOCUMENT, MEDIA_PHOTO, MEDIA_VIDEO, bot_api_chat_id
from app.core.config import AppConfig
from app.core.content_cleaner import (
    KIND_SERVICE,
    CleanRules,
    clean_text,
    filter_reason,
    resolve_caption,
)
from app.core.heartbeat import heartbeat_age_seconds, read_status, write_status
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
    LISTEN_MODE_ALL,
    SENDER_MODE_BOT,
    Chat,
    Route,
)
from app.db.session import session_scope
from app.services import (
    bot_service,
    delivery_service,
    history_service,
    hot_keyword_service,
    keyword_service,
    lead_service,
    tg_account_service,
)


def _bot_media_kind(view_kind: str) -> str:
    """把内容类型映射成 Bot API 的上传方式。"""
    from app.core.content_cleaner import KIND_PHOTO, KIND_VIDEO

    if view_kind == KIND_PHOTO:
        return MEDIA_PHOTO
    if view_kind == KIND_VIDEO:
        return MEDIA_VIDEO
    return MEDIA_DOCUMENT


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
        # 本次启动注册了哪些线路（写进心跳，界面上用来发现"改了但没重启"）
        self._registered: dict[str, Any] = {}
        # 机器人（Bot API）客户端缓存：sender_mode=bot 的线路用它们发言
        self._bot_apis: dict[int, Any] = {}

    @property
    def control_path(self):
        """控制文件路径。"""
        return self.config.path(self.config.runtime.control_file)

    @property
    def status_path(self):
        """状态文件路径。"""
        return self.config.path(self.config.runtime.status_file)

    async def run(self, *, client: Any = None) -> int:
        """启动运行：获取锁、注册监听、进入心跳与队列循环。"""
        try:
            self._lock.acquire()
        except RuntimeLockError as exc:
            logger.error("启动失败：{}", exc)
            return 1

        owns_client = client is None
        try:
            if client is None:
                client = await self._open_client()
            handlers = await self._register_handlers(client)
            self._registered = handlers
            logger.info(
                "实时监听已启动：{} 个源（A 线 {} 条 / B 线 {} 条）",
                handlers["sources"],
                handlers["carry"],
                handlers["monitor"],
            )
            await self._publish(status="running", extra={"routes": handlers})
            # 心跳独立跑：投递循环里在下载大文件时，界面也不会显示成掉线
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())
            try:
                await self._loop(client)
            finally:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
        finally:
            if owns_client and client is not None:
                with contextlib.suppress(Exception):
                    await client.disconnect()
            await self._close_bot_clients()
            self._lock.release()
            await self._publish(status="stopped", extra={})
        return 0

    async def _heartbeat_loop(self) -> None:
        """只负责定期写心跳，与投递循环解耦。"""
        while True:
            await asyncio.sleep(max(2, self.heartbeat_seconds // 2))
            with contextlib.suppress(Exception):
                await self._publish(status="running", extra={})

    async def _open_client(self) -> Any:
        from app.core.demo_client import DemoAccountClient

        async with session_scope() as session:
            account = await tg_account_service.get_default_account(session)
            if account is None:
                raise RuntimeError("没有可用的执行账号，请先在「执行账号池」登记并登录")
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

    async def _register_handlers(self, client: Any) -> int:
        """给每条启用的线路源注册新消息监听（A 线搬运 + B 线监听共用一次注册）。"""
        from telethon import events

        async with session_scope() as session:
            from sqlalchemy import select

            records = list(await session.scalars(select(Route).where(Route.enabled.is_(True))))
            sources: dict[int, dict[str, list[Route]]] = {}
            for route in records:
                bucket = sources.setdefault(route.source_chat_id, {"A": [], "B": []})
                bucket.setdefault(route.business_type, []).append(route)
            source_entities: dict[int, int] = {}
            for chat_id in sources:
                chat = await session.get(Chat, chat_id)
                if chat is not None and chat.tg_id:
                    source_entities[chat_id] = int(chat.tg_id)

        counts: dict[str, Any] = {"sources": 0, "carry": 0, "monitor": 0, "ids": []}
        for chat_id, buckets in sources.items():
            tg_id = source_entities.get(chat_id)
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
        return counts

    async def _on_new_message(self, event: Any, routes: list[Route]) -> None:
        """实时消息入队。"""
        view = message_view_from_telethon(event.message)
        async with session_scope() as session:
            for route in routes:
                fresh = await session.get(Route, route.id)
                if fresh is None or not fresh.enabled:
                    continue
                config = load_a_config(fresh.a_config)
                if filter_reason(view, content_types=config.content_types) is not None:
                    continue
                cleaned, keep = resolve_caption(
                    clean_text(view.text, CleanRules.from_config(config.model_dump())),
                    empty_text_policy=config.empty_text_policy,
                    has_media=view.kind != "text",
                )
                if not keep:
                    continue
                target_ids = await history_service.route_target_chat_ids(session, fresh.id)
                if not target_ids:
                    continue
                await delivery_service.enqueue_message(
                    session,
                    route=fresh,
                    target_chat_ids=target_ids,
                    source_chat_id=fresh.source_chat_id,
                    source_message_id=view.message_id,
                    media_group_id=view.grouped_id,
                    source_message_ids=[view.message_id],
                )
                for chat_id in target_ids:
                    await delivery_service.advance_progress(
                        session,
                        route_id=fresh.id,
                        target_chat_id=chat_id,
                        source_message_id=view.message_id,
                    )
                logger.info("源消息 {} 已入队（线路 {}）", view.message_id, fresh.name)

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
            # 热门词采集一条消息只做一次（同一条消息不因多条线路重复计数）
            collected = False
            for route in routes:
                fresh = await session.get(Route, route.id)
                if fresh is None or not fresh.enabled:
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

                source_chat = await session.get(Chat, fresh.source_chat_id)
                if source_chat is None or not source_chat.tg_id:
                    continue
                if config.skip_admins and await self._is_admin(
                    client,
                    chat_id=fresh.source_chat_id,
                    tg_id=int(source_chat.tg_id),
                    user_id=sender.tg_user_id,
                ):
                    continue

                entries = await keyword_service.load_entries(
                    session,
                    config.keyword_group_ids or None,
                )
                # 排除词在最前面判断：命中就整条忽略（全量模式同样生效）
                exclude_words = tuple(
                    dict.fromkeys(
                        [
                            *config.exclude_keywords,
                            *await keyword_service.load_exclude_words(
                                session,
                                config.exclude_group_ids,
                            ),
                        ]
                    )
                )
                if is_excluded(text, exclude_words):
                    logger.debug("被排除词挡住：{}", text[:30])
                    continue
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
                if hit is None and config.listen_mode != LISTEN_MODE_ALL:
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
                contacts = extract_contacts(
                    text,
                    capture_phone=config.capture_phone,
                    capture_contact=config.capture_contact,
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
                )
                await lead_service.upsert_member(
                    session,
                    sender,
                    seen_at=view.date,
                    hit=hit is not None,
                )
                logger.info(
                    "监听到发言：{}（线路 {}，命中 {}）",
                    sender.display_name or sender.tg_user_id,
                    fresh.name,
                    hit.keyword if hit else "无（全量入库）",
                )

                # 全量监听默认只入库不刷屏；命中关键词或显式打开时才推卡片
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
            target_chat = await session.get(Chat, chat_id)
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

    async def _loop(self, client: Any) -> None:
        """心跳 + 串行投递循环，直到收到停止请求。"""
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
                delivered = await self._deliver_once(client)
            except Exception as exc:  # noqa: BLE001 - 单次循环异常不应终止进程
                logger.exception("投递循环异常：{}", exc)
                delivered = 0

            now = asyncio.get_running_loop().time()
            if now - last_heartbeat >= self.heartbeat_seconds or delivered == 0:
                await self._publish(status="running", extra={"last_tick_delivered": delivered})
                last_heartbeat = now
            if delivered == 0:
                await asyncio.sleep(self.poll_interval)

    async def _deliver_once(self, client: Any) -> int:
        """投递一条就绪任务；返回 1 表示有投递，0 表示队列空。"""
        from app.core.route_config import load_a_config as _load_a

        async with session_scope() as session:
            job = await delivery_service.next_ready_job(session)
            if job is None:
                return 0
            route = await session.get(Route, job.route_id) if job.route_id else None
            if route is None:
                await delivery_service.skip_job(session, job, reason="线路已删除")
                return 1
            source_chat = await session.get(Chat, route.source_chat_id)
            target_chat = await session.get(Chat, job.target_chat_id)
            if source_chat is None or target_chat is None:
                await delivery_service.skip_job(session, job, reason="源或目标已删除")
                return 1

            a_config = _load_a(route.a_config)
            ad_asset = None
            if a_config.ad_asset_id:
                from app.db.models import AdAsset

                ad_asset = await session.get(AdAsset, a_config.ad_asset_id)

            use_bot = route.sender_mode == SENDER_MODE_BOT
            try:
                source_entity = await resolve_entity(client, int(source_chat.tg_id))
            except Exception as exc:  # noqa: BLE001 - 解析失败按投递失败处理
                await delivery_service.mark_failure(session, job, error=str(exc))
                return 1

            if use_bot:
                # 机器人发送：Bot API 只认 chat_id；内容由账号取回后交给机器人上传
                try:
                    bot_api = await self._bot_api(route.notify_bot_id)
                except Exception as exc:  # noqa: BLE001 - 拿不到机器人按投递失败
                    await delivery_service.mark_failure(
                        session,
                        job,
                        error=f"取发送机器人失败：{exc}",
                    )
                    return 1

                async def load_payload() -> delivery_service.BotPayload:
                    message, cleaned = await self._load_clean_source(
                        client,
                        source_entity=source_entity,
                        job=job,
                        a_config=a_config,
                    )
                    if message is None:
                        raise RuntimeError("取不到源消息，无法用机器人发送")
                    view = message_view_from_telethon(message)
                    data, name = await self._prepare_bot_payload(client, message)
                    return delivery_service.BotPayload(
                        caption=cleaned if a_config.text_mode == "clean" else view.text,
                        content=data,
                        filename=name,
                        kind=_bot_media_kind(view.kind),
                    )

                await delivery_service.deliver_job_via_bot(
                    session,
                    self.config,
                    job=job,
                    route=route,
                    bot_api=bot_api,
                    target_chat=target_chat,
                    a_config=a_config,
                    ad_asset=ad_asset,
                    source_chat=source_chat,
                    payload_loader=load_payload,
                )
                return 1

            try:
                target_entity = await resolve_entity(client, int(target_chat.tg_id))
            except Exception as exc:  # noqa: BLE001 - 解析失败按投递失败处理
                await delivery_service.mark_failure(session, job, error=str(exc))
                return 1

            source_message = None
            caption = None
            if a_config.text_mode == "clean":
                source_message, caption = await self._load_clean_source(
                    client,
                    source_entity=source_entity,
                    job=job,
                    a_config=a_config,
                )

            await delivery_service.deliver_job(
                session,
                self.config,
                job=job,
                route=route,
                client=client,
                source_chat=source_chat,
                target_chat=target_chat,
                a_config=a_config,
                ad_asset=ad_asset,
                source_entity=source_entity,
                target_entity=target_entity,
                source_message=source_message,
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
    ) -> tuple[Any, str | None]:
        """净化模式：取回原消息并算出净化后的文案。"""
        raw = job.source_message_ids or str(job.source_message_id)
        message_ids = [int(item) for item in str(raw).split(",") if item.strip()]
        try:
            fetched = await client.get_messages(source_entity, ids=message_ids)
        except Exception as exc:  # noqa: BLE001 - 取不到原消息就退回转发
            logger.warning("取原消息失败，退回转发：{}", exc)
            return None, None

        message = fetched[0] if isinstance(fetched, list) else fetched
        if message is None:
            return None, None
        view = message_view_from_telethon(message)
        caption = clean_text(view.text, CleanRules.from_config(a_config.model_dump()))
        return message, caption

    async def _prepare_bot_payload(
        self,
        client: Any,
        message: Any,
    ) -> tuple[bytes | None, str | None]:
        """用机器人发送时，先把媒体下载成字节（机器人不必在源群里）。"""
        from app.core.telegram_client import download_media_bytes

        data = None
        try:
            data = await download_media_bytes(client, message)
        except Exception as exc:  # noqa: BLE001 - 下载失败就只发文案
            logger.warning("下载源媒体失败（将只发文案）：{}", exc)
        filename = getattr(getattr(message, "file", None), "name", None)
        if not filename:
            document = getattr(message, "document", None)
            for attribute in getattr(document, "attributes", None) or []:
                name = getattr(attribute, "file_name", None)
                if name:
                    filename = name
                    break
        return data, filename

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


# 心跳超过这个秒数就认为运行时已经死了
RUNTIME_STALE_SECONDS = 30


def _runtime_is_alive(config: AppConfig) -> bool:
    """心跳还在跳，说明运行时进程活着。"""
    status = read_status(config.path(config.runtime.status_file))
    if not status or status.get("status") != "running":
        return False
    age = heartbeat_age_seconds(status)
    return age is not None and age < RUNTIME_STALE_SECONDS


async def pending_route_ids(session: Any, config: AppConfig) -> list[int] | None:
    """启用中、但没被正在运行的运行时接管的线路 ID。

    None 表示判断不出来（运行时没在跑，或心跳还是旧格式）——界面据此不做提醒。
    """
    from sqlalchemy import select

    from app.db.models import Route

    status = read_status(config.path(config.runtime.status_file))
    routes = (status or {}).get("routes") or {}
    registered = routes.get("ids")
    if not _runtime_is_alive(config) or registered is None:
        return None
    enabled = list(await session.scalars(select(Route.id).where(Route.enabled.is_(True))))
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


def _spawn_runtime(config: AppConfig) -> subprocess.Popen:
    """同步拉起运行时进程（由 start_runtime_process 放进线程执行）。"""
    root = Path(config.project_root)
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    out_path = data_dir / "runtime.stdout.log"
    err_path = data_dir / "runtime.stderr.log"

    creationflags = 0
    new_session = True
    if os.name == "nt":  # pragma: no cover - Windows 分支
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        new_session = False

    with out_path.open("ab") as out, err_path.open("ab") as err:
        process = subprocess.Popen(  # noqa: S603 - 命令固定，参数不来自外部输入
            [sys.executable, "main.py", "run"],
            cwd=str(root),
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            creationflags=creationflags,
            start_new_session=new_session,
        )
    (data_dir / "runtime.pid").write_text(str(process.pid), encoding="utf-8")
    return process
