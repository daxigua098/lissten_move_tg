"""投递引擎：幂等入队、串行投递、广告附加与失败退避重试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import AppConfig
from app.core.route_config import ACarryConfig
from app.core.telegram_client import (
    forward_to_target,
    render_ad_text,
    repost_message,
    send_ad,
)
from app.db.base import utc_now
from app.db.models import (
    JOB_FAILED,
    JOB_PENDING,
    JOB_PROCESSING,
    JOB_RETRYING,
    JOB_SKIPPED,
    JOB_SUCCESS,
    AdAsset,
    Chat,
    DeliveryJob,
    Route,
)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BASE_SECONDS = 5


async def enqueue_message(
    session: AsyncSession,
    *,
    route: Route,
    target_chat_ids: list[int],
    source_chat_id: int,
    source_message_id: int,
    media_group_id: int | None = None,
    source_message_ids: list[int] | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> list[DeliveryJob]:
    """为一条源消息创建投递任务；已存在的幂等键自动跳过。"""
    created: list[DeliveryJob] = []
    for target_chat_id in target_chat_ids:
        existing = await session.scalar(
            select(DeliveryJob.id).where(
                DeliveryJob.source_chat_id == source_chat_id,
                DeliveryJob.source_message_id == source_message_id,
                DeliveryJob.target_chat_id == target_chat_id,
            )
        )
        if existing is not None:
            continue
        job = DeliveryJob(
            route_id=route.id,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            source_message_ids=(
                ",".join(str(item) for item in source_message_ids) if source_message_ids else None
            ),
            media_group_id=media_group_id,
            target_chat_id=target_chat_id,
            status=JOB_PENDING,
            max_attempts=max_attempts,
        )
        session.add(job)
        created.append(job)
    if created:
        await session.commit()
        for job in created:
            await session.refresh(job)
    return created


async def next_ready_job(
    session: AsyncSession,
    *,
    now: datetime | None = None,
) -> DeliveryJob | None:
    """取下一个可投递任务（pending 或到点的 retrying），按 ID 保证顺序。"""
    moment = now or utc_now()
    statement = (
        select(DeliveryJob)
        .where(
            DeliveryJob.status.in_((JOB_PENDING, JOB_RETRYING)),
            (DeliveryJob.next_retry_at.is_(None)) | (DeliveryJob.next_retry_at <= moment),
        )
        .order_by(DeliveryJob.id)
        .limit(1)
    )
    return await session.scalar(statement)


async def skip_job(session: AsyncSession, job: DeliveryJob, *, reason: str) -> DeliveryJob:
    """把任务标记为跳过（如源消息已删除、内容被过滤）。"""
    job.status = JOB_SKIPPED
    job.last_error = reason[:500]
    job.next_retry_at = None
    await session.commit()
    await session.refresh(job)
    return job


async def mark_failure(
    session: AsyncSession,
    job: DeliveryJob,
    *,
    error: str,
    retry_base_seconds: int = DEFAULT_RETRY_BASE_SECONDS,
) -> DeliveryJob:
    """记录失败：未达上限则安排退避重试，达到上限标记失败。

    注意 attempt_count 由投递入口递增，这里只负责判断与排期。
    """
    job.last_error = error[:500]
    if job.attempt_count >= job.max_attempts:
        job.status = JOB_FAILED
        job.next_retry_at = None
    else:
        job.status = JOB_RETRYING
        delay = retry_base_seconds * (2 ** (job.attempt_count - 1))
        job.next_retry_at = utc_now() + timedelta(seconds=delay)
    await session.commit()
    await session.refresh(job)
    return job


async def today_success_count(
    session: AsyncSession,
    *,
    route_id: int,
    target_chat_id: int,
    now: datetime | None = None,
) -> int:
    """今日该线路+目标已成功投递的条数（广告频率计数按天重置）。"""
    moment = now or utc_now()
    start = moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    statement = (
        select(func.count())
        .select_from(DeliveryJob)
        .where(
            DeliveryJob.route_id == route_id,
            DeliveryJob.target_chat_id == target_chat_id,
            DeliveryJob.status == JOB_SUCCESS,
            DeliveryJob.sent_at >= start,
        )
    )
    return int(await session.scalar(statement) or 0)


async def should_attach_ad(
    session: AsyncSession,
    *,
    route_id: int,
    target_chat_id: int,
    a_config: ACarryConfig,
    now: datetime | None = None,
) -> bool:
    """按广告策略判断本条是否要挂广告。"""
    if a_config.ad_policy == "none" or a_config.ad_asset_id is None:
        return False
    if a_config.ad_policy == "every":
        return True
    sent = await today_success_count(
        session,
        route_id=route_id,
        target_chat_id=target_chat_id,
        now=now,
    )
    nth = max(1, a_config.ad_nth)
    return (sent + 1) % nth == 0


async def deliver_job(
    session: AsyncSession,
    config: AppConfig,
    *,
    job: DeliveryJob,
    route: Route,
    client: Any,
    source_chat: Chat,
    target_chat: Chat,
    a_config: ACarryConfig,
    ad_asset: AdAsset | None = None,
    source_entity: Any = None,
    target_entity: Any = None,
    source_message: Any = None,
    caption: str | None = None,
) -> DeliveryJob:
    """执行一次投递：转发消息 → 按策略附加广告 → 更新状态。"""
    job.status = JOB_PROCESSING
    job.attempt_count += 1
    await session.commit()

    message_ids = _message_ids(job)
    try:
        if a_config.text_mode == "clean" and source_message is not None:
            # 净化后的文案只能通过重新上传生效，转发无法修改原文
            result = await repost_message(
                client,
                target_entity=target_entity,
                message=source_message,
                caption=caption,
            )
        else:
            result = await forward_to_target(
                client,
                source_entity=source_entity,
                target_entity=target_entity,
                message_ids=message_ids,
                drop_author=a_config.transfer_mode == "copy",
            )
        job.target_message_id = _first_message_id(result)
        job.status = JOB_SUCCESS
        job.sent_at = utc_now()
        job.last_error = None
        job.next_retry_at = None

        if ad_asset is not None and await should_attach_ad(
            session,
            route_id=route.id,
            target_chat_id=target_chat.id,
            a_config=a_config,
        ):
            await _send_ad_message(
                config,
                client=client,
                target_entity=target_entity,
                route=route,
                source_chat=source_chat,
                ad_asset=ad_asset,
            )
            job.ad_applied = True

        await session.commit()
        await session.refresh(job)
        return job
    except Exception as exc:  # noqa: BLE001 - 失败原因要落库供排障
        await session.rollback()
        return await mark_failure(session, job, error=str(exc))


async def _send_ad_message(
    config: AppConfig,
    *,
    client: Any,
    target_entity: Any,
    route: Route,
    source_chat: Chat,
    ad_asset: AdAsset,
) -> None:
    text = render_ad_text(
        ad_asset.text,
        source_title=source_chat.title or source_chat.username or "",
        route_name=route.name,
    )
    image_path = config.path(ad_asset.image_path) if ad_asset.image_path else None
    await send_ad(
        client,
        target_entity=target_entity,
        text=text,
        image_path=str(image_path) if image_path else None,
        link_url=ad_asset.link_url,
        link_text=ad_asset.link_text,
    )


async def advance_progress(
    session: AsyncSession,
    *,
    route_id: int,
    target_chat_id: int,
    source_message_id: int,
) -> None:
    """推进目标级水位线（只在成功投递后调用）。"""
    from app.db.models import RouteTargetProgress

    progress = await session.get(RouteTargetProgress, (route_id, target_chat_id))
    if progress is None:
        progress = RouteTargetProgress(route_id=route_id, target_chat_id=target_chat_id)
        session.add(progress)
    if source_message_id > progress.last_delivered_message_id:
        progress.last_delivered_message_id = source_message_id
    progress.last_run_at = utc_now()
    await session.commit()


async def job_stats(session: AsyncSession) -> dict[str, int]:
    """按状态统计任务数。"""
    rows = await session.execute(
        select(DeliveryJob.status, func.count(DeliveryJob.id)).group_by(DeliveryJob.status)
    )
    known = (JOB_PENDING, JOB_RETRYING, JOB_SUCCESS, JOB_FAILED, JOB_SKIPPED)
    stats = {status: 0 for status in known}
    for status, count in rows.all():
        stats[str(status)] = int(count)
    return stats


async def retry_failed_jobs(session: AsyncSession) -> int:
    """把失败任务重新置为待投递。"""
    from sqlalchemy import update

    result = await session.execute(
        update(DeliveryJob)
        .where(DeliveryJob.status == JOB_FAILED)
        .values(status=JOB_PENDING, attempt_count=0, next_retry_at=None, last_error=None)
    )
    await session.commit()
    return int(result.rowcount or 0)


async def cancel_pending_jobs(session: AsyncSession) -> int:
    """把待投递与重试中的任务置为跳过（用于停止全部）。"""
    from sqlalchemy import update

    result = await session.execute(
        update(DeliveryJob)
        .where(DeliveryJob.status.in_((JOB_PENDING, JOB_RETRYING)))
        .values(status=JOB_SKIPPED, next_retry_at=None, last_error="已被手动停止")
    )
    await session.commit()
    return int(result.rowcount or 0)


def _message_ids(job: DeliveryJob) -> list[int]:
    if job.source_message_ids:
        values = [item.strip() for item in job.source_message_ids.split(",") if item.strip()]
        if values:
            return [int(item) for item in values]
    return [int(job.source_message_id)]


def _first_message_id(result: Any) -> int | None:
    if result is None:
        return None
    if isinstance(result, (list, tuple)):
        return _first_message_id(result[0]) if result else None
    message_id = getattr(result, "id", None)
    return int(message_id) if message_id is not None else None
