"""从文本里提取 Telegram 资源链接（纯逻辑，不联网）。

用途（F-R03 链接滚雪球 / F-R05 句式搜索）：群简介、置顶消息、消息正文、
监听中的实时消息里都藏着「别的群」的链接，把它们捞出来进候选池，就能滚雪球。

刻意排除的写法：

- ``t.me/s/xxx``：网页预览路径，不是群标识；
- ``t.me/c/123/45``：内部私有链接，只有已加入的账号能打开，对发现无意义；
- ``addstickers`` / ``share`` / ``proxy`` / ``socks`` / ``iv`` 等功能路径。

``t.me/joinchat/xxx`` 是老式邀请写法，归一成 invite（与 ``t.me/+xxx`` 同一种）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

LinkKind = Literal["username", "invite"]

USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
INVITE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{12,}$")

# t.me/xxx/123（消息链接）也算资源引用，所以只取路径的第一段
URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?(?:t\.me|telegram\.me)/([A-Za-z0-9_+\-/]+)",
    re.IGNORECASE,
)
MENTION_PATTERN = re.compile(r"(?<![A-Za-z0-9_])@([A-Za-z][A-Za-z0-9_]{3,31})")

# 这些路径不是"群/频道资源"
RESERVED_SEGMENTS = frozenset(
    {
        "s",
        "c",
        "joinchat",
        "addstickers",
        "addemoji",
        "share",
        "proxy",
        "socks",
        "iv",
        "setlanguage",
        "bg",
        "invoice",
        "giftcode",
        "boost",
        "contact",
        "login",
        "confirmphone",
    }
)


@dataclass(frozen=True)
class LinkHit:
    """一条提取到的资源引用。"""

    kind: LinkKind
    value: str
    raw: str

    @property
    def normalized_key(self) -> str:
        """去重键：不同写法（大小写、带不带 https://）归一到同一个键。"""
        if self.kind == "username":
            return f"username:{self.value.lower()}"
        return f"invite:{self.value}"

    @property
    def display(self) -> str:
        """展示用的规范写法。"""
        if self.kind == "username":
            return f"@{self.value}"
        return f"t.me/+{self.value}"


def extract_links(text: str | None) -> list[LinkHit]:
    """从一段文本里提取资源引用（保序、按归一化键去重）。

    先扫 URL、再扫 @username：同一条消息里既贴链接又写 @ 时，
    两种写法指向同一个群的话会按 normalized_key 合并成一条。
    """
    body = text or ""
    if not body:
        return []

    hits: list[LinkHit] = []
    seen: set[str] = set()

    def push(hit: LinkHit) -> None:
        if hit.normalized_key in seen:
            return
        seen.add(hit.normalized_key)
        hits.append(hit)

    for match in URL_PATTERN.finditer(body):
        hit = _hit_from_path(match.group(1), raw=match.group(0))
        if hit is not None:
            push(hit)

    for match in MENTION_PATTERN.finditer(body):
        push(LinkHit(kind="username", value=match.group(1), raw=match.group(0)))

    return hits


def extract_link_keys(text: str | None) -> set[str]:
    """只要去重键，用于"这条消息里有没有新链接"的快速判断。"""
    return {hit.normalized_key for hit in extract_links(text)}


def _hit_from_path(path: str, *, raw: str) -> LinkHit | None:
    """把 URL 路径解析成资源引用；不是资源就返回 None。"""
    cleaned = path.strip("/")
    if not cleaned:
        return None
    head, _, rest = cleaned.partition("/")
    lowered = head.lower()

    if lowered == "joinchat":
        invite = rest.strip("/")
        if INVITE_PATTERN.match(invite):
            return LinkHit(kind="invite", value=invite, raw=raw)
        return None

    if head.startswith("+"):
        invite = head[1:]
        if INVITE_PATTERN.match(invite):
            return LinkHit(kind="invite", value=invite, raw=raw)
        return None

    if lowered in RESERVED_SEGMENTS:
        return None

    if USERNAME_PATTERN.match(head):
        return LinkHit(kind="username", value=head, raw=raw)
    return None
