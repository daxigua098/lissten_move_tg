"""跨进程运行时锁：防止同时启动多个搬运实例。"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

from app.core.paths import ensure_dir, read_json, write_json_atomic

try:  # psutil 用于可靠判断 PID 是否存活
    import psutil
except ImportError:  # pragma: no cover - 缺依赖时退化为不判断
    psutil = None


class RuntimeLockError(RuntimeError):
    """已有实例在运行。"""


class RuntimeLock:
    """基于「PID + 进程启动时间」的运行时锁。"""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._acquired = False

    def acquire(self) -> dict[str, Any]:
        """获取锁；已有存活实例时抛 RuntimeLockError。"""
        ensure_dir(self.path.parent)
        holder = read_json(self.path)
        if holder and self._is_alive(holder):
            pid = holder.get("pid")
            raise RuntimeLockError(f"已有搬运实例在运行（PID {pid}），请先停止它")

        payload = {
            "pid": os.getpid(),
            "proc_start_time": self._process_start_time(os.getpid()),
            "started_at": self._now_iso(),
        }
        write_json_atomic(self.path, payload)
        self._acquired = True
        return payload

    def release(self) -> None:
        """释放锁（只清理自己持有的锁）。"""
        holder = read_json(self.path)
        if holder and holder.get("pid") == os.getpid():
            with contextlib.suppress(OSError):
                self.path.unlink()
        self._acquired = False

    @property
    def acquired(self) -> bool:
        """是否已持有。"""
        return self._acquired

    def __enter__(self) -> RuntimeLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.release()

    def _is_alive(self, holder: dict[str, Any]) -> bool:
        pid = holder.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            return False
        if pid == os.getpid():
            return False
        if psutil is None:
            return True
        if not psutil.pid_exists(pid):
            return False
        # PID 可能被复用：比对进程启动时间
        recorded = holder.get("proc_start_time")
        current = self._process_start_time(pid)
        if recorded and current and abs(float(recorded) - float(current)) > 1:
            return False
        return True

    @staticmethod
    def _process_start_time(pid: int) -> float | None:
        if psutil is None:
            return None
        try:
            return float(psutil.Process(pid).create_time())
        except Exception:  # noqa: BLE001 - 拿不到启动时间就不做比对
            return None

    @staticmethod
    def _now_iso() -> str:
        from datetime import UTC, datetime

        return datetime.now(UTC).isoformat(timespec="seconds")
