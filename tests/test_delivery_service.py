"""投递引擎测试：幂等入队、退避重试、广告策略与状态流转。"""

from __future__ import annotations

from datetime import timedelta

from conftest import SimpleNamespace, fake_message

from app.core.route_config import ACarryConfig
from app.db.base import as_utc, utc_now
from app.db.models import (
    JOB_FAILED,
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_RETRYING,
    JOB_SUCCESS,
    AdAsset,
    DeliveryJob,
    TenantChat,
)
from app.db.session import session_scope
from app.services import chat_service, delivery_service, route_service


async def _prepare_route(db, *, targets: int = 1, a_config: dict | None = None):
    """建一条 A 线：1 个源 + N 个接收目标。"""
    from app.core.telegram_client import ChatProfile

    async with session_scope() as session:
        source = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=7001,
                chat_type="channel",
                title="素材源",
                username="src_ch",
                is_private=False,
            ),
        )
        await chat_service.set_source(session, source)
        target_ids = []
        for index in range(targets):
            target = await chat_service.upsert_chat_from_profile(
                session,
                ChatProfile(
                    tg_id=7100 + index,
                    chat_type="channel",
                    title=f"目标{index}",
                    username=f"target{index}",
                    is_private=False,
                ),
            )
            await chat_service.set_target(session, target)
            target_ids.append(target.id)
        route = await route_service.create_route(
            session,
            name="测试线路",
            source_chat_id=source.id,
            business_type="A",
            target_chat_ids=target_ids,
            a_config=a_config or {"ad_policy": "none"},
        )
        return route.id, source.id, target_ids


async def test_enqueue_is_idempotent(db) -> None:
    route_id, source_id, target_ids = await _prepare_route(db, targets=2)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        first = await delivery_service.enqueue_message(
            session,
            route=route,
            target_chat_ids=target_ids,
            source_chat_id=source_id,
            source_message_id=101,
        )
        second = await delivery_service.enqueue_message(
            session,
            route=route,
            target_chat_ids=target_ids,
            source_chat_id=source_id,
            source_message_id=101,
        )

    assert len(first) == 2
    assert second == []  # 幂等键命中，不重复入队


