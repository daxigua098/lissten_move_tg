"""资源发现的请求模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ResourceUpdateRequest(BaseModel):
    """人工修正字段 / 收藏 / 黑名单 / 备注。"""

    language: str | None = Field(default=None, max_length=16)
    country: str | None = Field(default=None, max_length=8)
    categories: list[str] | None = None
    content_rating: str | None = Field(
        default=None,
        pattern="^(normal|sensitive|unknown)$",
    )
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
    # 点了就立刻执行（而不是只排队等运行时）；超出限速的部分仍然排队
    execute_now: bool = True
    # 失败退避里的任务也立刻重试（用户显式点了「立即重试」）
    force: bool = False


class ResourceAdoptRequest(BaseModel):
    """采纳：写成监听源，可选顺手建线。"""

    account_id: int | None = None
    create_route: bool = False
    business_type: str = Field(default="B", pattern="^[AB]$")
    target_chat_ids: list[int] = Field(default_factory=list)
    route_name: str | None = Field(default=None, max_length=128)
    defer_join: bool = False


class DirectorySyncRequest(BaseModel):
    """目录同步一次（F-R20 / F-R21 / F-R24）。"""

    source: str = Field(default="combot", pattern="^(combot|tgme)$")
    # 语言代码（zh / en …）、global、channels，或 tg-me 的关键词
    scope: str = Field(default="global", min_length=1, max_length=32)
    max_pages: int | None = Field(default=None, ge=1, le=1000)
    resume: bool = True


class DirectoryTaskRequest(BaseModel):
    """把某个站点某个范围设为每天自动同步（F-R24）。"""

    source: str = Field(default="combot", pattern="^(combot|tgme)$")
    scope: str = Field(default="zh", min_length=1, max_length=32)
    enabled: bool = True


class OnlineSearchRequest(BaseModel):
    """搜索时的在线补搜（F-R19）：本地结果已经先渲染，这一步只追加。"""

    keywords: list[str] = Field(min_length=1, max_length=10)
    sites: list[str] = Field(default_factory=lambda: ["telegram", "combot", "tgme"])
    limit: int = Field(default=20, ge=1, le=100)
    account_id: int | None = None
