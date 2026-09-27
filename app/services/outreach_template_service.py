"""冷触达话术模板：平台共享模板（只读）+ 会员私有模板。"""

from __future__ import annotations

import json

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.db.models import (
    MEDIA_KINDS,
    TEMPLATE_FIRST_CONTACT,
    TEMPLATE_KINDS,
    TEMPLATE_SCOPE_PLATFORM,
    TEMPLATE_SCOPE_TENANT,
    OutreachTemplate,
)
from app.services import upload_service

# 首条招呼不允许出现链接：既不礼貌，也最容易触发反垃圾
FIRST_CONTACT_BANNED_TOKENS = ("http://", "https://", "t.me/", "www.", "tg://")


def validate_text(kind: str, text: str) -> str:
    """校验话术；首条招呼禁止带链接。"""
    body = (text or "").strip()
    if not body:
        raise ValidationFailedError("话术内容不能为空")
    if kind == TEMPLATE_FIRST_CONTACT:
        lowered = body.lower()
        for token in FIRST_CONTACT_BANNED_TOKENS:
            if token in lowered:
                raise ValidationFailedError(
                    "首条招呼不能包含链接或邀请链接，请让对方主动回复后再发"
                )
    return body


def validate_media(
    kind: str,
    media_path: str | None,
    media_kind: str | None,
) -> tuple[str | None, str | None]:
    """校验富媒体：只给跟进 / 自动回复；首条招呼仍然禁止媒体。"""
    path = upload_service.normalize_relative_path(media_path)
    if not path:
        return None, None
    if kind == TEMPLATE_FIRST_CONTACT:
        raise ValidationFailedError("首条招呼不能带图片或视频，请把媒体放到跟进 / 自动回复模板里")
    media = (media_kind or "").strip().lower()
    if media not in MEDIA_KINDS:
        raise ValidationFailedError("媒体类型必须是 image 或 video")
    return path, media


async def list_templates(
    session: AsyncSession,
    tenant_id: int,
    *,
    kind: str | None = None,
) -> list[OutreachTemplate]:
    """平台模板 + 本租户模板。"""
    conditions = [
        or_(
            OutreachTemplate.scope == TEMPLATE_SCOPE_PLATFORM,
            OutreachTemplate.tenant_id == tenant_id,
        )
    ]
    if kind:
        conditions.append(OutreachTemplate.kind == kind)
    statement = (
        select(OutreachTemplate)
        .where(*conditions)
        .order_by(OutreachTemplate.scope, OutreachTemplate.id)
    )
    return list(await session.scalars(statement))


async def get_template(
    session: AsyncSession,
    tenant_id: int,
    template_id: int,
) -> OutreachTemplate:
    """取平台模板或本租户模板。"""
    row = await session.get(OutreachTemplate, template_id)
    if row is None or (row.scope != TEMPLATE_SCOPE_PLATFORM and row.tenant_id != tenant_id):
        raise NotFoundError("话术模板不存在")
    return row