async def test_next_ready_job_respects_retry_time(db) -> None:
    route_id, source_id, target_ids = await _prepare_route(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        await delivery_service.enqueue_message(
            session,
            route=route,
            target_chat_ids=target_ids,
            source_chat_id=source_id,
            source_message_id=201,
        )

    async with session_scope() as session:
        job = await delivery_service.next_ready_job(session)
        assert job is not None and job.source_message_id == 201
        job.attempt_count += 1  # 真实流程里由投递入口递增
        await delivery_service.mark_failure(session, job, error="第一次失败", retry_base_seconds=60)

    async with session_scope() as session:
        job = await session.get(DeliveryJob, job.id)
        assert job.status == JOB_RETRYING
        assert job.attempt_count == 1
        # SQLite 读回来是 naive UTC，比较前统一标注时区
        assert as_utc(job.next_retry_at) > utc_now() + timedelta(seconds=30)
        # 还没到重试时间 → 不参与投递
        assert await delivery_service.next_ready_job(session) is None


async def test_failure_exhausts_attempts(db) -> None:
    route_id, source_id, target_ids = await _prepare_route(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        jobs = await delivery_service.enqueue_message(
            session,
            route=route,
            target_chat_ids=target_ids,
            source_chat_id=source_id,
            source_message_id=301,
            max_attempts=2,
        )
        job_id = jobs[0].id

    for _ in range(2):
        async with session_scope() as session:
            job = await session.get(DeliveryJob, job_id)
            job.attempt_count += 1
            await delivery_service.mark_failure(session, job, error="一直失败")

    async with session_scope() as session:
        job = await session.get(DeliveryJob, job_id)
        assert job.status == JOB_FAILED
        assert job.next_retry_at is None
        assert await delivery_service.retry_failed_jobs(session) == 1

    async with session_scope() as session:
        job = await session.get(DeliveryJob, job_id)
        assert job.status == JOB_PENDING
        assert job.attempt_count == 0


async def test_deliver_job_success_with_ad(
    db,
    fake_delivery_client,
) -> None:
    route_id, source_id, target_ids = await _prepare_route(
        db,
        a_config={"ad_policy": "none"},
    )
    async with session_scope() as session:
        asset = AdAsset(
            name="渠道A",
            text="{源名} · 每日更新",
            link_url="https://t.me/x",
            link_text="点我",
        )
        session.add(asset)
        await session.commit()
        await session.refresh(asset)
        asset_id = asset.id

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        route.a_config = ACarryConfig(
            ad_policy="every",
            ad_asset_id=asset_id,
        ).model_dump_json()
        await session.commit()
        jobs = await delivery_service.enqueue_message(
            session,
            route=route,
            target_chat_ids=target_ids,
            source_chat_id=source_id,
            source_message_id=401,
        )
        job_id = jobs[0].id

    async with session_scope() as session:
        job = await session.get(DeliveryJob, job_id)
        route = await route_service.get_route(session, route_id)
        source_chat = await session.get(TenantChat, source_id)
        target_chat = await session.get(TenantChat, target_ids[0])
        ad_asset = await session.get(AdAsset, asset_id)

        await delivery_service.deliver_job(
            session,
            db,
            job=job,
            route=route,
            client=fake_delivery_client,
            source_chat=source_chat,
            target_chat=target_chat,
            a_config=ACarryConfig(ad_policy="every", ad_asset_id=asset_id),
            ad_asset=ad_asset,
            source_entity="src",
            target_entity="dst",
        )

    assert fake_delivery_client.forwarded[0]["drop_author"] is True  # copy 模式去来源
    assert fake_delivery_client.forwarded[0]["ids"] == [401]
    assert any("每日更新" in item["text"] for item in fake_delivery_client.sent)
    assert fake_delivery_client.sent[0]["text"].startswith("素材源")  # {源名} 已替换

    async with session_scope() as session:
        job = await session.get(DeliveryJob, job_id)
        assert job.status == JOB_SUCCESS
        assert job.target_message_id == 9001
        assert job.ad_applied is True
        assert job.sent_at is not None


async def test_deliver_job_failure_marks_retry(db, fake_delivery_client) -> None:
    route_id, source_id, target_ids = await _prepare_route(db)
    fake_delivery_client.fail_times = 1

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        jobs = await delivery_service.enqueue_message(
            session,
            route=route,
            target_chat_ids=target_ids,
            source_chat_id=source_id,
            source_message_id=501,
        )
        job_id = jobs[0].id

    async with session_scope() as session:
        job = await session.get(DeliveryJob, job_id)
        route = await route_service.get_route(session, route_id)
        source_chat = await session.get(TenantChat, source_id)
        target_chat = await session.get(TenantChat, target_ids[0])
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

    async with session_scope() as session:
        job = await session.get(DeliveryJob, job_id)
        assert job.status == JOB_RETRYING
        assert job.attempt_count == 1
        assert "模拟发送失败" in job.last_error


async def test_ad_frequency_counts_from_today(db) -> None:
    route_id, source_id, target_ids = await _prepare_route(db)
    config = ACarryConfig(ad_policy="nth", ad_nth=3, ad_asset_id=1)

    async with session_scope() as session:
        assert (
            await delivery_service.should_attach_ad(
                session,
                route_id=route_id,
                target_chat_id=target_ids[0],
                a_config=config,
            )
            is False
        )
        # 已有 2 条成功 → 下一条正好是第 3 条
        for index in range(2):
            session.add(
                DeliveryJob(
                    route_id=route_id,
                    source_chat_id=source_id,
                    source_message_id=600 + index,
                    target_chat_id=target_ids[0],
                    status=JOB_SUCCESS,
                    sent_at=utc_now(),
                )
            )
        await session.commit()
        assert (
            await delivery_service.should_attach_ad(
                session,
                route_id=route_id,
                target_chat_id=target_ids[0],
                a_config=config,
            )
            is True
        )


async def test_cancel_pending_jobs(db) -> None:
    route_id, source_id, target_ids = await _prepare_route(db)

    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        await delivery_service.enqueue_message(
            session,
            route=route,
            target_chat_ids=target_ids,
            source_chat_id=source_id,
            source_message_id=701,
        )
        assert await delivery_service.cancel_pending_jobs(session) == 1
        stats = await delivery_service.job_stats(session)

    assert stats["skipped"] == 1
    assert stats["pending"] == 0


async def test_repost_mode_uses_cleaned_caption(db, fake_delivery_client) -> None:
    route_id, source_id, target_ids = await _prepare_route(
        db,
        a_config={"ad_policy": "none", "text_mode": "clean"},
    )
    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        jobs = await delivery_service.enqueue_message(
            session,
            route=route,
            target_chat_ids=target_ids,
            source_chat_id=source_id,
            source_message_id=801,
        )
        job_id = jobs[0].id

    async with session_scope() as session:
        job = await session.get(DeliveryJob, job_id)
        route = await route_service.get_route(session, route_id)
        source_chat = await session.get(TenantChat, source_id)
        target_chat = await session.get(TenantChat, target_ids[0])
        await delivery_service.deliver_job(
            session,
            db,
            job=job,
            route=route,
            client=fake_delivery_client,
            source_chat=source_chat,
            target_chat=target_chat,
            a_config=ACarryConfig(ad_policy="none", text_mode="clean"),
            source_entity="src",
            target_entity="dst",
            source_messages=[fake_message(801, "正文 https://ad.example.com", photo=True)],
            caption="正文",
        )

    # 净化模式走重新上传，而不是转发
    assert fake_delivery_client.forwarded == []
    assert fake_delivery_client.sent[0]["caption"] == "正文"


async def test_album_job_forwards_all_media_in_one_call(db, fake_delivery_client) -> None:
    """一条相册任务只发一次，全部媒体 ID 一起转发（不再逐张刷屏）。"""
    route_id, source_id, target_ids = await _prepare_route(db)
    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        jobs = await delivery_service.enqueue_message(
            session,
            route=route,
            target_chat_ids=target_ids,
            source_chat_id=source_id,
            source_message_id=1103,
            media_group_id=555,
            source_message_ids=[1101, 1102, 1103],
        )
        job_id = jobs[0].id

    async with session_scope() as session:
        job = await session.get(DeliveryJob, job_id)
        route = await route_service.get_route(session, route_id)
        source_chat = await session.get(TenantChat, source_id)
        target_chat = await session.get(TenantChat, target_ids[0])
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

    assert len(fake_delivery_client.forwarded) == 1  # 只有一次发送
    assert fake_delivery_client.forwarded[0]["ids"] == [1101, 1102, 1103]


async def test_album_repost_mode_sends_as_one_album(db, fake_delivery_client) -> None:
    """净化模式下，整组媒体作为一条相册发出。"""
    route_id, source_id, target_ids = await _prepare_route(
        db,
        a_config={"ad_policy": "none", "text_mode": "clean"},
    )
    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        jobs = await delivery_service.enqueue_message(
            session,
            route=route,
            target_chat_ids=target_ids,
            source_chat_id=source_id,
            source_message_id=1203,
            media_group_id=777,
            source_message_ids=[1201, 1202, 1203],
        )
        job_id = jobs[0].id

    album = [
        fake_message(1201, "正文 https://ad.example.com", photo=True),
        fake_message(1202, photo=True),
        fake_message(1203, photo=True),
    ]
    async with session_scope() as session:
        job = await session.get(DeliveryJob, job_id)
        route = await route_service.get_route(session, route_id)
        source_chat = await session.get(TenantChat, source_id)
        target_chat = await session.get(TenantChat, target_ids[0])
        await delivery_service.deliver_job(
            session,
            db,
            job=job,
            route=route,
            client=fake_delivery_client,
            source_chat=source_chat,
            target_chat=target_chat,
            a_config=ACarryConfig(ad_policy="none", text_mode="clean"),
            source_entity="src",
            target_entity="dst",
            source_messages=album,
            caption="正文",
        )

    assert fake_delivery_client.forwarded == []  # 没有逐条转发
    assert len(fake_delivery_client.sent) == 1  # 只发了一条
    assert fake_delivery_client.sent[0]["file"] == album  # 整组媒体一起发
    assert fake_delivery_client.sent[0]["caption"] == "正文"


async def test_requeue_stale_jobs_puts_processing_back(db) -> None:
    """异常退出留下的「处理中」任务要能重新入队。"""
    route_id, source_id, target_ids = await _prepare_route(db)
    async with session_scope() as session:
        route = await route_service.get_route(session, route_id)
        jobs = await delivery_service.enqueue_message(
            session,
            route=route,
            target_chat_ids=target_ids,
            source_chat_id=source_id,
            source_message_id=906,
        )
        job = await session.get(DeliveryJob, jobs[0].id)
        job.status = JOB_PROCESSING
        await session.commit()

    async with session_scope() as session:
        requeued = await delivery_service.requeue_stale_jobs(session)
        job = await session.get(DeliveryJob, jobs[0].id)

    assert requeued == 1
    assert job.status == JOB_PENDING


def test_simple_namespace_available() -> None:
    assert SimpleNamespace(x=1).x == 1
