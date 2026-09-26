"""A 线搬运相册：一条帖子的多张图/视频要合成一条发出，不能逐张刷屏。"""

from __future__ import annotations

from types import SimpleNamespace

from conftest import fake_message

from app.core.content_cleaner import MessageView
from app.core.route_config import ACarryConfig
from app.db.models import Chat, DeliveryJob, Route
from app.db.session import session_scope
from app.services import chat_service, delivery_service, history_service, route_service
from app.services.runtime_service import RuntimeService


async def _prepare_route(db, *, a_config: dict | None = None):
    """建一条 A 线：1 个源 + 1 个接收目标。"""
    from app.core.telegram_client import ChatProfile

    async with session_scope() as session:
        source = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=6001,
                chat_type="channel",
                title="素材源",
                username="album_src",
                is_private=False,
            ),
        )
        await chat_service.set_source(session, source)
        target = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=6101,
                chat_type="channel",
                title="目标",
                username="album_dst",
                is_private=False,
            ),
        )
        await chat_service.set_target(session, target)
        route = await route_service.create_route(
            session,
            name="相册线路",
            source_chat_id=source.id,
            business_type="A",
            target_chat_ids=[target.id],
            a_config=a_config or {"ad_policy": "none"},
        )
        return route.id, source.id, target.id


def _album_event(
    message_id: int,
    grouped_id: int,
    *,
    text: str = "",
    photo: bool = True,
    video: bool = False,
):
    """构造一条属于相册的消息事件。"""
    return SimpleNamespace(
        message=fake_message(
            message_id,
            text,
            photo=photo,
            video=video,
            grouped_id=grouped_id,
        )
    )


async def _jobs(db) -> list[DeliveryJob]:
    from sqlalchemy import select

    async with session_scope() as session:
        return list(await session.scalars(select(DeliveryJob).order_by(DeliveryJob.id)))


async def test_album_items_are_merged_into_one_job(db) -> None:
    """实时收到的多张图，只产生一个任务，并带上全部消息 ID。"""
    route_id, _source_id, _target_id = await _prepare_route(db)
    service = RuntimeService(db)
    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)

    await service._on_new_message(_album_event(1001, 42, text="三张图"), [route])
    await service._on_new_message(_album_event(1002, 42), [route])
    await service._on_new_message(_album_event(1003, 42), [route])

    # 还没收齐：先不入队，否则又会逐张发出去
    assert await _jobs(db) == []

    flushed = await service._flush_album(42)

    assert flushed == 1
    jobs = await _jobs(db)
    assert len(jobs) == 1
    assert jobs[0].source_message_ids == "1001,1002,1003"
    assert jobs[0].source_message_id == 1003  # 水位线看最后一条
    assert jobs[0].media_group_id == 42


async def test_single_message_still_enqueues_immediately(db) -> None:
    """没有 grouped_id 的普通消息仍然立刻入队。"""
    route_id, _source_id, _target_id = await _prepare_route(db)
    service = RuntimeService(db)
    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)

    await service._on_new_message(
        SimpleNamespace(message=fake_message(2001, "普通单图", photo=True)),
        [route],
    )

    jobs = await _jobs(db)
    assert len(jobs) == 1
    assert jobs[0].source_message_ids == "2001"
    assert jobs[0].media_group_id is None


async def test_album_respects_content_type_filter(db) -> None:
    """过滤掉不要的类型后，任务里只留剩下的媒体。"""
    route_id, _source_id, _target_id = await _prepare_route(
        db,
        a_config={"ad_policy": "none", "content_types": ["photo"]},
    )
    service = RuntimeService(db)
    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)

    await service._on_new_message(_album_event(3001, 88), [route])
    await service._on_new_message(_album_event(3002, 88, photo=False, video=True), [route])
    await service._on_new_message(_album_event(3003, 88), [route])

    await service._flush_album(88)

    jobs = await _jobs(db)
    assert len(jobs) == 1
    assert jobs[0].source_message_ids == "3001,3003"  # 视频那条被挡掉


async def test_album_fully_filtered_enqueues_nothing(db) -> None:
    """整组都不符合内容类型时不产生任务。"""
    route_id, _source_id, _target_id = await _prepare_route(
        db,
        a_config={"ad_policy": "none", "content_types": ["text"]},
    )
    service = RuntimeService(db)
    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)

    await service._on_new_message(_album_event(4001, 99), [route])
    await service._on_new_message(_album_event(4002, 99), [route])
    await service._flush_album(99)

    assert await _jobs(db) == []


