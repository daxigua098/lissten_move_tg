"""内容净化与类型筛选（纯逻辑，不联网）。

输入统一用 MessageView，Telegram 消息由 app.core.telegram_client 适配，
这样净化规则可以完全独立单测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

KIND_TEXT = "text"
KIND_PHOTO = "photo"
KIND_VIDEO = "video"
KIND_DOCUMENT = "document"
KIND_POLL = "poll"
KIND_OTHER = "other"
KIND_SERVICE = "service"

ALL_KINDS = (KIND_TEXT, KIND_PHOTO, KIND_VIDEO, KIND_DOCUMENT, KIND_POLL)

URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.IGNORECASE)
TG_LINK_PATTERN = re.compile(r"(?:t\.me|telegram\.me)/\S+", re.IGNORECASE)
MENTION_PATTERN = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{3,32}\b")
# 先抓"疑似号码"（允许空格、连字符、括号），再按数字位数判断是否为电话号
PHONE_CANDIDATE_PATTERN = re.compile(r"(?<![\d+])(\+?\d[\d\s\-()]{7,}\d)(?!\d)")
PHONE_MIN_DIGITS = 10
INLINE_BUTTON_MARKER = "\u200b"  # 占位：Telegram 按钮不属于正文，复制时天然不带


@dataclass(frozen=True)
class MessageView:
    """净化引擎需要的最小消息视图。"""

    message_id: int
    kind: str = KIND_TEXT
    text: str = ""
    grouped_id: int | None = None
    is_post: bool = False
    sender_id: int | None = None
    sender_username: str | None = None
    sender_is_bot: bool = False
    is_forwarded: bool = False
    date: datetime | None = None
    extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CleanRules:
    """A 线净化规则。"""

    strip_url: bool = True
    strip_mention: bool = True
    strip_phone: bool = False
    promo_blacklist: tuple[str, ...] = ()
    promo_regex: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: dict) -> CleanRules:
        """从线路配置构造。"""
        values = config or {}
        return cls(
            strip_url=bool(values.get("strip_url", True)),
            strip_mention=bool(values.get("strip_mention", True)),
            strip_phone=bool(values.get("strip_phone", False)),
            promo_blacklist=tuple(
                str(item).strip() for item in values.get("promo_blacklist", []) if str(item).strip()
            ),
            promo_regex=tuple(
                str(item).strip() for item in values.get("promo_regex", []) if str(item).strip()
            ),
        )


def clean_text(text: str | None, rules: CleanRules) -> str:
    """按规则净化正文：剥链接、@提及、电话与推广词行。"""
    source = text or ""
    if not source:
        return ""

    lines = source.splitlines()
    kept: list[str] = []
    for line in lines:
        if _is_promo_line(line, rules):
            continue
        cleaned = line
        if rules.strip_url:
            cleaned = TG_LINK_PATTERN.sub("", cleaned)
            cleaned = URL_PATTERN.sub("", cleaned)
        if rules.strip_mention:
            cleaned = MENTION_PATTERN.sub("", cleaned)
        if rules.strip_phone:
            cleaned = _strip_phone(cleaned)
        kept.append(_tidy(cleaned))

    result = "\n".join(kept)
    # 收敛连续空行，避免净化后留下大片空白
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def is_empty_text(text: str | None) -> bool:
    """判断净化后是否已经没有正文。"""
    return not (text or "").strip()


def resolve_caption(
    text: str | None,
    *,
    empty_text_policy: str = "keep_media",
    has_media: bool = False,
) -> tuple[str, bool]:
    """返回 `(标题文本, 是否继续投递)`。

    净化后正文为空时：
        - keep_media：保留媒体继续投递（默认）
        - drop：整条丢弃
    """
    if not is_empty_text(text):
        return (text or "").strip(), True
    if has_media and empty_text_policy != "drop":
        return "", True
    return "", False


def allows_kind(kind: str, content_types: list[str] | tuple[str, ...]) -> bool:
    """内容类型白名单。"""
    return kind in (content_types or ())


def filter_reason(
    message: MessageView,
    *,
    content_types: list[str] | tuple[str, ...],
) -> str | None:
    """返回被过滤的原因；None 表示应当搬运。"""
    if message.kind == KIND_SERVICE:
        return "系统消息"
    if message.kind == KIND_OTHER:
        return "不支持的消息类型"
    if not allows_kind(message.kind, content_types):
        return f"内容类型不在白名单：{message.kind}"
    return None


def _is_promo_line(line: str, rules: CleanRules) -> bool:
    text = (line or "").strip()
    if not text:
        return False
    for keyword in rules.promo_blacklist:
        if keyword and keyword in text:
            return True
    for pattern in rules.promo_regex:
        if not pattern:
            continue
        try:
            if re.search(pattern, text):
                return True
        except re.error:
            # 正则写错时直接跳过该条，避免误删正常内容
            continue
    return False


def _strip_phone(text: str) -> str:
    """剥离位数达到电话号码级别的数字串。"""

    def replacer(match: re.Match[str]) -> str:
        digits = sum(1 for char in match.group(0) if char.isdigit())
        return "" if digits >= PHONE_MIN_DIGITS else match.group(0)

    return PHONE_CANDIDATE_PATTERN.sub(replacer, text)


def _tidy(line: str) -> str:
    """清理行内多余空格与残留标点。"""
    text = re.sub(r"[ \t]{2,}", " ", line).strip()
    text = re.sub(r"^[\s\-—·:：,，、]+$", "", text)
    return text
