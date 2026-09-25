"""关键词组与关键词的读写，以及给线路用的关键词条目加载。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.errors import ConflictError, NotFoundError, ValidationFailedError
from app.core.keyword_matcher import KeywordEntry, parse_aliases
from app.db.models import (
    GROUP_KIND_EXCLUDE,
    GROUP_KIND_KEYWORD,
    GROUP_KIND_MERGE,
    GROUP_KINDS,
    Keyword,
    KeywordGroup,
)

# 预置的常见类别别名库：让用户不用从零开始写词表。
# 每项是 (用途, 组名, 说明, [(主词, "别名,别名")])
SEED_GROUPS: list[tuple[str, str, str, list[tuple[str, str]]]] = [
    (
        GROUP_KIND_KEYWORD,
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
        GROUP_KIND_KEYWORD,
        "资源求助",
        "求片 / 求资源 / 要链接",
        [
            ("资源", "求资源,资源群,分享资源"),
            ("链接", "地址,入口,磁力,种子,下载"),
            ("求片", "求资源,谁有,有没有,跪求"),
        ],
    ),
    (
        GROUP_KIND_KEYWORD,
        "引流合作",
        "推广、广告位、合作洽谈",
        [
            ("推广", "引流,打广告,广告位,互推"),
            ("合作", "洽谈,商务,对接,置换"),
            ("加我", "私下聊,私聊,联系我,找我"),
        ],
    ),
    (
        GROUP_KIND_KEYWORD,
        "体育赛事",
        "体育类话题（示例：篮球、足球都算体育）",
        [
            ("体育", "篮球,足球,乒乓球,羽毛球,赛事,球赛,nba,cba"),
            ("下注", "滚球,盘口,让球,赔率,投注"),
            ("电竞", "王者荣耀,英雄联盟,lol,dota,比赛"),
        ],
    ),
    (
        GROUP_KIND_KEYWORD,
        "博彩相关",
        "博彩、代理、平台推广",
        [
            ("博彩", "菠菜,时时彩,彩票,棋牌"),
            ("代理", "招商,股东,开线,包网"),
            ("平台", "信誉平台,正规平台,首存,返水"),
        ],
    ),
    (
        GROUP_KIND_EXCLUDE,
        "通用噪声（排除）",
        "机器人 / 客服 / 公告这类不值得跟进的发言",
        [
            ("机器人", "bot,机器人助理,自动回复"),
            ("客服", "在线客服,客服中心"),
            ("管理", "管理员,群管,管理团队"),
            ("公告", "系统公告,群公告,通知"),
            ("助手", "小助手,助理,助手号"),
        ],
    ),
    (
        GROUP_KIND_EXCLUDE,
        "广告推广号（排除）",
        "到处打广告的号，不是潜在客户",
        [
            ("广告", "广告位,打广告,广告合作"),
            ("推广", "推广位,引流,互推"),
            ("招商", "招代理,招加盟"),
        ],
    ),
    (
        GROUP_KIND_MERGE,
        "同类词归并（示例）",
        "把同一种说法的不同写法归到一个名字下，热门词统计会合并计数",
        [
            ("微信", "加我微信,微信同号,威信,薇信,vx,v信,weixin,wechat"),
            ("电话", "手机号,手机,电话号码,留个号,联系方式"),
            ("电报", "tg,telegram,飞机号,紙飞机"),
        ],
    ),
]


async def list_groups(
    session: AsyncSession,
    kind: str | None = None,
) -> list[KeywordGroup]:
    # 必须预加载关键词：异步会话里惰性加载会直接报 MissingGreenlet
    statement = (
        select(KeywordGroup).options(selectinload(KeywordGroup.keywords)).order_by(KeywordGroup.id)
    )
    if kind:
        statement = statement.where(KeywordGroup.kind == kind)
    rows = await session.scalars(statement)
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
    kind: str = GROUP_KIND_KEYWORD,
) -> KeywordGroup:
    clean = (name or "").strip()
    if not clean:
        raise ValidationFailedError("词组名称不能为空")
    if kind not in GROUP_KINDS:
        raise ValidationFailedError(f"词组用途必须是 {'/'.join(GROUP_KINDS)} 之一")
    existing = await session.scalar(select(KeywordGroup).where(KeywordGroup.name == clean))
    if existing is not None:
        raise ConflictError(f"词组「{clean}」已存在")
    group = KeywordGroup(name=clean, description=(description or "").strip(), kind=kind)
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
            raise ValidationFailedError("词组名称不能为空")
        duplicated = await session.scalar(
            select(KeywordGroup).where(KeywordGroup.name == clean, KeywordGroup.id != group_id)
        )
        if duplicated is not None:
            raise ConflictError(f"词组「{clean}」已存在")
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
        .where(
            Keyword.enabled.is_(True),
            KeywordGroup.enabled.is_(True),
            KeywordGroup.kind == GROUP_KIND_KEYWORD,
        )
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


async def load_exclude_words(
    session: AsyncSession,
    group_ids: list[int] | None = None,
) -> list[str]:
    """把选中的排除词组展开成一串排除词（主词 + 别名）。

    线路可以多选引用多个排除词组，这里取并集；词组或词被停用就不参与。
    """
    if not group_ids:
        return []
    statement = (
        select(Keyword)
        .join(KeywordGroup, KeywordGroup.id == Keyword.group_id)
        .where(
            Keyword.enabled.is_(True),
            KeywordGroup.enabled.is_(True),
            KeywordGroup.kind == GROUP_KIND_EXCLUDE,
            Keyword.group_id.in_(group_ids),
        )
        .order_by(Keyword.id)
    )
    words: list[str] = []
    for row in await session.scalars(statement):
        for item in (row.word, *parse_aliases(row.aliases)):
            if item and item not in words:
                words.append(item)
    return words


async def load_merge_rules(
    session: AsyncSession,
    group_ids: list[int] | None = None,
) -> list[tuple[str, list[str]]]:
    """加载归并规则：`[(归类名, [变体...])]`。

    热门关键词排名会按这些规则把同类说法合并计数，
    例如「加我微信」「微信同号」都归到「微信」。
    """
    statement = (
        select(Keyword)
        .join(KeywordGroup, KeywordGroup.id == Keyword.group_id)
        .where(
            Keyword.enabled.is_(True),
            KeywordGroup.enabled.is_(True),
            KeywordGroup.kind == GROUP_KIND_MERGE,
        )
        .order_by(Keyword.id)
    )
    if group_ids:
        statement = statement.where(Keyword.group_id.in_(group_ids))
    rules: list[tuple[str, list[str]]] = []
    for row in await session.scalars(statement):
        variants = [item for item in parse_aliases(row.aliases) if item]
        rules.append((row.word, variants))
    return rules


async def ensure_seed_groups(session: AsyncSession) -> int:
    """写入预置词库：按用途分别补齐，已经有的那一类不动。

    这样老库（只有关键词组）点一次「导入预置词库」就能补上排除词组。
    """
    created = 0
    for kind in GROUP_KINDS:
        existing = await session.scalar(
            select(func.count()).select_from(KeywordGroup).where(KeywordGroup.kind == kind)
        )
        if existing:
            continue
        for seed_kind, name, description, keywords in SEED_GROUPS:
            if seed_kind != kind:
                continue
            group = KeywordGroup(name=name, description=description, kind=kind)
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
        "kind": row.kind,
        "keyword_count": len(row.keywords) if with_keywords else None,
    }
    if with_keywords:
        payload["keywords"] = [serialize_keyword(item) for item in row.keywords]
    return payload
