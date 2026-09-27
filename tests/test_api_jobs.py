"""投递任务与运行时控制接口测试。"""

from __future__ import annotations

import os

from conftest import ADMIN_API_TOKEN, auth_header, login

from app.core.telegram_client import ChatProfile
from app.db.models import JOB_FAILED, JOB_PENDING, JOB_SKIPPED, DeliveryJob
from app.db.session import session_scope
from app.services import chat_service, route_service


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


async def _prepare_route_with_job(admin_client, api_config, *, status: str = JOB_FAILED):
    """建一条 A 线并塞一条指定状态的任务。"""
    async with session_scope() as session:
        source = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=9101,
                chat_type="channel",
                title="任务源",
                username="job_src",
                is_private=False,
            ),
        )
        await chat_service.set_source(session, source)
        target = await chat_service.upsert_chat_from_profile(
            session,
            ChatProfile(
                tg_id=9102,
                chat_type="channel",
                title="任务目标",
                username="job_target",
                is_private=False,
            ),
        )
        await chat_service.set_target(session, target)
        route = await route_service.create_route(
            session,
            name="任务线路",
            source_chat_id=source.id,
            business_type="A",
            target_chat_ids=[target.id],
            a_config={"ad_policy": "none"},
        )
        job = DeliveryJob(
            route_id=route.id,
            source_chat_id=source.id,
            source_message_id=1234,
            target_chat_id=target.id,
            status=status,
            attempt_count=5,
            last_error="模拟失败",
        )
        session.add(job)
        await session.commit()
        await session.refresh(job)
        return job.id


async def test_job_list_requires_auth(admin_client) -> None:
    anonymous = await admin_client.get("/api/jobs")
    assert anonymous.status_code == 401

    token = (await login(admin_client)).json()["token"]
    ok = await admin_client.get("/api/jobs", headers=auth_header(token))
    assert ok.status_code == 200


async def test_job_list_shows_titles_and_route(admin_client, api_config) -> None:
    job_id = await _prepare_route_with_job(admin_client, api_config)

    response = await admin_client.get("/api/jobs", headers=_headers())

    item = response.json()["items"][0]
    assert item["id"] == job_id
    assert item["route_name"] == "任务线路"
    assert item["source_title"] == "任务源"
    assert item["target_title"] == "任务目标"
    assert item["status"] == JOB_FAILED
    assert item["status_label"] == "失败"
    assert item["last_error"] == "模拟失败"


async def test_retry_failed_resets_jobs(admin_client, api_config) -> None:
    job_id = await _prepare_route_with_job(admin_client, api_config)

    response = await admin_client.post("/api/jobs/retry-failed", headers=_headers())

    assert response.status_code == 200
    assert response.json()["retried"] == 1

    async with session_scope() as session:
        job = await session.get(DeliveryJob, job_id)
        assert job.status == JOB_PENDING
        assert job.attempt_count == 0
        assert job.last_error is None


async def test_retry_and_skip_single_job(admin_client, api_config) -> None:
    job_id = await _prepare_route_with_job(admin_client, api_config)

    retried = await admin_client.post(f"/api/jobs/{job_id}/retry", headers=_headers())
    assert retried.status_code == 200
    assert retried.json()["status"] == JOB_PENDING

    skipped = await admin_client.post(f"/api/jobs/{job_id}/skip", headers=_headers())
    assert skipped.status_code == 200
    assert skipped.json()["status"] == JOB_SKIPPED

    missing = await admin_client.post("/api/jobs/99999/retry", headers=_headers())
    assert missing.status_code == 404


async def test_job_stats_endpoint(admin_client, api_config) -> None:
    await _prepare_route_with_job(admin_client, api_config)

    response = await admin_client.get("/api/jobs/stats", headers=_headers())

    body = response.json()
    assert response.status_code == 200
    assert body["total"] == 1
    failed = next(item for item in body["items"] if item["status"] == JOB_FAILED)
    assert failed["label"] == "失败"
    assert failed["count"] == 1


async def test_runtime_control_flow(admin_client, api_config) -> None:
    paused = await admin_client.post("/api/runtime/pause", headers=_headers())
    assert paused.status_code == 200
    assert paused.json()["paused"] is True

    resumed = await admin_client.post("/api/runtime/resume", headers=_headers())
    assert resumed.json()["paused"] is False
    assert resumed.json()["stop_requested"] is False

    stopped = await admin_client.post("/api/runtime/stop", headers=_headers())
    assert stopped.json()["stop_requested"] is True

    # 收尾：清掉控制标记，避免影响其他用例
    await admin_client.post("/api/runtime/resume", headers=_headers())

    status = await admin_client.get("/api/runtime/status", headers=_headers())
    assert status.status_code == 200
    assert "jobs" in status.json()


async def test_runtime_start_skips_when_already_running(admin_client, api_config) -> None:
    """心跳还活着时不重复拉起进程（避免一个账号被两个运行时抢）。"""
    from app.core.heartbeat import write_status

    write_status(
        api_config.path(api_config.runtime.status_file),
        {"status": "running", "pid": os.getpid()},
    )

    response = await admin_client.post("/api/runtime/start", headers=_headers())

    body = response.json()
    assert response.status_code == 200
    assert body["start"]["started"] is False
    assert body["start"]["reason"] == "already_running"


async def test_system_status_reports_stale_runtime_as_stopped(admin_client, api_config) -> None:
    """心跳过期后总览必须显示未启动；否则用户会以为监听仍在跑。"""
    from datetime import UTC, datetime, timedelta

    from app.core.paths import write_json_atomic

    write_json_atomic(
        api_config.path(api_config.runtime.status_file),
        {
            "status": "running",
            "heartbeat_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(
                timespec="seconds"
            ),
            "pid": 4321,
            "routes": {"sources": 2, "carry": 0, "monitor": 2},
        },
    )

    response = await admin_client.get("/api/system/status", headers=_headers())

    assert response.status_code == 200
    runtime = response.json()["runtime"]
    assert runtime["status"] == "stopped"
    assert runtime["routes"]["monitor"] == 2


async def test_viewer_cannot_control_runtime(admin_client, api_config) -> None:
    from app.db.session import session_scope as scope
    from app.services import user_service

    async with scope() as session:
        await user_service.create_user(
            session,
            api_config,
            username="erge",
            password="Pass1234",
            role="viewer",
            must_change_password=False,
        )
    token = (await login(admin_client, username="erge", password="Pass1234")).json()["token"]

    readable = await admin_client.get("/api/jobs", headers=auth_header(token))
    assert readable.status_code == 200

    forbidden = await admin_client.post("/api/runtime/pause", headers=auth_header(token))
    assert forbidden.status_code == 403

    retry_denied = await admin_client.post("/api/jobs/retry-failed", headers=auth_header(token))
    assert retry_denied.status_code == 403
