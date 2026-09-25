"""历史补齐测试：目标级水位线保证不重复搬运。"""

from __future__ import annotations

from conftest import fake_message
from sqlalchemy import select

from app.core.route_config import ACarryConfig
from app.db.models import RouteTargetProgress
from app.db.session import session_scope
from app.services import chat_service, history_service, route_service


async def _prepare(db, *, targets: int = 2, a_config: dict | None = None):
    from app.core.telegram_client import ChatProfile

    async with session_scope() as session:
        source = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=7201,
                chat_type="channel",
                title="素材源",
                username="hist_src",
                is_private=False,
            ),
        )
        await chat_service.set_source(session, source)
        target_ids = []
        for index in range(targets):
            target = await chat_service.upsert_chat_from_profile(
                session,
                ChatProfile(
                    tg_id=7300 + index,
                    chat_type="channel",
                    title=f"目标{index}",
                    username=f"hist_target{index}",
                    is_private=False,
                ),
            )
            await chat_service.set_target(session, target)
            target_ids.append(target.id)
        route = await route_service.create_route(
            session,
            name="历史线路",
            source_chat_id=source.id,
            business_type="A",
            target_chat_ids=target_ids,
            a_config=a_config or {"ad_policy": "none"},
        )
        return route.id, source.id, target_ids


async def test_history_only_fills_missing_per_target(db, fake_delivery_client) -> None:
    route_id, source_id, target_ids = await _prepare(db, targets=2)
    fake_delivery_client.history = [
        fake_message(101, "第一条", photo=True),
        fake_message(102, "第二条", photo=True),
        fake_message(103, "第三条", photo=True),
    ]

    # 目标 1 已经搬到了 103，目标 2 还停在 0
    async with session_scope() as session:
        row = await session.get(RouteTargetProgress, (route_id, target_ids[0]))
        assert row is not None
        row.last_delivered_message_id = 103
        await session.commit()

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        result = await history_service.sync_route_history(
            session,
            db,
            route=route,
            client=fake_delivery_client,
            a_config=ACarryConfig(ad_policy="none"),
            source_entity="src",
        )

    # 只有目标 2 需要补 3 条，目标 1 一条都不重复
    assert result["enqueued"] == 3
    assert result["inspected"] == 3

    async with session_scope() as session:
        rows = list(
            await session.scalars(
                select(RouteTargetProgress).where(RouteTargetProgress.route_id == route_id)
            )
        )
    progress = {row.target_chat_id: row.last_delivered_message_id for row in rows}
    assert progress[target_ids[0]] == 103  # 老目标水位线不动
    assert progress[target_ids[1]] == 103  # 新目标补齐到最新


async def test_history_skips_filtered_content(db, fake_delivery_client) -> None:
    route_id, source_id, target_ids = await _prepare(
        db,
        targets=1,
        a_config={"ad_policy": "none", "content_types": ["photo", "video"]},
    )
    fake_delivery_client.history = [
        fake_message(201, "纯文本不该搬"),
        fake_message(202, "带图", photo=True),
        fake_message(203, "置顶广告", photo=True, pinned=True),
    ]

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        result = await history_service.sync_route_history(
            session,
            db,
            route=route,
            client=fake_delivery_client,
            a_config=ACarryConfig(
                ad_policy="none",
                content_types=["photo", "video"],
                skip_pinned=True,
            ),
            source_entity="src",
        )

    assert result["enqueued"] == 1
    assert result["filtered"] == 1


async def test_history_drops_empty_text_under_drop_policy(db, fake_delivery_client) -> None:
    route_id, source_id, target_ids = await _prepare(db, targets=1)
    fake_delivery_client.history = [fake_message(301, "加我微信")]  # 整行被推广词剥掉

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        result = await history_service.sync_route_history(
            session,
            db,
            route=route,
            client=fake_delivery_client,
            a_config=ACarryConfig(
                ad_policy="none",
                content_types=["text"],
                promo_blacklist=["加我微信"],
                empty_text_policy="drop",
            ),
            source_entity="src",
        )

    assert result["enqueued"] == 0
    assert result["filtered"] == 1
