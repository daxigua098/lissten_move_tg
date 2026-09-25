"""前端版本信息：用于识别浏览器是否还在用旧页面。"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Request

from app.core.config import AppConfig

router = APIRouter(prefix="/api/meta", tags=["meta"])

_JS_PATTERN = re.compile(r"/assets/(index-[\w.-]+\.js)")


def current_bundle_name(config: AppConfig) -> str | None:
    """从构建产物 index.html 里解析出当前 JS 文件名。"""
    index_file = config.project_root / "frontend" / "dist" / "index.html"
    if not index_file.is_file():
        return None
    try:
        content = index_file.read_text(encoding="utf-8")
    except OSError:
        return None
    found = _JS_PATTERN.search(content)
    return found.group(1) if found else None


@router.get("")
async def meta(request: Request) -> dict[str, Any]:
    """返回前端构建标识（不含敏感信息，无需登录）。"""
    config: AppConfig = request.app.state.config
    return {
        "app": config.app.name,
        "version": "0.1.0",
        "bundle": current_bundle_name(config),
        "demo_mode": config.app.demo_mode,
    }
