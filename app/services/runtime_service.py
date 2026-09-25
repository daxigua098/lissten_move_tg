"""运行时：实时监听源消息 + 串行投递队列。

一个进程内只跑一个实例（靠 RuntimeLock 兜底），所有投递严格串行，
与《需求说明书》的"单账号串行、不并发发送"一致。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from loguru import logger

from app.core.config import AppConfig
from app.core.content_cleaner import CleanRules, clean_text, filter_reason, resolve_caption
from app.core.heartbeat import write_status
from app.core.route_config import load_a_config
from app.core.runtime_control import is_paused, is_stop_requested
from app.core.runtime_lock import RuntimeLock, RuntimeLockError
from app.core.telegram_client import message_view_from_telethon
from app.db.models import (
    ACCOUNT_ACTIVE,
    BUSINESS_CARRY,
    Chat,
    Route,
)
from app.db.session import session_scope
from app.services import delivery_service, history_service, tg_account_service


class RuntimeService:
    """A 线搬运运行时。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        client_factory: Any = None,
        poll_interval: float = 2.0,
        heartbeat_seconds: int | None = None,
    ) -> None:
        self.config = config
        self.client_factory = client_factory
        self.poll_interval = poll_interval
        self.heartbeat_seconds = heartbeat_seconds or config.runtime.heartbeat_seconds
        self._lock = RuntimeLock(config.path(config.runtime.lock_file))
        self._stop = False

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
            logger.info("实时监听已启动，覆盖 {} 条 A 线", handlers)
            await self._publish(status="running", extra={"routes": handlers})
            await self._loop(client)
        finally:
            if owns_client and client is not None:
                with contextlib.suppress(Exception):
                    await client.disconnect()
            self._lock.release()
            await self._publish(status="stopped", extra={})
        return 0

    async def _open_client(self) -> Any:
        async with session_scope() as session:
            account = await tg_account_service.get_default_account(session)
            if account is None:
                raise RuntimeError("没有可用的执行账号，请先在「执行账号池」登记并登录")
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

    async def _register_handlers(self, client: Any) -> int:
        """给每条启用的 A 线源注册新消息监听。"""
        from telethon import events

        async with session_scope() as session:
            from sqlalchemy import select

            records = list(
                await session.scalars(
                    select(Route).where(
                        Route.business_type == BUSINESS_CARRY,
                        Route.enabled.is_(True),
                    )
                )
            )
            sources: dict[int, list[Route]] = {}
            for route in records:
                sources.setdefault(route.source_chat_id, []).append(route)
            source_entities = {}
            for chat_id in sources:
                chat = await session.get(Chat, chat_id)
                if chat is not None and chat.tg_id:
                    source_entities[chat_id] = int(chat.tg_id)

        count = 0
        for chat_id, route_list in sources.items():
            tg_id = source_entities.get(chat_id)
            if not tg_id:
                continue
            try:
                entity = await client.get_entity(tg_id)
            except Exception as exc:  # noqa: BLE001 - 单个源解析失败跳过
                logger.warning("监听源 {} 解析失败：{}", chat_id, exc)
                continue

            async def handler(event: Any, routes=route_list) -> None:  # noqa: ANN001
                await self._on_new_message(event, routes)

            client.add_event_handler(handler, events.NewMessage(chats=entity))
            count += 1
        return count

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

            try:
                source_entity = await client.get_entity(int(source_chat.tg_id))
                target_entity = await client.get_entity(int(target_chat.tg_id))
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
                **extra,
            },
        )


async def runtime_status(config: AppConfig) -> dict[str, Any]:
    """读取运行时状态（供 CLI status 使用）。"""
    from app.core.heartbeat import heartbeat_age_seconds, read_status

    status = read_status(config.path(config.runtime.status_file))
    return {
        "status": (status or {}).get("status", "stopped"),
        "heartbeat_age_seconds": heartbeat_age_seconds(status),
        "raw": status,
    }
