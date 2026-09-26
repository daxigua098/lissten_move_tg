"""内容分级：词表判定、探测写入与人工锁定（F-R22）。"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.core.resource_probe import compute_metrics, detect_content_rating
from app.db.models import RATING_NORMAL, RATING_SENSITIVE, RATING_UNKNOWN, TgResource
from app.db.session import session_scope
from app.services import resource_service
from app.services.resource_service import ResourceRef


def _msg(text: str) -> SimpleNamespace:
    """能被 compute_metrics 读到的消息替身（字段名与 MessageView 对齐）。"""
    return SimpleNamespace(
        text=text,
        date=datetime(2026, 9, 25, 10, 0, tzinfo=UTC),
        sender_id=555,
        sender_is_bot=False,
    )


def test_detect_content_rating_by_words() -> None:
    assert detect_content_rating("港澳博彩交流群") == RATING_SENSITIVE
    assert detect_content_rating("高薪兼职日结") == RATING_NORMAL
    # 没有可用文本时不下结论，留给探测复判
    assert detect_content_rating() == RATING_UNKNOWN
    assert detect_content_rating("") == RATING_UNKNOWN
    assert detect_content_rating(None) == RATING_UNKNOWN
    # 大小写与英文词同样命中
    assert detect_content_rating("NSFW content") == RATING_SENSITIVE


def test_compute_metrics_carries_rating() -> None:
    sensitive = compute_metrics(
        [_msg("真人荷官在线发牌，返水最高")],
        title="某某娱乐城",
        about=None,
    )
    normal = compute_metrics(
        [_msg("今天面试了三个人，都不错")],
        title="求职交流群",
        about=None,
    )
    # 没有消息、只有标题时也要给个初判
    empty = compute_metrics([], title="求职交流群")

    assert sensitive.content_rating == RATING_SENSITIVE
    assert normal.content_rating == RATING_NORMAL
    assert empty.content_rating == RATING_NORMAL


async def test_manual_rating_is_not_overwritten(db) -> None:
    async with session_scope() as session:
        outcome = await resource_service.upsert_resource(
            session,
            ResourceRef(title="某某娱乐城", username="casino_chat"),
        )
        resource = outcome.resource
        # 入库时按标题粗判成敏感
        assert resource.content_rating == RATING_UNKNOWN
        await resource_service.update_manual_fields(
            session,
            resource,
            content_rating=RATING_NORMAL,
        )

    async with session_scope() as session:
        row = await session.get(TgResource, resource.id)
        metrics = compute_metrics(
            [_msg("真人荷官在线发牌")],
            title="某某娱乐城",
        )
        await resource_service.apply_metrics(session, row, metrics)
        row = await session.get(TgResource, row.id)

    # 人工改成常规之后，探测不再把它改回敏感
    assert row.content_rating == RATING_NORMAL
    assert "content_rating" in resource_service.load_locked(row)


async def test_directory_coarse_rating_only_fills_unknown(db) -> None:
    async with session_scope() as session:
        outcome = await resource_service.upsert_resource(
            session,
            ResourceRef(
                title="求职交流群",
                tg_id=700700,
                username="job_ok",
                content_rating=RATING_NORMAL,
            ),
            discovered_by="directory",
        )
        resource_id = outcome.resource.id

    async with session_scope() as session:
        row = await session.get(TgResource, resource_id)
        assert row.content_rating == RATING_NORMAL
        # 已经是 normal（探测或前一次粗判的结论），下一次粗判不会把它改掉
        await resource_service.upsert_resource(
            session,
            ResourceRef(
                title="求职交流群",
                tg_id=700700,
                username="job_ok",
                content_rating=RATING_SENSITIVE,
            ),
            discovered_by="directory",
        )
        row = await session.get(TgResource, resource_id)

    assert row.content_rating == RATING_NORMAL
