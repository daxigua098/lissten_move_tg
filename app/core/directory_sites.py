"""三方目录站：站点元数据、URL 构造与页面解析（F-R20 / F-R21）。

这一层只做"把页面变成干净的条目"，**不发请求也不碰数据库**——抓取在
``app/core/directory_client.py``，落库在 ``app/services/directory_sync_service.py``。
这样解析规则可以脱离网络单测，站点改版时改动面也小。

两个站各自踩过的坑（详见《三方目录站_可行性调研》）：

- combot 的语言过滤必须走**路径式** ``/api/chart/<lang>``，``?lang=`` 查询参数无效；
- tg-me 的搜索参数是 ``u`` 不是 ``q``；条目链接里的 ``com.`` 是它把 ``@``
  做的 SEO 替换（``com.qqpp`` = ``@qqpp``，``com.joinchat-XXXX`` = 邀请链接）。
"""

from __future__ import annotations

import html as html_lib
import json
import re
from dataclasses import dataclass
from typing import Any

from app.core.errors import ValidationFailedError

# 站点标识（与 tg_resources.source_site / discover_tasks.source 对齐）
COMBOT = "combot"
TGME = "tgme"
DIRECTORY_SITES = (COMBOT, TGME)

# 范围
SCOPE_GLOBAL = "global"
SCOPE_CHANNELS = "channels"

COMBOT_BASE = "https://combot.org"
# tg-me 主站与镜像（实测同源，页面字节数一致，可热切换）
TGME_PRIMARY = "https://tg-me.com"
TGME_MIRROR = "https://tgoop.com"
TGME_WWW = "https://www.tg-me.com"

# 实测两站都是 100 条/页
PAGE_SIZE = 100

# combot 榜单规模（用于算 pages_total，实测值）
COMBOT_TOTALS = {SCOPE_GLOBAL: 38079, SCOPE_CHANNELS: 19747}
COMBOT_TOTALS_ZH = 2320


@dataclass(frozen=True)
class DirectoryEntry:
    """目录站的一条候选资源（归一化后的形态）。"""

    source_site: str
    title: str
    tg_id: int | None = None
    username: str | None = None
    invite_link: str | None = None
    chat_type: str = "supergroup"
    member_count: int | None = None
    language: str | None = None
    rank: int | None = None

    @property
    def source_url(self) -> str | None:
        """给用户点得开的链接：优先公开用户名，其次邀请链接。"""
        if self.username:
            return f"https://t.me/{self.username}"
        return self.invite_link


# ---------------------------------------------------------------- URL 构造


def combot_page_url(
    scope: str,
    offset: int,
    *,
    limit: int = PAGE_SIZE,
    base: str = COMBOT_BASE,
) -> str:
    """combot 目录接口地址（免密钥 JSON）。

    ``scope`` 为语言代码时走路径式 ``/api/chart/<lang>``——实测 ``?lang=`` 无效。
    """
    if scope == SCOPE_CHANNELS:
        path = "/api/chart/channels/all"
    elif scope in ("", SCOPE_GLOBAL):
        path = "/api/chart/all"
    else:
        path = f"/api/chart/{scope}"
    # only_avatars=false 才会返回成员数 / 语言 / 数字 ID；头像字段在入库前丢弃
    return f"{base}{path}?limit={int(limit)}&offset={int(offset)}&only_avatars=false"


def tgme_page_url(keyword: str, page: int, *, base: str = TGME_WWW) -> str:
    """tg-me 的列表页：``/telegram-group/<关键词>/<页码>.html``（页码从 1 起）。"""
    clean = (keyword or "").strip()
    if not clean:
        raise ValidationFailedError("tg-me 列表必须有关键词")
    return f"{base}/telegram-group/{clean}/{max(1, int(page))}.html"


def pages_total(source: str, scope: str, *, page_size: int = PAGE_SIZE) -> int | None:
    """按实测规模算总页数；算不出来返回 None（界面显示未知）。"""
    if source != COMBOT:
        return None
    total = COMBOT_TOTALS.get(scope)
    if scope not in ("", SCOPE_GLOBAL, SCOPE_CHANNELS):
        total = COMBOT_TOTALS_ZH if scope == "zh" else None
    if total is None:
        return None
    return (total + page_size - 1) // page_size


# ---------------------------------------------------------------- 字段归一

_LANG_EMPTY = {"", "n/a", "na", "none", "unknown", "null", "-"}
_LANG_RE = re.compile(r"^[a-z]{2,3}(?:-[a-z]{2,4})?$")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


def normalize_language(raw: Any) -> str | None:
    """把目录站的语言代码归一到项目口径（两字母小写，如 ``ZH`` → ``zh``）。"""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    lowered = text.casefold()
    if lowered in _LANG_EMPTY:
        return None
    if "-" in lowered:
        # pt-BR → pt：跟探测侧的两字母口径保持一致
        lowered = lowered.split("-", 1)[0]
    if not _LANG_RE.match(lowered):
        return None
    return lowered


