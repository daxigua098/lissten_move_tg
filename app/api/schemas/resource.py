"""资源发现的请求模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ResourceUpdateRequest(BaseModel):
    """人工修正字段 / 收藏 / 黑名单 / 备注。"""

    language: str | None = Field(default=None, max_length=16)
    country: str | None = Field(default=None, max_length=8)
    categories: list[str] | None = None
    note: str | None = Field(default=None, max_length=255)
    is_favorite: bool | None = None
    is_blacklisted: bool | None = None
    blacklist_reason: str | None = Field(default=None, max_length=255)
    status: str | None = Field(default=None, pattern="^(candidate|probed|adopted|retired)$")


class ResourceImportRequest(BaseModel):
    """手动添加：粘贴链接 / 用户名 / 数字 ID，一行一个。"""

    inputs: list[str] = Field(min_length=1)
    account_id: int | None = None
    # 私有邀请链接需要显式允许加入，否则识别不出群
    join: bool = False
    probe: bool = True


class ResourceRefreshRequest(BaseModel):
    """批量刷新（探测）。"""

    ids: list[int] = Field(min_length=1)
    account_id: int | None = None
    sample_depth: int | None = Field(default=None, ge=5, le=500)


class ResourceJoinRequest(BaseModel):
    """批量加入 / 退出群组池。"""

    ids: list[int] = Field(min_length=1)
    account_id: int | None = None
    action: str = Field(default="join", pattern="^(join|leave)$")


class ResourceCollectRequest(BaseModel):
    """采集一次：不填关键词就跑一批到点的发现任务。"""

    keywords: list[str] = Field(default_factory=list)
    account_id: int | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ResourceAdoptRequest(BaseModel):
    """采纳：写成监听源，可选顺手建线。"""

    account_id: int | None = None
    create_route: bool = False
    business_type: str = Field(default="B", pattern="^[AB]$")
    target_chat_ids: list[int] = Field(default_factory=list)
    route_name: str | None = Field(default=None, max_length=128)
    defer_join: bool = False


class DiscoverTaskCreateRequest(BaseModel):
    """新增发现任务。"""

    kind: str = Field(default="keyword", pattern="^(keyword|phrase|hotword|link)$")
    keyword: str = Field(min_length=1, max_length=64)
    category: str | None = Field(default=None, max_length=32)
    enabled: bool = True


class DiscoverTaskUpdateRequest(BaseModel):
    """修改发现任务。"""

    keyword: str | None = Field(default=None, min_length=1, max_length=64)
    category: str | None = Field(default=None, max_length=32)
    enabled: bool | None = None


class HotwordImportRequest(BaseModel):
    """把热门词导入发现关键词库。"""

    top_n: int = Field(default=20, ge=1, le=200)
    min_count: int = Field(default=2, ge=1, le=100000)
