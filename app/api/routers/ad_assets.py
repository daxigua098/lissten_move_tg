"""广告素材库：素材的唯一编辑入口。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_role, session_dependency
from app.api.schemas.route import AdAssetCreateRequest, AdAssetUpdateRequest
from app.core.errors import NotFoundError
from app.db.base import as_utc
from app.db.models import ROLE_SUB_ADMIN, AdAsset, Route
from app.services import ad_asset_service

router = APIRouter(
    prefix="/api/ad-assets",
    tags=["ad-assets"],
    dependencies=[Depends(require_role(ROLE_SUB_ADMIN))],
)


def serialize_asset(asset: AdAsset, references: list[Route] | None = None) -> dict[str, Any]:
    """素材对外结构。"""
    items = references or []
    return {
        "id": asset.id,
        "name": asset.name,
        "text": asset.text,
        "image_path": asset.image_path,
        "link_url": asset.link_url,
        "link_text": asset.link_text,
        "enabled": asset.enabled,
        "reference_count": len(items),
        "references": [{"route_id": item.id, "name": item.name} for item in items],
        "created_at": as_utc(asset.created_at),
        "updated_at": as_utc(asset.updated_at),
    }


@router.get("")
async def list_assets(
    enabled: bool | None = Query(default=None),
    keyword: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=300),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """素材列表（含被引用条数）。"""
    rows, total = await ad_asset_service.list_assets(
        session,
        enabled=enabled,
        keyword=keyword,
        limit=limit,
        offset=offset,
    )
    items = []
    for row in rows:
        references = await ad_asset_service.list_referencing_routes(session, row.id)
        items.append(serialize_asset(row, references))
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("", status_code=201)
async def create_asset(
    payload: AdAssetCreateRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """新建素材。"""
    asset = await ad_asset_service.create_asset(
        session,
        name=payload.name,
        text=payload.text,
        image_path=payload.image_path,
        link_url=payload.link_url,
        link_text=payload.link_text,
        enabled=payload.enabled,
    )
    return serialize_asset(asset, [])


@router.get("/{asset_id}")
async def get_asset(
    asset_id: int,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """素材详情（含引用它的线路）。"""
    asset = await ad_asset_service.get_asset(session, asset_id)
    if asset is None:
        raise NotFoundError("广告素材不存在")
    references = await ad_asset_service.list_referencing_routes(session, asset_id)
    return serialize_asset(asset, references)


@router.patch("/{asset_id}")
async def update_asset(
    asset_id: int,
    payload: AdAssetUpdateRequest,
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """更新素材（立即对引用它的线路生效）。"""
    asset = await ad_asset_service.update_asset(
        session,
        asset_id,
        name=payload.name,
        text=payload.text,
        image_path=payload.image_path,
        link_url=payload.link_url,
        link_text=payload.link_text,
        enabled=payload.enabled,
    )
    references = await ad_asset_service.list_referencing_routes(session, asset_id)
    return serialize_asset(asset, references)


@router.delete("/{asset_id}")
async def delete_asset(
    asset_id: int,
    force: bool = Query(default=False),
    session: AsyncSession = Depends(session_dependency),
) -> dict[str, Any]:
    """删除素材；被引用时默认拒绝，可加 force 强制删除。"""
    asset = await ad_asset_service.delete_asset(session, asset_id, force=force)
    return {"id": asset_id, "name": asset.name, "deleted": True}
