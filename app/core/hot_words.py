"""从会员发言里提取候选关键词。

没有接分词库：这个小工具要的是"哪个词被反复提到"，用
中文 2~4 字 n-gram + 英文/数字整词，配合停用词过滤就够用，
而且不引入额外依赖、命中原因可解释。

降噪三招：
1. 先剥掉链接、@用户名、手机号、邮箱（这些是联系方式，不是搜索词）；
2. 去掉停用词、纯数字、单字；
3. 排名时做"长词优先"清理：同频次下短词被长词包含就丢掉（抖音 / 抖音号）。
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

URL = re.compile(r"(?:https?://|www\.)\S+|(?:t\.me|telegram\.me)/\S+", re.IGNORECASE)
MENTION = re.compile(r"@[A-Za-z][A-Za-z0-9_]{3,31}")
PHONE = re.compile(r"(?<!\d)(?:\+?86[\s-]?)?1[3-9]\d{9}(?!\d)")
INTL_PHONE = re.compile(r"(?<!\d)\+\d{8,15}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
EMOJI = re.compile(
    "["
    "\U0001f300-\U0001faff"
    "\U00002700-\U000027bf"
    "\U0001f000-\U0001f0ff"
    "\U00002600-\U000026ff"
    "\ufe0f\u200d"
    "]+"
)

CJK = "\u4e00-\u9fff"
CJK_RUN = re.compile(f"[{CJK}]+")
LATIN_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_+-]*")

STOPWORDS = frozenset(
    [
        "的",
        "了",
        "是",
        "在",
        "我",
        "你",
        "他",
        "她",
        "它",
        "们",
        "有",
        "和",
        "就",
        "都",
        "不",
        "也",
        "这",
        "那",
        "个",
        "上",
        "下",
        "来",
        "去",
        "说",
        "要",
        "会",
        "能",
        "把",
        "被",
        "让",
        "给",
        "对",
        "从",
        "到",
        "与",
        "及",
        "或",
        "但是",
        "因为",
        "所以",
        "如果",
        "还是",
        "然后",
        "已经",
        "自己",
        "大家",
        "什么",
        "怎么",
        "为什么",
        "哪里",
        "哪个",
        "一个",
        "这个",
        "那个",
        "没有",
        "不是",
        "就是",
        "可以",
        "我们",
        "你们",
        "他们",
        "现在",
        "今天",
        "明天",
        "哈哈",
        "呵呵",
        "嘿嘿",
        "嗯",
        "啊",
        "吧",
        "呢",
        "啦",
        "哦",
        "呀",
        "哈",
        "吗",
        "么",
        "呗",
        "求",
        "请问",
        "谢谢",
        "多谢",
        "有人",
        "有没有",
        "知道",
        "告诉",
        "一下",
        "多少",
        "怎么弄",
        "的话",
        "时候",
        "东西",
        "地方",
        "感觉",
        "真的",
        "应该",
        "可能",
        "需要",
        "喜欢",
        "想要",
        "大佬",
        "老哥",
        "兄弟",
        "姐妹",
        "朋友",
        "群主",
        "管理",
        "管理员",
        "今晚",
        "昨晚",
        "早上",
        "晚上",
        "中午",
        "刚才",
        "马上",
        "一起",
        "以后",
    ]
)

# 停用字：用来判断"整词都是口水话"的短词
FILLER_CHARS = "的了是在我你他她它们有和就都不也这那个上下来说要会能"

MIN_LATIN = 2
MAX_TOKEN_LENGTH = 24
# 单条消息最多贡献多少个候选词，避免长广告文淹没统计
MAX_TOKENS_PER_MESSAGE = 24


def clean_text_for_words(text: str | None) -> str:
    """剥掉链接、联系方式与表情，留下正文。"""
    body = text or ""
    for pattern in (URL, EMAIL, MENTION, PHONE, INTL_PHONE, EMOJI):
        body = pattern.sub(" ", body)
    return body


def _is_noise(token: str) -> bool:
    if len(token) < 2 or len(token) > MAX_TOKEN_LENGTH:
        return True
    if token in STOPWORDS:
        return True
    if token.isdigit():
        return True
    return all(char in FILLER_CHARS for char in token)


def extract_tokens(text: str | None, *, limit: int = MAX_TOKENS_PER_MESSAGE) -> list[str]:
    """返回这条消息里的候选关键词（去重、保序）。"""
    body = clean_text_for_words(text)
    if not body:
        return []

    tokens: list[str] = []
    seen: set[str] = set()

    def push(token: str) -> None:
        if token in seen or _is_noise(token):
            return
        seen.add(token)
        tokens.append(token)

    for match in LATIN_RUN.finditer(body.lower()):
        token = match.group()
        if len(token) >= MIN_LATIN and not token.isdigit():
            push(token)

    for match in CJK_RUN.finditer(body):
        run = match.group()
        # 中文按 4→3→2 字滑窗，长词优先（更可能是真正的搜索词）
        for size in (4, 3, 2):
            if len(run) < size:
                continue
            for start in range(len(run) - size + 1):
                push(run[start : start + size])

    return tokens[:limit]


def drop_substring_duplicates(counts: dict[str, int]) -> dict[str, int]:
    """同频次时丢掉被更长词包含的短词：抖音(3) / 抖音号(3) → 只留 抖音号。"""
    kept: dict[str, int] = {}
    for token, count in counts.items():
        redundant = any(
            other != token and token in other and counts[other] >= count for other in counts
        )
        if not redundant:
            kept[token] = count
    return kept


def overlap_ratio(left: str, right: str) -> float:
    """两个词的最大重叠程度（最长公共子串 ÷ 较短词长度）。"""
    if not left or not right:
        return 0.0
    match = SequenceMatcher(None, left, right).find_longest_match(0, len(left), 0, len(right))
    return match.size / min(len(left), len(right))


def drop_overlapping_duplicates(
    counts: dict[str, int],
    *,
    min_overlap: float = 0.6,
) -> dict[str, int]:
    """去掉同一短语被切出来的重叠碎片。

    n-gram 滑窗会把「喜欢交朋友」切出「喜欢交朋」和「欢交朋友」两个词，
    它们出现次数一样、重叠 75%，展示时只留一个（次数高的优先，其次词更长）。
    """
    items = sorted(counts.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
    kept: dict[str, int] = {}
    for token, count in items:
        redundant = any(
            other_count >= count and overlap_ratio(token, other) >= min_overlap
            for other, other_count in kept.items()
        )
        if not redundant:
            kept[token] = count
    return kept
