"""关键词与线索的请求模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class KeywordGroupCreateRequest(BaseModel):
    """新建关键词组。"""

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=255)
    # keyword=关键词组（判断命中）/ exclude=排除词组（命中即忽略）
    # merge=归并规则（同类词合并计数）
    kind: str = Field(default="keyword", pattern="^(keyword|exclude|merge)$")


class KeywordGroupUpdateRequest(BaseModel):
    """修改关键词组。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=255)
    enabled: bool | None = None


class KeywordCreateRequest(BaseModel):
    """在组里新增关键词。"""

    group_id: int = Field(gt=0)
    word: str = Field(min_length=1, max_length=64)
    aliases: str = ""
    enabled: bool = True


class KeywordUpdateRequest(BaseModel):
    """修改关键词。"""

    word: str | None = Field(default=None, min_length=1, max_length=64)
    aliases: str | None = None
    enabled: bool | None = None


class KeywordMatchRequest(BaseModel):
    """用一段文本试跑关键词，返回命中了什么。"""

    text: str = Field(min_length=1, max_length=4000)
    group_ids: list[int] = Field(default_factory=list)
    exclude_group_ids: list[int] = Field(default_factory=list)
    sensitivity: str = "loose"
    match_contains: bool = True
    match_fuzzy: bool = True
    exclude_keywords: list[str] = Field(default_factory=list)


class HotKeywordPromoteRequest(BaseModel):
    """把热门词加进某个关键词组。"""

    token: str = Field(min_length=1, max_length=64)
    group_id: int = Field(gt=0)
    # 归并后的变体：一起写进别名，例如 微信 → 加我微信 / 微信同号
    aliases: list[str] = Field(default_factory=list)
