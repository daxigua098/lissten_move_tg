"""关键词匹配：包含匹配 + 模糊匹配 + 别名。

设计取舍（与用户确认过的方向一致）：
- 「体育 → 篮球 / 乒乓球」这种语义相近**不做**模型推理，靠关键词的别名覆盖，
  数据不出机器、命中原因完全可解释；
- 模糊匹配只兜错别字与细微差异（蓝球 → 篮球），不负责语义；
- 语义匹配预留 `semantic` 开关，接本地向量模型时在这里扩展即可。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from difflib import SequenceMatcher

from app.db.models.lead import MATCH_CONTAINS, MATCH_FUZZY

# 敏感度 → 模糊匹配阈值（越低越容易命中）
# 注意中文只有两个字时，一个字不同相似度就是 0.5（篮球 / 蓝球、羽毛球 / 篮球 都是 0.5），
# 阈值压到 0.5 会变成「只要沾一个字就算命中」，误报失控。
# 所以阈值从 0.6 起步：两字词靠别名，三个字以上才依赖模糊匹配兜错别字。
SENSITIVITY_THRESHOLDS = {
    "loose": 0.6,
    "standard": 0.75,
    "strict": 0.85,
}

_ALIAS_SPLIT = re.compile(r"[,，、;；\n\r\t|]+")
_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\u202a-\u202e\ufeff]")


@dataclass(frozen=True)
class KeywordEntry:
    """一条关键词（含别名）。"""

    id: int
    group_id: int | None
    word: str
    aliases: tuple[str, ...] = ()

    def candidates(self) -> tuple[str, ...]:
        return (self.word, *self.aliases)


@dataclass(frozen=True)
class KeywordHit:
    """一条命中记录。"""

    keyword_id: int
    group_id: int | None
    keyword: str
    matched: str
    mode: str
    score: float


def parse_aliases(raw: str | None) -> tuple[str, ...]:
    """把「篮球, 足球、乒乓球」切成别名列表。"""
    if not raw:
        return ()
    return tuple(item.strip() for item in _ALIAS_SPLIT.split(raw) if item.strip())


def normalize(text: str | None) -> str:
    """统一大小写、去掉零宽字符与多余空白，避免同一条消息匹配不上。"""
    if not text:
        return ""
    cleaned = _ZERO_WIDTH.sub("", text)
    return re.sub(r"\s+", "", cleaned).casefold()


def similarity(left: str, right: str) -> float:
    """两个字符串的相似度（0~1）。"""
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def best_fuzzy_score(keyword: str, text: str) -> float:
    """在 text 上滑动与 keyword **等长**的窗口，取最相似的窗口得分。

    只取等长窗口：短窗口会让一个相同的字就把分数顶起来（「球」对「篮球」
    能拿到 0.67），那就变成关键词只认一个字了，误报会失控。
    多字/少字的情况交给包含匹配兜。
    """
    size = len(keyword)
    if size == 0 or not text:
        return 0.0
    # 关键词里的字在文本里一个都没有，直接跳过（省掉大量无谓比较）
    if not set(keyword) & set(text):
        return 0.0
    best = 0.0
    if size > len(text):
        return 0.0
    for start in range(len(text) - size + 1):
        score = similarity(keyword, text[start : start + size])
        if score > best:
            best = score
            if best >= 0.999:
                return best
    return best


def match_text(
    text: str | None,
    entries: list[KeywordEntry],
    *,
    sensitivity: str = "loose",
    match_contains: bool = True,
    match_fuzzy: bool = True,
    exclude: tuple[str, ...] = (),
) -> list[KeywordHit]:
    """返回命中的关键词（按得分从高到低），没有命中返回空列表。"""
    normalized = normalize(text)
    if not normalized:
        return []
    if is_excluded(text, exclude):
        return []

    threshold = SENSITIVITY_THRESHOLDS.get(sensitivity, SENSITIVITY_THRESHOLDS["loose"])
    hits: list[KeywordHit] = []
    for entry in entries:
        best: KeywordHit | None = None
        for candidate in entry.candidates():
            needle = normalize(candidate)
            if not needle:
                continue
            if match_contains and needle in normalized:
                best = KeywordHit(
                    keyword_id=entry.id,
                    group_id=entry.group_id,
                    keyword=entry.word,
                    matched=candidate,
                    mode=MATCH_CONTAINS,
                    score=1.0,
                )
                break
            if not match_fuzzy:
                continue
            score = best_fuzzy_score(needle, normalized)
            if score >= threshold and (best is None or score > best.score):
                best = KeywordHit(
                    keyword_id=entry.id,
                    group_id=entry.group_id,
                    keyword=entry.word,
                    matched=candidate,
                    mode=MATCH_FUZZY,
                    score=round(score, 4),
                )
        if best is not None:
            hits.append(best)
    hits.sort(key=lambda item: item.score, reverse=True)
    return hits


def is_excluded(text: str | None, exclude: tuple[str, ...] | list[str]) -> bool:
    """消息里出现任意一个排除词就算被挡住（整条丢弃，不是只忽略那个词）。"""
    normalized = normalize(text)
    if not normalized:
        return False
    for word in exclude:
        candidate = normalize(word)
        if candidate and candidate in normalized:
            return True
    return False
