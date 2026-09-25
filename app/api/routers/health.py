"""健康检查。"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """健康检查：不返回任何敏感信息。"""
    return {"status": "ok", "time": datetime.now(UTC).isoformat(timespec="seconds")}
