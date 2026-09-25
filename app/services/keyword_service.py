"""关键词组与关键词的读写，以及给线路用的关键词条目加载。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.keyword_matcher import KeywordEntry, parse_aliases
from app.db.models import Keyword, KeywordGroup

# 预置的常见类别别名库：让用户不用从零开始写词表。
# 每项是 (组名, 说明, [(主词, "别名,别名")])
SEED_GROUPS: list[tuple[str, str, list[tuple[str, str]]]] = [
    (
        "联系方式",
        "找人要联系方式 / 留联系方式",
        [
            ("微信", "威信,薇信,v信,vx,weixin,wechat"),
            ("电话", "手机号,手机,电话号码,联系方式,留个号"),
            ("QQ", "扣扣,q号,企鹅号"),
            ("电报", "tg,telegram,飞机号"),
        ],
    ),
    (
        "资源求助",
        "求片 / 求资源 / 要链接",
        [
            ("资源", "求资源,资源群,分享资源"),
            ("链接", "地址,入口,磁力,种子,下载"),
            ("求片", "求资源,谁有,有没有,跪求"),
        ],
    ),
    (
        "引流合作",
        "推广、广告位、合作洽谈",
        [
            ("推广", "引流,打广告,广告位,互推"),
            ("合作", "洽谈,商务,对接,置换"),
            ("加我", "私下聊,私聊,联系我,找我"),
        ],
    ),
    (
        "体育赛事",
        "体育类话题（示例：篮球、足球都算体育）",
        [
            ("体育", "篮球,足球,乒乓球,羽毛球,赛事,球赛,nba,cba"),
            ("下注", "滚球,盘口,让球,赔率,投注"),
            ("电竞", "王者荣耀,英雄联盟,lol,dota,比赛"),
        ],
    ),
    (
        "博彩相关",
        "博彩、代理、平台推广",
        [
            ("博彩", "菠菜,时时彩,彩票,棋牌"),
            ("代理", "招商,股东,开线,包网"),
            ("平台", "信誉平台,正规平台,首存,返水"),
        ],
    ),
]


async def list_groups(session: AsyncSession) -> list[KeywordGroup]:
    # 必须预加载关键词：异步会话里惰性加载会直接报 MissingGreenlet
    rows = await session.scalars(
        select(KeywordGroup).options(selectinload(KeywordGroup.keywords)).order_by(KeywordGroup.id)
    )
    return list(rows)


async def get_group(session: AsyncSession, group_id: int) -> KeywordGroup:
    group = await session.get(KeywordGroup, group_id)
    if group is None:
        raise NotFoundError("关键词组不存在")
    return group


async def create_group(
    session: AsyncSession,
    *,
    name: str,
    description: str = "",
) -> KeywordGroup:
    clean = (name or "").strip()
    if not clean:
        raise ValidationFailedError("关键词组名称不能为空")
    existing = await session.scalar(select(KeywordGroup).where(KeywordGroup.name == clean))
    if existing is not None:
        raise ConflictError(f"关键词组「{clean}」已存在")
    group = KeywordGroup(name=clean, description=(description or "").strip())
    session.add(group)
    await session.commit()
    await session.refresh(group)
    return group


async def update_group(
    session: AsyncSession,
    group_id: int,
    *,
    name: str | None = None,
    description: str | None = None,
    enabled: bool | None = None,
) -> KeywordGroup:
    group = await get_group(session, group_id)
    if name is not None:
        clean = name.strip()
        if not clean:
            raise ValidationFailedError("关键词组名称不能为空")
        duplicated = await session.scalar(
            select(KeywordGroup).where(KeywordGroup.name == clean, KeywordGroup.id != group_id)
        )
        if duplicated is not None:
            raise ConflictError(f"关键词组「{clean}」已存在")
        group.name = clean
    if description is not None:
        group.description = description.strip()
    if enabled is not None:
        group.enabled = enabled
    await session.commit()
    await session.refresh(group)
    return group


async def delete_group(session: AsyncSession, group_id: int) -> None:
    group = await get_group(session, group_id)
    await session.delete(group)
    await session.commit()


async def add_keyword(
    session: AsyncSession,
    group_id: int,
    *,
    word: str,
    aliases: str = "",
    enabled: bool = True,
) -> Keyword:
    await get_group(session, group_id)
    clean = (word or "").strip()
    if not clean:
        raise ValidationFailedError("关键词不能为空")
    duplicated = await session.scalar(
        select(Keyword).where(Keyword.group_id == group_id, Keyword.word == clean)
    )
    if duplicated is not None:
        raise ConflictError(f"关键词「{clean}」已经在组里了")
    keyword = Keyword(
        group_id=group_id, word=clean, aliases=(aliases or "").strip(), enabled=enabled
    )
    session.add(keyword)
    await session.commit()
    await session.refresh(keyword)
    return keyword


async def update_keyword(
    session: AsyncSession,
    keyword_id: int,
    *,
    word: str | None = None,
    aliases: str | None = None,
    enabled: bool | None = None,
) -> Keyword:
    keyword = await session.get(Keyword, keyword_id)
    if keyword is None:
        raise NotFoundError("关键词不存在")
    if word is not None:
        clean = word.strip()
        if not clean:
            raise ValidationFailedError("关键词不能为空")
        keyword.word = clean
    if aliases is not None:
        keyword.aliases = aliases.strip()
    if enabled is not None:
        keyword.enabled = enabled
    await session.commit()
    await session.refresh(keyword)
    return keyword


async def delete_keyword(session: AsyncSession, keyword_id: int) -> None:
    keyword = await session.get(Keyword, keyword_id)
    if keyword is None:
        raise NotFoundError("关键词不存在")
    await session.delete(keyword)
    await session.commit()


async def load_entries(
    session: AsyncSession,
    group_ids: list[int] | None = None,
) -> list[KeywordEntry]:
    """加载可用的关键词条目（供匹配引擎使用）。"""
    statement = (
        select(Keyword)
        .join(KeywordGroup, KeywordGroup.id == Keyword.group_id)
        .where(Keyword.enabled.is_(True), KeywordGroup.enabled.is_(True))
    )
    if group_ids:
        statement = statement.where(Keyword.group_id.in_(group_ids))
    rows = list(await session.scalars(statement.order_by(Keyword.id)))
    return [
        KeywordEntry(
            id=row.id,
            group_id=row.group_id,
            word=row.word,
            aliases=parse_aliases(row.aliases),
        )
        for row in rows
    ]


async def ensure_seed_groups(session: AsyncSession) -> int:
    """首次使用时写入预置别名库；已有任何分组就不动。"""
    total = await session.scalar(select(func.count()).select_from(KeywordGroup))
    if total:
        return 0
    created = 0
    for name, description, keywords in SEED_GROUPS:
        group = KeywordGroup(name=name, description=description)
        session.add(group)
        await session.flush()
        for word, aliases in keywords:
            session.add(Keyword(group_id=group.id, word=word, aliases=aliases))
        created += 1
    await session.commit()
    return created


def serialize_keyword(row: Keyword) -> dict[str, Any]:
    return {
        "id": row.id,
        "group_id": row.group_id,
        "word": row.word,
        "aliases": row.aliases,
        "alias_list": list(parse_aliases(row.aliases)),
        "enabled": row.enabled,
    }


def serialize_group(row: KeywordGroup, *, with_keywords: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "enabled": row.enabled,
        "keyword_count": len(row.keywords) if with_keywords else None,
    }
    if with_keywords:
        payload["keywords"] = [serialize_keyword(item) for item in row.keywords]
    return payload