async def create_template(
    session: AsyncSession,
    tenant_id: int,
    *,
    name: str,
    kind: str,
    text: str,
    variables: list[str] | None = None,
    created_by: str | None = None,
    scope: str = TEMPLATE_SCOPE_TENANT,
    media_path: str | None = None,
    media_kind: str | None = None,
) -> OutreachTemplate:
    """新建模板；平台模板由平台侧创建，`tenant_id` 留空表示共享。"""
    title = (name or "").strip()
    if not title:
        raise ValidationFailedError("请填写模板名称")
    if kind not in TEMPLATE_KINDS:
        raise ValidationFailedError(f"模板类型必须是 {'/'.join(TEMPLATE_KINDS)} 之一")
    if scope not in (TEMPLATE_SCOPE_PLATFORM, TEMPLATE_SCOPE_TENANT):
        raise ValidationFailedError("模板范围不合法")
    body = validate_text(kind, text)
    media, media_type = validate_media(kind, media_path, media_kind)
    owner = None if scope == TEMPLATE_SCOPE_PLATFORM else tenant_id
    await _ensure_unique(session, owner, kind, title)
    row = OutreachTemplate(
        scope=scope,
        tenant_id=owner,
        name=title,
        kind=kind,
        text=body,
        variables=json.dumps(list(variables or []), ensure_ascii=False),
        media_path=media,
        media_kind=media_type,
        created_by=created_by,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_template(
    session: AsyncSession,
    tenant_id: int,
    template_id: int,
    *,
    name: str | None = None,
    kind: str | None = None,
    text: str | None = None,
    enabled: bool | None = None,
    variables: list[str] | None = None,
    media_path: str | None = None,
    media_kind: str | None = None,
) -> OutreachTemplate:
    """修改会员自己的模板；平台模板只读。"""
    row = await get_template(session, tenant_id, template_id)
    if row.scope == TEMPLATE_SCOPE_PLATFORM:
        raise ValidationFailedError("平台模板只读，请先「选用」成自己的模板再修改")

    target_kind = kind or row.kind
    if kind is not None and kind not in TEMPLATE_KINDS:
        raise ValidationFailedError(f"模板类型必须是 {'/'.join(TEMPLATE_KINDS)} 之一")
    if name is not None:
        title = name.strip()
        if not title:
            raise ValidationFailedError("请填写模板名称")
        await _ensure_unique(session, tenant_id, target_kind, title, skip_id=row.id)
        row.name = title
    if text is not None:
        row.text = validate_text(target_kind, text)
        row.version += 1
    if kind is not None:
        row.kind = kind
    if variables is not None:
        row.variables = json.dumps(list(variables), ensure_ascii=False)
    if media_path is not None or media_kind is not None:
        media, media_type = validate_media(target_kind, media_path, media_kind)
        row.media_path = media
        row.media_kind = media_type
    if enabled is not None:
        row.enabled = bool(enabled)
    await session.commit()
    await session.refresh(row)
    return row


async def delete_template(
    session: AsyncSession,
    tenant_id: int,
    template_id: int,
) -> OutreachTemplate:
    """删除会员自己的模板；平台模板只读。"""
    row = await get_template(session, tenant_id, template_id)
    if row.scope == TEMPLATE_SCOPE_PLATFORM:
        raise ValidationFailedError("平台模板不能删除")
    await session.delete(row)
    await session.commit()
    return row


async def adopt_template(
    session: AsyncSession,
    tenant_id: int,
    template_id: int,
    *,
    created_by: str | None = None,
) -> OutreachTemplate:
    """选用平台模板：复制成自己的租户副本并记录来源版本。"""
    source = await get_template(session, tenant_id, template_id)
    name = source.name if source.scope == TEMPLATE_SCOPE_PLATFORM else f"{source.name} 副本"
    await _ensure_unique(session, tenant_id, source.kind, name)
    row = OutreachTemplate(
        scope=TEMPLATE_SCOPE_TENANT,
        tenant_id=tenant_id,
        name=name,
        kind=source.kind,
        text=source.text,
        variables=source.variables,
        source_template_id=source.id,
        source_version=source.version,
        created_by=created_by,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def _ensure_unique(
    session: AsyncSession,
    tenant_id: int | None,
    kind: str,
    name: str,
    *,
    skip_id: int | None = None,
) -> None:
    statement = select(OutreachTemplate.id).where(
        OutreachTemplate.kind == kind,
        OutreachTemplate.name == name,
    )
    statement = (
        statement.where(OutreachTemplate.tenant_id.is_(None))
        if tenant_id is None
        else statement.where(OutreachTemplate.tenant_id == tenant_id)
    )
    existing = await session.scalar(statement)
    if existing is not None and existing != skip_id:
        raise ConflictError("同类型下已有同名模板")
