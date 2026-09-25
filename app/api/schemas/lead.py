"""关键词与线索的请求模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class KeywordGroupCreateRequest(BaseModel):
    """新建关键词组。"""

    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=255)


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
    sensitivity: str = "loose"
    match_contains: bool = True
    match_fuzzy: bool = True
    exclude_keywords: list[str] = Field(default_factory=list)
