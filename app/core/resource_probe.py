"""资源探测的口径计算（纯逻辑，不联网）。

需求书 F-R06 / F-R07 要求「活跃度、语言、行业、线索潜力都用自有数据计算，
口径统一、可解释」。所以这里不引入模型、不做语义推断：每个指标都能指着
采样消息算出它是怎么来的，界面上也能解释给运营看。

几个口径上的取舍（都写进注释，避免以后有人"顺手优化"改坏）：

- **真人活跃度**：只统计非机器人、非纯广告的消息（本模块默认口径）；
- **日均发帖**：采样跨度不足 1 天时按 1 天算，避免把"半小时 100 条"放大成几千条；
- **线索潜力**：命中率放大 5 倍后封顶 100（20% 的消息命中关键词即满分）；
- **国家**：只从消息里的手机号国际区号推断，语言推不出国家（阿拉伯语 ≠ 沙特）。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.core.link_extractor import extract_links

# ---------------------------------------------------------------- 广告与噪音

# 纯广告判定：命中这些"卖货话术"或"光贴链接 + 联系方式"就当成广告
AD_KEYWORDS = (
    "推广",
    "广告",
    "加微",
    "加我微信",
    "微信号",
    "私聊",
    "联系我",
    "承接",
    "代发",
    "刷单",
    "返水",
    "首存",
    "优惠",
    "包赢",
    "代充",
    "全网最低",
    "厂家直销",
    "招代理",
    "招商",
    "引流",
)
CONTACT_KEYWORDS = ("微信", "wechat", "whatsapp", "电报", "telegram", "tg", "qq", "飞机")
PHONE_PATTERN = re.compile(r"(?<!\d)(?:\+?86[\s-]?)?1[3-9]\d{9}(?!\d)")
INTL_PHONE_PATTERN = re.compile(r"(?<!\d)\+(\d{2,3})[\s-]?\d{6,12}(?!\d)")

# ---------------------------------------------------------------- 内容分级

# 敏感内容词表（F-R22）。两个来源都不算数：站点给的榜单位置、语言代码。
# 判定只看群名 / 简介 / 采样消息里的实际用词。
# 词表刻意保守：宁可把"擦边但正常"的群判成 normal，也不要一上来就藏一片。
SENSITIVE_WORDS = (
    # 成人
    "成人",
    "情色",
    "色情",
    "约炮",
    "裸聊",
    "福利姬",
    "楼凤",
    "全套",
    "包夜",
    "资源片",
    "av资源",
    "成人视频",
    "18+",
    "nsfw",
    "porn",
    "nude",
    "escort",
    # 赌博
    "博彩",
    "菠菜",
    "赌博",
    "赌场",
    "彩票",
    "时时彩",
    "棋牌",
    "六合彩",
    "百家乐",
    "返水",
    "真人荷官",
    "线上娱乐城",
)


def detect_content_rating(*texts: str | None) -> str:
    """按词表粗判内容分级（F-R22）。

    返回 ``sensitive`` / ``normal`` / ``unknown``：

    - 有文本且命中敏感词 → ``sensitive``；
    - 有足够文本但一个都没命中 → ``normal``；
    - 没有可用文本（比如刚入库、只有标题）→ ``unknown``，留给探测复判。
    """
    body = " ".join(item for item in texts if item).strip()
    if len(body) < 2:
        return "unknown"
    lowered = body.casefold()
    return "sensitive" if any(word in lowered for word in SENSITIVE_WORDS) else "normal"


def looks_like_ad(text: str | None) -> bool:
    """是不是一条纯广告。

    两种形态：
    1. 明着写"推广 / 加微 / 返水"这类卖货话术；
    2. 光贴链接（或 @用户名）再挂一个联系方式——正文几乎是空的。
    """
    body = (text or "").strip()
    if not body:
        return False
    lowered = body.casefold()
    if any(word in lowered for word in AD_KEYWORDS):
        return True
    has_target = bool(extract_links(body)) or "@" in body
    has_contact = any(word in lowered for word in CONTACT_KEYWORDS) or bool(
        PHONE_PATTERN.search(body)
    )
    # 去掉链接和联系方式后没剩几个字，说明这条就是纯引流
    stripped = re.sub(r"\S*@\S*", " ", body)
    stripped = re.sub(r"(?:https?://)?(?:t\.me|telegram\.me)/\S+", " ", stripped)
    stripped = PHONE_PATTERN.sub(" ", stripped)
    remainder = re.sub(r"[\s\W_]+", "", stripped, flags=re.UNICODE)
    # 阈值给到 10 个字：「t.me/xxx 微信 abc123」这类只剩联系方式的算广告，
    # 而「分享一个不错的链接 t.me/xxx 大家看看」正文够长，不算。
    return has_target and has_contact and len(remainder) <= 10


# ---------------------------------------------------------------- 语言 / 国家

_SCRIPT_PATTERNS = (
    ("zh", re.compile(r"[\u4e00-\u9fff]")),
    ("ru", re.compile(r"[\u0400-\u04ff]")),
    ("ar", re.compile(r"[\u0600-\u06ff]")),
    ("hi", re.compile(r"[\u0900-\u097f]")),
    ("fa", re.compile(r"[\u0600-\u06ff]")),  # 与阿拉伯语同区段，先命中 ar 就用 ar
)

# 国际区号 → 国家。只认"消息里真的出现了手机号"这种情况，不做猜测。
COUNTRY_PREFIXES = {
    "86": "CN",
    "852": "HK",
    "853": "MO",
    "886": "TW",
    "81": "JP",
    "82": "KR",
    "66": "TH",
    "84": "VN",
    "60": "MY",
    "62": "ID",
    "63": "PH",
    "65": "SG",
    "95": "MM",
    "855": "KH",
    "856": "LA",
    "91": "IN",
    "92": "PK",
    "971": "AE",
    "7": "RU",
    "44": "GB",
    "1": "US",
}


def detect_language(*texts: str | None) -> str | None:
    """按字符区段占比判断语言；判断不出来返回 None。"""
    body = " ".join(item for item in texts if item)
    if not body.strip():
        return None
    counts: dict[str, int] = {}
    for code, pattern in _SCRIPT_PATTERNS:
        found = len(pattern.findall(body))
        if found:
            counts[code] = counts.get(code, 0) + found
    if counts:
        return max(counts.items(), key=lambda kv: kv[1])[0]
    # 没有表意文字：有拉丁字母就算英文，只剩数字/符号就说不出来
    return "en" if re.search(r"[A-Za-z]", body) else None


def detect_country(text: str | None) -> str | None:
    """从消息里的手机号国际区号推断国家；没有号码就返回 None。"""
    body = text or ""
    if not body:
        return None
    hits: list[str] = []
    for match in INTL_PHONE_PATTERN.finditer(body):
        prefix = match.group(1)
        # 先试 3 位区号，再试 2 位、1 位，避免 +852 被读成 +8
        for size in (3, 2, 1):
            code = prefix[:size]
            if code in COUNTRY_PREFIXES:
                hits.append(COUNTRY_PREFIXES[code])
                break
    if not hits:
        return None
    return max(set(hits), key=hits.count)


# ---------------------------------------------------------------- 行业字典

# 行业推断用的词表。命中即打标，同一条消息可以命中多个行业。
# 想调整就往这里加词（非功能需求里的"行业字典可配置"）。
INDUSTRY_DICTIONARY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("搜索导航", ("搜索", "搜群", "导航", "大全", "索引", "找群", "资源库", "极搜")),
    ("加密货币", ("加密货币", "btc", "比特币", "usdt", "以太坊", "炒币", "虚拟币", "链游", "web3")),
    ("博彩", ("博彩", "菠菜", "赌场", "彩票", "时时彩", "棋牌", "六合", "返水", "百家乐")),
    ("求职招聘", ("招聘", "求职", "找工作", "招工", "日结", "兼职", "小时工", "劳务")),
    ("交友相亲", ("交友", "相亲", "单身", "脱单", "约会", "同城", "附近人")),
    ("游戏", ("游戏", "开黑", "手游", "端游", "梦幻", "王者", "吃鸡", "私服")),
    ("电商带货", ("电商", "带货", "开店", "拼多多", "淘宝", "亚马逊", "跨境", "货源")),
    ("金融贷款", ("贷款", "借款", "信用卡", "网贷", "征信", "过桥", "车贷", "房贷")),
    ("投资理财", ("投资", "理财", "股票", "基金", "期货", "外汇", "黄金", "原始股")),
    ("移民留学", ("移民", "留学", "签证", "出国", "护照", "绿卡", "海外身份")),
    ("房产", ("房产", "买房", "楼盘", "二手房", "租房", "中介", "学区房")),
    ("汽车", ("汽车", "二手车", "车友", "改装", "提车", "车险")),
    ("医疗健康", ("医疗", "医院", "看病", "药品", "代购药", "养生", "整形")),
    ("教育培训", ("培训", "考证", "学历", "网课", "考试", "考研", "公考", "家教")),
    ("旅游", ("旅游", "旅行", "组团", "签证", "机票", "酒店", "自驾")),
    ("资源分享", ("资源", "分享", "破解", "课程", "素材", "网盘", "影视", "小说")),
)


def detect_categories(*texts: str | None, limit: int = 5) -> list[str]:
    """按行业字典给资源打标签（按命中词数排序，最多 limit 个）。"""
    body = " ".join(item for item in texts if item).casefold()
    if not body.strip():
        return []
    scored: list[tuple[int, str]] = []
    for name, words in INDUSTRY_DICTIONARY:
        hits = sum(1 for word in words if word in body)
        if hits:
            scored.append((hits, name))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [name for _hits, name in scored[:limit]]


# ---------------------------------------------------------------- 指标计算


@dataclass(frozen=True)
class ProbeThresholds:
    """索引型判定阈值（F-R07，默认值取自需求书第 10 章）。"""

    index_member_threshold: int = 5000
    index_feature_threshold: int = 3
    activity_threshold: float = 30.0
    link_density_threshold: float = 30.0


@dataclass(frozen=True)
class ProbeMetrics:
    """一次探测算出来的全部指标。"""

    sample_size: int = 0
    posts_per_day: float = 0.0
    human_ratio: float = 0.0
    unique_senders: int = 0
    link_density: float = 0.0
    activity_score: float = 0.0
    lead_potential: float = 0.0
    bot_ratio: float = 0.0
    last_active_at: datetime | None = None
    language: str | None = None
    country: str | None = None
    categories: tuple[str, ...] = ()
    index_score: int = 0
    is_index_group: bool = False
    index_reasons: tuple[str, ...] = field(default_factory=tuple)
    # 内容分级（F-R22）：normal / sensitive / unknown
    content_rating: str = "unknown"


def _text_of(message: Any) -> str:
    return str(getattr(message, "text", "") or "")


def _date_of(message: Any) -> datetime | None:
    value = getattr(message, "date", None)
    return value if isinstance(value, datetime) else None


def _sender_of(message: Any) -> int | None:
    value = getattr(message, "sender_id", None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_bot(message: Any) -> bool:
    return bool(getattr(message, "sender_is_bot", False))


def count_keyword_hits(
    texts: Sequence[str],
    entries: Iterable[Any],
    *,
    sensitivity: str = "loose",
) -> int:
    """采样消息里有多少条命中了现有词库（F-R06 线索潜力分的原料）。"""
    from app.core.keyword_matcher import match_text

    entry_list = list(entries)
    if not entry_list:
        return 0
    hits = 0
    for text in texts:
        if match_text(text, entry_list, sensitivity=sensitivity):
            hits += 1
    return hits


def compute_metrics(
    messages: Sequence[Any],
    *,
    title: str | None = None,
    about: str | None = None,
    member_count: int | None = None,
    keyword_entries: Iterable[Any] = (),
    sensitivity: str = "loose",
    thresholds: ProbeThresholds | None = None,
    now: datetime | None = None,
) -> ProbeMetrics:
    """按采样消息算出一组指标（口径见模块开头）。"""
    limits = thresholds or ProbeThresholds()
    samples = list(messages)
    total = len(samples)
    if total == 0:
        return ProbeMetrics(
            language=detect_language(title, about),
            categories=tuple(detect_categories(title, about)),
            country=detect_country(about),
            content_rating=detect_content_rating(title, about),
        )

    texts = [_text_of(item) for item in samples]
    link_messages = sum(1 for text in texts if extract_links(text))
    bot_messages = sum(1 for item in samples if _is_bot(item))
    senders = {
        _sender_of(item) for item in samples if not _is_bot(item) and _sender_of(item) is not None
    }

    human_like = sum(
        1
        for item, text in zip(samples, texts, strict=True)
        if not _is_bot(item) and not looks_like_ad(text)
    )

    human_ratio = human_like / total
    link_density = link_messages / total * 100
    bot_ratio = bot_messages / total

    times = sorted(item for item in (_date_of(sample) for sample in samples) if item is not None)
    last_active_at = times[-1] if times else None
    posts_per_day = _posts_per_day(times, total)

    unique_senders = len(senders)
    activity_score = round(
        100
        * (
            0.5 * human_ratio
            + 0.3 * min(1.0, unique_senders / 50)
            + 0.2 * min(1.0, posts_per_day / 50)
        ),
        1,
    )

    hit_messages = count_keyword_hits(texts, keyword_entries, sensitivity=sensitivity)
    lead_potential = round(min(100.0, hit_messages / total * 100 * 5), 1)

    joined = "\n".join(text for text in texts if text)
    language = detect_language(title, about, joined)
    country = detect_country(joined) or detect_country(about) or detect_country(title)
    categories = detect_categories(title, about, joined)

    index_score, reasons = _index_features(
        title=title,
        about=about,
        member_count=member_count,
        activity_score=activity_score,
        link_density=link_density,
        bot_ratio=bot_ratio,
        short_ratio=_short_ratio(texts),
        limits=limits,
    )

    return ProbeMetrics(
        sample_size=total,
        posts_per_day=posts_per_day,
        human_ratio=round(human_ratio, 4),
        unique_senders=unique_senders,
        link_density=round(link_density, 2),
        activity_score=activity_score,
        lead_potential=lead_potential,
        bot_ratio=round(bot_ratio, 4),
        last_active_at=last_active_at,
        language=language,
        country=country,
        categories=tuple(categories),
        index_score=index_score,
        is_index_group=index_score >= limits.index_feature_threshold,
        index_reasons=tuple(reasons),
        content_rating=detect_content_rating(title, about, joined),
    )


def _posts_per_day(times: list[datetime], total: int) -> float:
    """日均发帖：条数 ÷ 采样跨度（天）。

    跨度不足 1 天按 1 天算——半小时 100 条不等于一天 4800 条，
    这种群"很活跃"但不需要一个虚高的数字去污染排序。
    """
    if total == 0:
        return 0.0
    if len(times) < 2:
        return float(total)
    span_seconds = (times[-1] - times[0]).total_seconds()
    span_days = max(span_seconds / 86400, 1.0)
    return round(total / span_days, 2)


def _short_ratio(texts: Sequence[str]) -> float:
    """短消息占比：索引型群的消息往往是"关键词"这种一句话。"""
    if not texts:
        return 0.0
    short = sum(1 for text in texts if len(text.strip()) <= 30)
    return short / len(texts)


TITLE_INDEX_WORDS = (
    "搜索",
    "搜群",
    "导航",
    "大全",
    "索引",
    "找群",
    "极搜",
    "资源库",
    "index",
    "search",
)


def _index_features(
    *,
    title: str | None,
    about: str | None,
    member_count: int | None,
    activity_score: float,
    link_density: float,
    bot_ratio: float,
    short_ratio: float,
    limits: ProbeThresholds,
) -> tuple[int, list[str]]:
    """F-R07 的 5 项特征，命中 ≥ 阈值项就认定为索引型群。"""
    reasons: list[str] = []
    haystack = f"{title or ''} {about or ''}".casefold()

    if any(word in haystack for word in TITLE_INDEX_WORDS):
        reasons.append("标题含搜索/导航类词")

    if link_density >= limits.link_density_threshold or (short_ratio >= 0.5 and link_density >= 10):
        reasons.append("消息以短链接为主")

    if bot_ratio >= 0.3:
        reasons.append("机器人发言占比高")

    if member_count is not None and member_count >= limits.index_member_threshold:
        reasons.append(f"成员数 ≥ {limits.index_member_threshold}")

    if activity_score >= limits.activity_threshold:
        reasons.append(f"真人活跃度 ≥ {limits.activity_threshold:g}")

    return len(reasons), reasons