async def test_two_albums_do_not_mix(db) -> None:
    """同时来的两条帖子各自成组，不会并成一条。"""
    route_id, _source_id, _target_id = await _prepare_route(db)
    service = RuntimeService(db)
    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)

    await service._on_new_message(_album_event(5001, 11), [route])
    await service._on_new_message(_album_event(5002, 22), [route])
    await service._on_new_message(_album_event(5003, 11), [route])
    await service._on_new_message(_album_event(5004, 22), [route])

    await service._flush_album(11)
    await service._flush_album(22)

    jobs = await _jobs(db)
    assert len(jobs) == 2
    assert jobs[0].source_message_ids == "5001,5003"
    assert jobs[1].source_message_ids == "5002,5004"


async def test_delivering_album_sends_once(db, fake_delivery_client) -> None:
    """投递时整组媒体只发一次（与投递引擎串起来跑一遍）。"""
    route_id, source_id, target_id = await _prepare_route(db)
    service = RuntimeService(db)
    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)

    await service._on_new_message(_album_event(6001, 33, text="看图"), [route])
    await service._on_new_message(_album_event(6002, 33), [route])
    await service._flush_album(33)

    async with session_scope() as session:
        job = await delivery_service.next_ready_job(session)
        route = await session.get(Route, route_id)
        source_chat = await session.get(Chat, source_id)
        target_chat = await session.get(Chat, target_id)
        await delivery_service.deliver_job(
            session,
            db,
            job=job,
            route=route,
            client=fake_delivery_client,
            source_chat=source_chat,
            target_chat=target_chat,
            a_config=ACarryConfig(ad_policy="none"),
            source_entity="src",
            target_entity="dst",
        )

    assert len(fake_delivery_client.forwarded) == 1
    assert fake_delivery_client.forwarded[0]["ids"] == [6001, 6002]


async def test_flush_pending_albums_on_shutdown(db) -> None:
    """停止运行时前要把没入队的相册冲出去，别丢帖子。"""
    route_id, _source_id, _target_id = await _prepare_route(db)
    service = RuntimeService(db)
    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)

    await service._on_new_message(_album_event(7001, 55), [route])
    await service._on_new_message(_album_event(7002, 55), [route])

    await service._flush_pending_albums()

    jobs = await _jobs(db)
    assert len(jobs) == 1
    assert jobs[0].source_message_ids == "7001,7002"


class TestAlbumGroups:
    """历史补齐沿用的分组函数。"""

    @staticmethod
    def _view(message_id: int, grouped_id: int | None) -> MessageView:
        return MessageView(message_id=message_id, grouped_id=grouped_id)

    def test_groups_consecutive_album_items(self) -> None:
        groups = history_service.album_groups(
            [
                self._view(1, None),
                self._view(2, 100),
                self._view(3, 100),
                self._view(4, None),
            ]
        )

        assert [[view.message_id for view in group] for group in groups] == [
            [1],
            [2, 3],
            [4],
        ]

    def test_groups_interleaved_album_items(self) -> None:
        """即使顺序被打乱，同一个 grouped_id 也归到一组。"""
        groups = history_service.album_groups(
            [
                self._view(2, 100),
                self._view(3, 200),
                self._view(4, 100),
            ]
        )

        assert [[view.message_id for view in group] for group in groups] == [
            [2, 4],
            [3],
        ]


class _HistoryClient:
    """只实现拉历史消息需要的 get_messages。"""

    def __init__(self, history: list) -> None:
        self.history = list(history)

    async def get_messages(self, entity, *, ids=None, min_id=0, limit=None, reverse=False):
        selected = [item for item in self.history if item.id > int(min_id)]
        selected.sort(key=lambda item: item.id)
        if limit:
            selected = selected[: int(limit)]
        return selected


async def test_history_backfill_merges_album(db) -> None:
    """历史补齐遇到相册也只产生一个任务。"""
    route_id, _source_id, _target_id = await _prepare_route(db)
    history = [
        fake_message(9001, "相册文案", photo=True, grouped_id=7),
        fake_message(9002, photo=True, grouped_id=7),
        fake_message(9003, photo=True, grouped_id=7),
        fake_message(9004, "单独一条"),
    ]
    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        result = await history_service.sync_route_history(
            session,
            db,
            route=route,
            client=_HistoryClient(history),
            a_config=ACarryConfig(ad_policy="none"),
            source_entity="src",
        )

    assert result["enqueued"] == 2
    jobs = await _jobs(db)
    assert len(jobs) == 2
    assert jobs[0].source_message_ids == "9001,9002,9003"
    assert jobs[0].media_group_id == 7
    assert jobs[1].source_message_ids == "9004"