def restore_tgme_marker(marker: str) -> tuple[str | None, str | None]:
    """还原 tg-me 的 ``com.`` 标识。

    返回 ``(username, invite_link)``，两者最多有一个非空：

    - ``com.qqpp`` → ``("qqpp", None)``（``com.`` 是 ``@`` 的 SEO 替换）
    - ``com.joinchat-Go_JyaIDmoMzNDYy`` → ``(None, "https://t.me/+Go_JyaIDmoMzNDYy")``
    - 其它形态一律返回 ``(None, None)``，宁可少存也不要存错的用户名。
    """
    clean = (marker or "").strip()
    if not clean.casefold().startswith("com."):
        return None, None
    body = clean[4:]
    if not body:
        return None, None
    lowered = body.casefold()
    for prefix in ("joinchat-", "joinchat/", "joinchat"):
        if lowered.startswith(prefix):
            code = body[len(prefix) :].lstrip("-/")
            if not code:
                return None, None
            return None, f"https://t.me/+{code}"
    if _USERNAME_RE.match(body):
        return body, None
    return None, None


def _to_int(raw: Any) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    text = str(raw).strip().replace(",", "").replace(" ", "")
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


# ---------------------------------------------------------------- combot 解析


def parse_combot_page(payload: Any, *, scope: str = SCOPE_GLOBAL) -> list[DirectoryEntry]:
    """解析 combot 的 ``/api/chart/...`` 返回。

    字段：``t`` 标题、``u`` 用户名、``c`` **数字 ID**、``s`` 成员数、``l`` 语言、
    ``p`` 名次；``i``（base64 头像）与 ``pc`` / ``b`` 直接丢弃。
    """
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8", errors="replace")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ValidationFailedError(f"combot 返回的不是 JSON：{exc}") from exc
    if isinstance(payload, dict):
        # 兼容 {"items": [...]} / {"data": [...]} 这类包装
        payload = payload.get("items") or payload.get("data") or []
    if not isinstance(payload, list):
        raise ValidationFailedError("combot 返回的 JSON 结构不认识")

    chat_type = "channel" if scope == SCOPE_CHANNELS else "supergroup"
    entries: list[DirectoryEntry] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        username = (item.get("u") or "").strip().lstrip("@") or None
        title = (item.get("t") or "").strip()
        if not title and not username:
            continue
        entries.append(
            DirectoryEntry(
                source_site=COMBOT,
                title=title or f"@{username}",
                tg_id=_to_int(item.get("c")),
                username=username,
                chat_type=chat_type,
                member_count=_to_int(item.get("s")),
                language=normalize_language(item.get("l")),
                rank=_to_int(item.get("p")),
            )
        )
    return entries


# ---------------------------------------------------------------- tg-me 解析

_TGME_ITEM_RE = re.compile(r'<li class="row clear list">(?P<body>.*?)</li>', re.S)
_HREF_RE = re.compile(r'href="(?P<href>[^"]+)"')
_TGME_TITLE_RE = re.compile(r"<h2><a[^>]*>(?P<title>.*?)</a></h2>", re.S)
_TGME_BADGE_RE = re.compile(r'<div class="right btn join">\s*(?P<badge>[^<]*?)\s*</div>')
_TGME_MEMBERS_RE = re.compile(r"([\d,]+)\s*Members", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _text_of(raw: str) -> str:
    """去标签 + 还原实体（``&amp;`` 之类）。"""
    return html_lib.unescape(_TAG_RE.sub("", raw or "")).strip()


def marker_from_href(href: str) -> str | None:
    """从 ``https://www.tg-me.com/<分类名>/com.xxx`` 里取出 ``com.xxx``。

    路径第一段是**分类名**（含 ``+`` 编码空格），不是用户名，所以要按段找 ``com.``。
    """
    if not href:
        return None
    path = href.split("?", 1)[0]
    if "://" in path:
        path = path.split("://", 1)[1]
    for segment in reversed(path.split("/")):
        if segment.casefold().startswith("com."):
            return html_lib.unescape(segment).strip()
    return None


def parse_tgme_listing(html: str) -> list[DirectoryEntry]:
    """解析 tg-me / tgoop 的列表页 HTML。"""
    if not html:
        return []
    entries: list[DirectoryEntry] = []
    for match in _TGME_ITEM_RE.finditer(html):
        block = match.group("body")
        href_match = _HREF_RE.search(block)
        marker = marker_from_href(href_match.group("href")) if href_match else None
        username, invite_link = restore_tgme_marker(marker or "")

        title_match = _TGME_TITLE_RE.search(block)
        title = _text_of(title_match.group("title")) if title_match else ""

        badge_match = _TGME_BADGE_RE.search(block)
        badge = _text_of(badge_match.group("badge")).casefold() if badge_match else ""
        is_channel = badge.startswith("channel") or badge.startswith("频道")
        chat_type = "channel" if is_channel else "group"

        members_match = _TGME_MEMBERS_RE.search(block)
        member_count = _to_int(members_match.group(1)) if members_match else None

        if not title and not username and not invite_link:
            continue
        entries.append(
            DirectoryEntry(
                source_site=TGME,
                title=title or username or (invite_link or ""),
                username=username,
                invite_link=invite_link,
                chat_type=chat_type,
                member_count=member_count,
            )
        )
    return entries


def parse_page(source: str, payload: Any, *, scope: str = SCOPE_GLOBAL) -> list[DirectoryEntry]:
    """按站点分派到对应解析器。"""
    if source == COMBOT:
        return parse_combot_page(payload, scope=scope)
    if source == TGME:
        return parse_tgme_listing(payload if isinstance(payload, str) else str(payload))
    raise ValidationFailedError(f"不认识的目录站：{source}")
