"""请求级租户作用域（P1-05「查询层收口」的地基）。

一次 API 请求进入时按**登录身份**设定作用域：

- 会员账号 → 自己的租户；
- 平台账号 / API Token → 自营租户（平台管通道与客户，不碰客户业务数据）。

作用域只影响 ``TenantOwnedMixin`` 的业务表：

- **写入**：``TenantOwnedMixin.tenant_id`` 的 Python 默认值取作用域，
  会员在「账号与机器人」「线路配置」「词库」里建的东西自动落到自己租户；
- **读取**：``app.db.session`` 注册的 ORM 事件按作用域给业务表补 ``tenant_id`` 条件，
  会员查列表 / 按 ID 取单条都拿不到别人的数据。

**非请求路径不设作用域**（运行时进程、脚本、CLI、后台巡检、平台后台跨租户查询），
此时读不过滤、写入落到自营租户——与单租户时代的口径完全一致。
个别需要"越过作用域"的查询可以用
``session.execute(stmt.execution_options(include_all_tenants=True))`` 显式放行。

运行时按租户切分连接池是 P1-06 的事，本模块只管数据归属与隔离。
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token

_scope: ContextVar[int | None] = ContextVar("tg_tenant_scope", default=None)


def current_tenant_id() -> int | None:
    """当前作用域；``None`` 表示不受限（非请求路径）。"""
    return _scope.get()


def scoped_tenant_id() -> int:
    """写入该落在哪个租户：有作用域用作用域，否则自营租户。"""
    value = _scope.get()
    if value is not None:
        return value
    from app.db.models.tenant import SELF_TENANT_ID

    return SELF_TENANT_ID


def set_tenant_scope(value: int | None) -> Token:
    """设定作用域并返回用于还原的 token。"""
    return _scope.set(value)


def reset_tenant_scope(token: Token) -> None:
    """还原到设定之前的值。"""
    _scope.reset(token)


@contextmanager
def tenant_scope(value: int | None) -> Iterator[None]:
    """临时切换作用域（脚本与用例用）。"""
    token = set_tenant_scope(value)
    try:
        yield
    finally:
        reset_tenant_scope(token)
