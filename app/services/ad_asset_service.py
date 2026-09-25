"""广告素材库：素材是广告内容的唯一编辑入口，线路只引用。"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, UserExistsError, ValidationFailedError
from app.db.models import AdAsset, Route


def validate_payload(
    *,
    text: str | None,
    image_path: str | None,
    link_url: str | None,
    link_text: str | None,
) -> None:
    """文案、图片、按钮至少填一项，否则会挂出一条空广告。"""
    if not (text or "").strip() and not (image_path or "").strip() and not (link_url or "").strip():
        raise ValidationFailedError("文案、图片、链接按钮至少要填一项")
    if (link_url or "").strip() and not (link_text or "").strip():
        raise ValidationFailedError("填写了链接地址就需要填写按钮文字")


async def list_assets(
    session: AsyncSession,
    *,
    enabled: bool | None = None,
    keyword: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[AdAsset], int]:
    """分页查询素材。"""
    statement = select(AdAsset).order_by(AdAsset.id)
    count_statement = select(func.count()).select_from(AdAsset)
    if enabled is not None:
        statement = statement.where(AdAsset.enabled.is_(enabled))
        count_statement = count_statement.where(AdAsset.enabled.is_(enabled))
    if keyword:
        pattern = f"%{keyword}%"
        condition = AdAsset.name.like(pattern) | AdAsset.text.like(pattern)
        statement = statement.where(condition)
        count_statement = count_statement.where(condition)
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)
    return rows, total


async def get_asset(session: AsyncSession, asset_id: int) -> AdAsset | None:
    """按 ID 查询素材。"""
    return await session.get(AdAsset, asset_id)


async def get_asset_by_name(session: AsyncSession, name: str) -> AdAsset | None:
    """按名称查询素材。"""
    return await session.scalar(select(AdAsset).where(AdAsset.name == (name or "").strip()))


async def create_asset(
    session: AsyncSession,
    *,
    name: str,
    text: str = "",
    image_path: str | None = None,
    link_url: str | None = None,
    link_text: str | None = None,
    enabled: bool = True,
) -> AdAsset:
    """新建素材。"""
    alias = (name or "").strip()
    if not alias:
        raise ValidationFailedError("请填写素材名")
    if await get_asset_by_name(session, alias) is not None:
        raise UserExistsError("该素材名已存在")
    validate_payload(text=text, image_path=image_path, link_url=link_url, link_text=link_text)

    asset = AdAsset(
        name=alias,
        text=(text or "").strip(),
        image_path=(image_path or "").strip() or None,
        link_url=(link_url or "").strip() or None,
        link_text=(link_text or "").strip() or None,
        enabled=enabled,
    )
    session.add(asset)
    await session.commit()
    await session.refresh(asset)
    return asset


async def update_asset(
    session: AsyncSession,
    asset_id: int,
    *,
    name: str | None = None,
    text: str | None = None,
    image_path: str | None = None,
    link_url: str | None = None,
    link_text: str | None = None,
    enabled: bool | None = None,
) -> AdAsset:
    """更新素材（改完立即对所有引用它的线路生效）。"""
    asset = await get_asset(session, asset_id)
    if asset is None:
        raise NotFoundError("广告素材不存在")

    if name is not None:
        alias = name.strip()
        if not alias:
            raise ValidationFailedError("素材名不能为空")
        existing = await get_asset_by_name(session, alias)
        if existing is not None and existing.id != asset.id:
            raise UserExistsError("该素材名已存在")
        asset.name = alias

    next_text = asset.text if text is None else text
    next_image = asset.image_path if image_path is None else image_path
    next_url = asset.link_url if link_url is None else link_url
    next_label = asset.link_text if link_text is None else link_text
    validate_payload(
        text=next_text,
        image_path=next_image,
        link_url=next_url,
        link_text=next_label,
    )

    asset.text = (next_text or "").strip()
    asset.image_path = (next_image or "").strip() or None
    asset.link_url = (next_url or "").strip() or None
    asset.link_text = (next_label or "").strip() or None
    if enabled is not None:
        asset.enabled = enabled
    await session.commit()
    await session.refresh(asset)
    return asset


async def list_referencing_routes(session: AsyncSession, asset_id: int) -> list[Route]:
    """找出所有引用该素材的线路。"""
    routes = list(await session.scalars(select(Route).order_by(Route.id)))
    references: list[Route] = []
    for route in routes:
        try:
            config = json.loads(route.a_config or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(config, dict) and config.get("ad_asset_id") == asset_id:
            references.append(route)
    return references


async def delete_asset(
    session: AsyncSession,
    asset_id: int,
    *,
    force: bool = False,
) -> AdAsset:
    """删除素材；被线路引用时默认拒绝。"""
    asset = await get_asset(session, asset_id)
    if asset is None:
        raise NotFoundError("广告素材不存在")
    references = await list_referencing_routes(session, asset_id)
    if references and not force:
        names = "、".join(item.name for item in references[:3])
        raise ValidationFailedError(
            f"该素材正在被 {len(references)} 条线路引用（{names}…），"
            "建议改为停用，或加 force 强制删除"
        )
    await session.delete(asset)
    await session.commit()
    return asset
