"""热门关键词：采集、累计与排名。

规则（按用户要求）：从监听到的会员发言里采词、累计排名，**永不删除**——
不参与 retention 清理，只做累加。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.hot_words import (
    drop_overlapping_duplicates,
    drop_substring_duplicates,
    extract_tokens,
)
from app.db.base import as_utc, utc_now
from app.db.models import HotKeyword


def merge_variants(
    counts: dict[str, int],
    rules: list[tuple[str, list[str]]],
) -> dict[str, dict[str, Any]]:
    """按归并规则把同类说法合并成一条。

    规则「微信 → 加我微信 / 微信同号」下，`加我微信(8)` 与 `微信同号(4)`
    合成 `微信(12)`，并保留变体明细，方便看是哪些说法贡献的次数。
    没被任何规则匹配到的词保持原样。
    """
    result: dict[str, dict[str, Any]] = {}
    consumed: set[str] = set()
    for name, variants in rules:
        needles = [item for item in (name, *variants) if item]
        matched: dict[str, int] = {}
        for token, count in counts.items():
            if token in consumed:
                continue
            if any(needle == token or needle in token or token in needle for needle in needles):
                matched[token] = count
        if not matched:
            continue
        consumed.update(matched)
        total = sum(matched.values())
        # 归类名本身没被采到过也照样显示（用户关心的是"微信"这个类）
        result[name] = {
            "count": total,
            "merged": True,
            "variants": dict(sorted(matched.items(), key=lambda kv: -kv[1])),
        }
    for token, count in counts.items():
        if token in consumed or token in result:
            continue
        result[token] = {"count": count, "merged": False, "variants": {token: count}}
    return result


async def collect_message(
    session: AsyncSession,
    *,
    text: str | None,
    source_chat_id: int | None,
    source_title: str = "",
    seen_at: datetime | None = None,
) -> int:
    """把一条发言里的候选词累加进热门词表，返回采到的词数。"""
    tokens = extract_tokens(text)
    if not tokens:
        return 0
    moment = seen_at or utc_now()
    rows = list(await session.scalars(select(HotKeyword).where(HotKeyword.token.in_(tokens))))
    existing = {row.token: row for row in rows}
    for token in tokens:
        row = existing.get(token)
        if row is None:
            row = HotKeyword(
                token=token,
                count=1,
                message_count=1,
                sources=_merge_source("", source_chat_id, source_title),
                first_seen_at=moment,
                last_seen_at=moment,
            )
            session.add(row)
            existing[token] = row
            continue
        row.count += 1
        row.message_count += 1
        row.last_seen_at = moment
        row.sources = _merge_source(row.sources, source_chat_id, source_title)
    await session.commit()
    return len(tokens)


def _merge_source(raw: str, source_chat_id: int | None, source_title: str) -> str:
    """按来源群累计次数（JSON：{群名: 次数}），方便看热词主要来自哪个群。"""
    try:
        data: dict[str, int] = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {}
    key = source_title or (f"#{source_chat_id}" if source_chat_id else "未知来源")
    data[key] = int(data.get(key, 0)) + 1
    return json.dumps(data, ensure_ascii=False)


async def rank_hot_keywords(
    session: AsyncSession,
    *,
    days: int | None = None,
    min_count: int = 1,
    limit: int = 200,
    offset: int = 0,
    merge_rules: list[tuple[str, list[str]]] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """按出现次数排名。

    返回 `(items, total, message_total)`：items 已做"长词优先"降噪。
    """
    statement = select(HotKeyword).where(HotKeyword.count >= max(1, min_count))
    if days:
        statement = statement.where(HotKeyword.last_seen_at >= utc_now() - timedelta(days=days))
    rows = list(await session.scalars(statement.order_by(HotKeyword.count.desc())))

    counts = {row.token: row.count for row in rows}
    keep = drop_substring_duplicates(counts)
    keep = drop_overlapping_duplicates(keep)
    row_by_token = {row.token: row for row in rows}
    merged = merge_variants(keep, merge_rules or [])

    ordered = sorted(merged.items(), key=lambda kv: (-kv[1]["count"], kv[0]))
    total = len(ordered)
    message_total = int(
        await session.scalar(select(func.coalesce(func.sum(HotKeyword.message_count), 0))) or 0
    )

    items: list[dict[str, Any]] = []
    for index, (token, payload) in enumerate(ordered[offset : offset + limit], start=offset + 1):
        variants = payload["variants"]
        base = row_by_token.get(token) or row_by_token.get(next(iter(variants), token))
        item = serialize_hot_keyword(base, rank=index) if base is not None else {}
        item.update(
            {
                "rank": index,
                "token": token,
                "count": payload["count"],
                "merged": payload["merged"],
                "variants": [{"token": name, "count": value} for name, value in variants.items()],
                "variant_text": "、".join(
                    f"{name}({value})" for name, value in list(variants.items())[:3]
                ),
                "id": base.id if base is not None else None,
            }
        )
        items.append(item)
    return items, total, message_total


async def stats(session: AsyncSession) -> dict[str, Any]:
    """概览：词条数、总出现次数、今日新增的词。"""
    total_tokens = int(await session.scalar(select(func.count()).select_from(HotKeyword)) or 0)
    total_count = int(
        await session.scalar(select(func.coalesce(func.sum(HotKeyword.count), 0))) or 0
    )
    today = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    new_today = int(
        await session.scalar(
            select(func.count()).select_from(HotKeyword).where(HotKeyword.first_seen_at >= today)
        )
        or 0
    )
    return {
        "tokens": total_tokens,
        "occurrences": total_count,
        "new_today": new_today,
    }


def serialize_hot_keyword(row: HotKeyword, *, rank: int | None = None) -> dict[str, Any]:
    try:
        sources: dict[str, int] = json.loads(row.sources) if row.sources else {}
    except json.JSONDecodeError:
        sources = {}
    first_seen = as_utc(row.first_seen_at)
    last_seen = as_utc(row.last_seen_at)
    return {
        "id": row.id,
        "rank": rank,
        "token": row.token,
        "count": row.count,
        "message_count": row.message_count,
        "sources": sources,
        "source_text": "、".join(
            f"{name}({count})" for name, count in sorted(sources.items(), key=lambda kv: -kv[1])[:3]
        ),
        "first_seen_at": first_seen.isoformat() if first_seen else None,
        "last_seen_at": last_seen.isoformat() if last_seen else None,
    }
