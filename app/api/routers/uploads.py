"""图片上传接口：广告素材的本地图片上传。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, File, Request, UploadFile

from app.api.deps import require_role
from app.core.config import AppConfig
from app.core.errors import ValidationFailedError
from app.db.models import ROLE_SUB_ADMIN
from app.services import upload_service

router = APIRouter(
    prefix="/api/uploads",
    tags=["uploads"],
    dependencies=[Depends(require_role(ROLE_SUB_ADMIN))],
)


@router.post("/image", status_code=201)
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """上传一张图片，返回相对路径与可访问 URL。"""
    config: AppConfig = request.app.state.config
    content = await file.read()
    return upload_service.save_image(
        config,
        filename=file.filename,
        content=content,
    )


@router.delete("/image/{filename}")
async def delete_image(filename: str, request: Request) -> dict[str, Any]:
    """删除已上传的图片。"""
    config: AppConfig = request.app.state.config
    removed = upload_service.delete_image(config, filename)
    if not removed:
        raise ValidationFailedError("文件不存在或已删除")
    return {"filename": filename, "deleted": True}
