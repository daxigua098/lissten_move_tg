"""运行时锁、心跳与控制开关测试。"""

from __future__ import annotations

import os

import pytest

from app.core.heartbeat import heartbeat_age_seconds, read_status, write_status
from app.core.runtime_control import (
    is_paused,
    is_stop_requested,
    read_control,
    set_paused,
    set_stop_requested,
)
from app.core.runtime_lock import RuntimeLock, RuntimeLockError


def test_lock_writes_pid_and_releases(tmp_path) -> None:
    path = tmp_path / "runtime.lock"
    lock = RuntimeLock(path)

    payload = lock.acquire()

    assert payload["pid"] == os.getpid()
    assert path.is_file()
    assert lock.acquired is True

    lock.release()
    assert not path.exists()
    assert lock.acquired is False


def test_stale_lock_is_replaced(tmp_path) -> None:
    from app.core.paths import write_json_atomic

    path = tmp_path / "runtime.lock"
    write_json_atomic(path, {"pid": 987654, "proc_start_time": 1.0})  # 不存在的 PID

    lock = RuntimeLock(path)
    payload = lock.acquire()

    assert payload["pid"] == os.getpid()
    lock.release()


def test_live_process_blocks_start(tmp_path, monkeypatch) -> None:
    from app.core.paths import write_json_atomic

    path = tmp_path / "runtime.lock"
    write_json_atomic(path, {"pid": 4242, "proc_start_time": 111.0})

    monkeypatch.setattr("app.core.runtime_lock.psutil.pid_exists", lambda pid: True)
    monkeypatch.setattr(
        "app.core.runtime_lock.RuntimeLock._process_start_time",
        staticmethod(lambda pid: 111.0),
    )

    with pytest.raises(RuntimeLockError):
        RuntimeLock(path).acquire()


def test_pid_reuse_is_detected(tmp_path, monkeypatch) -> None:
    from app.core.paths import write_json_atomic

    path = tmp_path / "runtime.lock"
    write_json_atomic(path, {"pid": 4242, "proc_start_time": 111.0})

    monkeypatch.setattr("app.core.runtime_lock.psutil.pid_exists", lambda pid: True)
    monkeypatch.setattr(
        "app.core.runtime_lock.RuntimeLock._process_start_time",
        staticmethod(lambda pid: 999.0),  # 启动时间不同 → PID 被复用
    )

    lock = RuntimeLock(path)
    lock.acquire()
    lock.release()


def test_heartbeat_write_and_age(tmp_path) -> None:
    path = tmp_path / "runtime_status.json"

    write_status(path, {"status": "running", "queue_size": 3})
    status = read_status(path)

    assert status is not None
    assert status["status"] == "running"
    assert status["queue_size"] == 3
    age = heartbeat_age_seconds(status)
    assert age is not None and age < 30


def test_heartbeat_age_none_when_missing(tmp_path) -> None:
    assert heartbeat_age_seconds(None) is None
    assert heartbeat_age_seconds({"status": "running"}) is None


def test_control_switches(tmp_path) -> None:
    path = tmp_path / "runtime_control.json"

    assert read_control(path) == {"paused": False, "stop_requested": False, "updated_at": None}

    set_paused(path, True)
    assert is_paused(path) is True

    set_stop_requested(path, True)
    assert is_stop_requested(path) is True

    set_paused(path, False)
    set_stop_requested(path, False)
    assert is_paused(path) is False
    assert is_stop_requested(path) is False
