"""功能授权与用量限制：功能包模板、租户功能块、租户限额（P2 账号体系）。

约定：

- 运行时判断依据是 ``tenant_modules``（一行一个功能块），模板只是开号时的快捷选项；
- 基础能力（登录、改密、TG 账号、机器人、运行总览）不落库，对所有会员恒开；
- ``tenant_limits`` 只有试用会写具体数值，正式会员留空 = 不限。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.base import utc_now
from app.db.models import (
    MODULE_LABELS,
    MODULES,
    PLAN_KINDS,
    PLAN_STANDARD,
    PlanTemplate,
    TenantLimit,
    TenantModule,
)

PLAN_CODE_MAX_LENGTH = 32
PLAN_NAME_MAX_LENGTH = 64

# 用量限制的合法键：与 TenantLimit 列一一对应
_LIMIT_INT_KEYS = ("max_routes", "max_tg_accounts", "max_sources")


def validate_module(module: str) -> str:
    """校验功能块代号。"""
    value = (module or "").strip()
    if value not in MODULES:
        raise ValidationFailedError(f"功能块必须是 {'/'.join(MODULES)} 之一")
    return value


def module_labels(modules: list[str]) -> list[str]:
    """把功能块代号翻译成显示名（前端可直接用）。"""
    return [MODULE_LABELS.get(item, item) for item in modules]


# --------------------------------------------------------------------------- #
# 租户功能授权
# --------------------------------------------------------------------------- #
async def list_module_rows(session: AsyncSession, tenant_id: int) -> list[TenantModule]:
    """列出某租户的全部功能授权行（含已关闭的）。"""
    statement = (
        select(TenantModule)
        .where(TenantModule.tenant_id == tenant_id)
        .order_by(TenantModule.module)
    )
    return list(await session.scalars(statement))


async def list_modules(session: AsyncSession, tenant_id: int) -> list[str]:
    """列出某租户**生效**的功能块代号。"""
    statement = (
        select(TenantModule.module)
        .where(
            TenantModule.tenant_id == tenant_id,
            TenantModule.enabled.is_(True),
        )
        .order_by(TenantModule.module)
    )
    return list(await session.scalars(statement))


async def has_module(session: AsyncSession, tenant_id: int, module: str) -> bool:
    """判断某租户是否启用了指定功能块。"""
    if module not in MODULES:
        return False
    statement = select(TenantModule).where(
        TenantModule.tenant_id == tenant_id,
        TenantModule.module == module,
        TenantModule.enabled.is_(True),
    )
    return await session.scalar(statement) is not None


async def grant_module(
    session: AsyncSession,
    *,
    tenant_id: int,
    module: str,
    granted_by: str | None = None,
) -> TenantModule:
    """开启一个功能块（幂等：已开则原样返回）。"""
    value = validate_module(module)
    row = await session.get(TenantModule, (tenant_id, value))
    if row is None:
        row = TenantModule(
            tenant_id=tenant_id,
            module=value,
            enabled=True,
            granted_by=(granted_by or "").strip() or None,
            granted_at=utc_now(),
        )
        session.add(row)
    else:
        row.enabled = True
        row.granted_by = (granted_by or "").strip() or row.granted_by
        row.granted_at = utc_now()
    row.updated_at = utc_now()
    await session.commit()
    await session.refresh(row)
    return row


async def revoke_module(
    session: AsyncSession,
    *,
    tenant_id: int,
    module: str,
) -> TenantModule:
    """关闭一个功能块（保留行，置 ``enabled=false``）。"""
    value = validate_module(module)
    row = await session.get(TenantModule, (tenant_id, value))
    if row is None:
        raise NotFoundError("该租户没有这个功能块的授权记录")
    row.enabled = False
    row.updated_at = utc_now()
    await session.commit()
    await session.refresh(row)
    return row


async def set_modules(
    session: AsyncSession,
    *,
    tenant_id: int,
    modules: list[str],
    granted_by: str | None = None,
) -> list[str]:
    """整体设置功能块：清单里有的开启，其余关闭。返回生效清单。"""
    wanted = {validate_module(item) for item in modules}
    existing = {row.module: row for row in await list_module_rows(session, tenant_id)}
    now = utc_now()
    actor = (granted_by or "").strip() or None

    for module in MODULES:
        row = existing.get(module)
        enabled = module in wanted
        if row is None:
            if not enabled:
                continue
            session.add(
                TenantModule(
                    tenant_id=tenant_id,
                    module=module,
                    enabled=True,
                    granted_by=actor,
                    granted_at=now,
                    updated_at=now,
                )
            )
        else:
            if row.enabled != enabled:
                row.updated_at = now
            row.enabled = enabled
            if enabled:
                row.granted_by = actor or row.granted_by
                row.granted_at = now

    await session.commit()
    return await list_modules(session, tenant_id)


async def clear_modules(session: AsyncSession, tenant_id: int) -> None:
    """删除某租户的全部功能授权行（删租户时兜底；FK 一般已级联）。"""
    for row in await list_module_rows(session, tenant_id):
        await session.delete(row)
    await session.commit()


# --------------------------------------------------------------------------- #
# 租户用量限制
# --------------------------------------------------------------------------- #
async def get_limits(session: AsyncSession, tenant_id: int) -> TenantLimit | None:
    """取某租户的用量限制行；没有则返回 None（= 不限）。"""
    return await session.get(TenantLimit, tenant_id)


async def set_limits(
    session: AsyncSession,
    *,
    tenant_id: int,
    max_routes: int | None = None,
    max_tg_accounts: int | None = None,
    max_sources: int | None = None,
    allow_export: bool = True,
) -> TenantLimit:
    """整体写入用量限制；数值为 None 表示不限。"""
    for key, value in (
        ("max_routes", max_routes),
        ("max_tg_accounts", max_tg_accounts),
        ("max_sources", max_sources),
    ):
        if value is not None and value < 0:
            raise ValidationFailedError(f"{key} 不能为负数")

    row = await session.get(TenantLimit, tenant_id)
    if row is None:
        row = TenantLimit(tenant_id=tenant_id)
        session.add(row)
    row.max_routes = max_routes
    row.max_tg_accounts = max_tg_accounts
    row.max_sources = max_sources
    row.allow_export = allow_export
    row.updated_at = utc_now()
    await session.commit()
    await session.refresh(row)
    return row


async def clear_limits(session: AsyncSession, tenant_id: int) -> None:
    """删除某租户的用量限制行。"""
    row = await session.get(TenantLimit, tenant_id)
    if row is not None:
        await session.delete(row)
        await session.commit()


def limits_to_payload(limit: TenantLimit | None) -> dict[str, Any]:
    """把限制行转成登录响应里的 limits 对象。

    "无限制"返回空对象：数值为 None 的键不出现，``allow_export`` 只在关闭时出现。
    """
    if limit is None:
        return {}
    payload: dict[str, Any] = {}
    for key in _LIMIT_INT_KEYS:
        value = getattr(limit, key)
        if value is not None:
            payload[key] = value
    if not limit.allow_export:
        payload["allow_export"] = False
    return payload


# --------------------------------------------------------------------------- #
# 功能包模板
# --------------------------------------------------------------------------- #
def _parse_json_list(raw: str, *, field: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError) as exc:  # pragma: no cover - 由上层校验兜底
        raise ValidationFailedError(f"{field} 不是合法 JSON") from exc
    if not isinstance(value, list):
        raise ValidationFailedError(f"{field} 必须是数组")
    return [validate_module(str(item)) for item in value]


def _parse_json_object(raw: str, *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:  # pragma: no cover - 由上层校验兜底
        raise ValidationFailedError(f"{field} 不是合法 JSON") from exc
    if not isinstance(value, dict):
        raise ValidationFailedError(f"{field} 必须是对象")
    return value


def plan_template_payload(template: PlanTemplate) -> dict[str, Any]:
    """模板转成可直接返回给前端的字典。"""
    return {
        "id": template.id,
        "code": template.code,
        "name": template.name,
        "kind": template.kind,
        "modules": _parse_json_list(template.modules, field="modules"),
        "limits": _parse_json_object(template.limits, field="limits"),
        "enabled": template.enabled,
    }


async def list_plan_templates(
    session: AsyncSession,
    *,
    enabled_only: bool = True,
) -> list[PlanTemplate]:
    """列出功能包模板。"""
    statement = select(PlanTemplate).order_by(PlanTemplate.id)
    if enabled_only:
        statement = statement.where(PlanTemplate.enabled.is_(True))
    return list(await session.scalars(statement))


async def get_plan_template(session: AsyncSession, code: str) -> PlanTemplate | None:
    """按 code 查询模板。"""
    return await session.scalar(
        select(PlanTemplate).where(PlanTemplate.code == (code or "").strip())
    )


async def create_plan_template(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    modules: list[str],
    limits: dict[str, Any] | None = None,
    kind: str = PLAN_STANDARD,
    enabled: bool = True,
) -> PlanTemplate:
    """新建功能包模板。"""
    code_value = (code or "").strip()
    name_value = (name or "").strip()
    if not code_value or len(code_value) > PLAN_CODE_MAX_LENGTH:
        raise ValidationFailedError(f"模板 code 必填且不超过 {PLAN_CODE_MAX_LENGTH} 位")
    if not name_value or len(name_value) > PLAN_NAME_MAX_LENGTH:
        raise ValidationFailedError("模板名不能为空且不能过长")
    if kind not in PLAN_KINDS:
        raise ValidationFailedError(f"模板类型必须是 {'/'.join(PLAN_KINDS)} 之一")
    for item in modules:
        validate_module(item)
    if await get_plan_template(session, code_value) is not None:
        raise ConflictError("模板 code 已存在")

    template = PlanTemplate(
        code=code_value,
        name=name_value,
        kind=kind,
        modules=json.dumps(sorted(set(modules)), ensure_ascii=False),
        limits=json.dumps(limits or {}, ensure_ascii=False),
        enabled=enabled,
    )
    session.add(template)
    await session.commit()
    await session.refresh(template)
    return template


async def seed_plan_templates(session: AsyncSession) -> int:
    """幂等写入预置模板，返回新增条数（已存在的不动）。"""
    seeds: tuple[tuple[str, str, str, list[str], dict[str, Any]], ...] = (
        (
            "trial_carry",
            "1 天试用（搬运）",
            "trial",
            ["carry"],
            {"max_routes": 1, "allow_export": False},
        ),
        (
            "trial_monitor",
            "1 天试用（监听）",
            "trial",
            ["monitor"],
            {"max_routes": 1, "allow_export": False},
        ),
        ("standard", "常规开通", "standard", [], {}),
        (
            "full",
            "全功能",
            "standard",
            ["carry", "monitor", "discovery"],
            {},
        ),
    )
    created = 0
    for code, name, kind, modules, limits in seeds:
        if await get_plan_template(session, code) is not None:
            continue
        await create_plan_template(
            session,
            code=code,
            name=name,
            kind=kind,
            modules=modules,
            limits=limits,
        )
        created += 1
    return created
