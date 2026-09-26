"""执行账号连接池：复用、失效重建与释放。"""

from __future__ import annotations

from app.core.account_client_pool import AccountClientPool


class FakeClient:
    """带连接状态的客户端替身。"""

    def __init__(self) -> None:
        self.connected = True
        self.disconnected = False

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnected = True


def _opener(made: list[FakeClient]):
    async def open_it() -> FakeClient:
        client = FakeClient()
        made.append(client)
        return client

    return open_it


async def test_pool_reuses_live_connection() -> None:
    """同一个账号第二次取连接要拿到同一条，不再重新握手。"""
    pool = AccountClientPool()
    made: list[FakeClient] = []

    first = await pool.get(1, _opener(made))
    second = await pool.get(1, _opener(made))

    assert first is second
    assert len(made) == 1
    # 不同账号各拿一条
    other = await pool.get(2, _opener(made))
    assert other is not first
    assert len(made) == 2


async def test_pool_rebuilds_when_connection_dropped() -> None:
    pool = AccountClientPool()
    made: list[FakeClient] = []

    first = await pool.get(1, _opener(made))
    first.connected = False
    second = await pool.get(1, _opener(made))

    assert second is not first
    assert len(made) == 2
    assert first.disconnected is True  # 失效的那条被关掉


async def test_pool_drop_forces_new_connection() -> None:
    """凭据变更 / 账号删除后必须拿到新连接，不能复用旧 session。"""
    pool = AccountClientPool()
    made: list[FakeClient] = []

    first = await pool.get(1, _opener(made))
    await pool.drop(1)
    second = await pool.get(1, _opener(made))

    assert second is not first
    assert first.disconnected is True

    await pool.drop(None)  # 不报错


async def test_pool_close_all_releases_everything() -> None:
    pool = AccountClientPool()
    made: list[FakeClient] = []
    await pool.get(1, _opener(made))
    await pool.get(2, _opener(made))

    await pool.close_all()

    assert all(client.disconnected for client in made)
    # 关掉之后缓存是空的：下次取会重新建
    again = await pool.get(1, _opener(made))
    assert len(made) == 3
    assert again.connected is True
