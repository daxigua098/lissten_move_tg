"""执行账号客户端池：复用 Telegram 连接。

为什么要池化：每次请求都新建 Telethon 客户端要重新握手（实测 5~8 秒），
而用户点的「让账号加入 / 刷新 / 同步群组池」都是交互动作，等 8 秒建连接
再干活体感很差。常驻连接后这些动作降到 1~3 秒。

**默认关闭（``USE_POOL = False``）**：实测常驻连接会一直占住 Telegram 的 session
文件（SQLite），别的进程就再也连不上它了——报 ``database is locked``，项目里
对应 ``SESSION_LOCK_HINT``（"会话文件正被搬运运行时占用"）。也就是说
「API 常驻连接」与「运行时同时使用同一个账号」二者只能选一个，而运行时必须能跑。
所以这里保留实现但默认关掉：调参数之前先想清楚谁持有账号连接。

真正要提速，要让账号操作统一交给运行时进程执行（IPC），那是另一个层级的改造。

其余约定：

- **测试注入的客户端不进池**：测试用替身没有真实连接语义，池化会串状态。
- 凭据变了（登录成功、账号被删）必须 ``drop`` 掉旧连接，否则会拿着旧 session 干活。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

from loguru import logger

# 默认不用池：常驻连接会锁住 session 文件，运行时就连不上账号了
USE_POOL = False


class AccountClientPool:
    """按账号缓存一条长连接。"""

    def __init__(self) -> None:
        self._clients: dict[int, Any] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, account_id: int) -> asyncio.Lock:
        """同一账号串行取连接，避免两个请求同时建连。"""
        key = int(account_id)
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @staticmethod
    def _is_usable(client: Any) -> bool:
        """连接还在吗（替身 / 未实现的客户端一律当作不可用）。"""
        try:
            return bool(client.is_connected())
        except Exception:  # noqa: BLE001 - 判断不了就当不可用
            return False

    async def get(self, account_id: int, opener: Callable[[], Awaitable[Any]]) -> Any:
        """取连接：能用就直接给，不能用就重开一条。"""
        key = int(account_id)
        cached = self._clients.get(key)
        if cached is not None and self._is_usable(cached):
            return cached
        async with self.lock(key):
            cached = self._clients.get(key)
            if cached is not None and self._is_usable(cached):
                return cached
            if cached is not None:
                await self._close(key)
            client = await opener()
            self._clients[key] = client
            return client

    async def drop(self, account_id: int | None) -> None:
        """丢掉某个账号的连接（凭据变更、账号删除时调用）。"""
        if account_id is None:
            return
        await self._close(int(account_id))

    async def close_all(self) -> None:
        """关掉所有连接（进程退出时调用）。"""
        for key in list(self._clients):
            await self._close(key)

    async def _close(self, key: int) -> None:
        client = self._clients.pop(key, None)
        if client is None:
            return
        with contextlib.suppress(Exception):
            await client.disconnect()
        logger.debug("已释放执行账号连接 #{}", key)


POOL = AccountClientPool()


async def drop_account_client(account_id: int | None) -> None:
    """对外的小工具：凭据变更后清掉缓存连接。"""
    await POOL.drop(account_id)


async def close_account_clients() -> None:
    """进程退出时释放全部连接。"""
    await POOL.close_all()
