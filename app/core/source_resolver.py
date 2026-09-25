"""Telegram 链接与标识解析（纯逻辑，不联网，便于单测）。

支持输入形式：
    - `t.me/xxx`、`https://t.me/xxx`、`@xxx`、`xxx`  → username
    - `t.me/+hash`、`https://t.me/joinchat/hash`      → invite（私有邀请）
    - `-1001234567890`、`1234567890`                  → tg_id
    - `+8613800001111`                                → phone（仅账号登录用）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

TargetKind = Literal["username", "invite", "tg_id", "phone"]

USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")
INVITE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{12,}$")
PHONE_PATTERN = re.compile(r"^\+\d{6,15}$")

_URL_PREFIXES = (
    "https://t.me/",
    "http://t.me/",
    "https://telegram.me/",
    "http://telegram.me/",
    "https://www.t.me/",
    "http://www.t.me/",
    "t.me/",
    "telegram.me/",
    "www.t.me/",
    "tg://",
)
_INVITE_PREFIXES = ("joinchat/", "+")


@dataclass(frozen=True)
class ResolvedTarget:
    """解析结果。"""

    kind: TargetKind
    value: str
    raw_input: str
    is_private: bool = False

    @property
    def normalized_key(self) -> str:
        """去重键：同一目标的不同写法归一到同一个键。"""
        if self.kind == "username":
            return f"username:{self.value.lower()}"
        if self.kind == "invite":
            return f"invite:{self.value}"
        if self.kind == "tg_id":
            return f"tg_id:{self.value}"
        return f"phone:{self.value}"

    @property
    def requires_join(self) -> bool:
        """私有邀请链接需要显式允许执行账号加入。"""
        return self.kind == "invite"


def resolve_target(raw: str) -> ResolvedTarget:
    """把用户输入解析成统一目标；无法识别时抛 ValueError。"""
    text = (raw or "").strip()
    if not text:
        raise ValueError("请输入群组、频道的链接或标识")

    if PHONE_PATTERN.match(text):
        return ResolvedTarget(kind="phone", value=text, raw_input=raw)

    remainder = _strip_prefixes(text)

    if remainder.startswith(_INVITE_PREFIXES):
        invite_hash = remainder.lstrip("+").replace("joinchat/", "", 1).strip("/")
        if not INVITE_PATTERN.match(invite_hash):
            raise ValueError(f"邀请链接格式不正确：{raw}")
        return ResolvedTarget(
            kind="invite",
            value=invite_hash,
            raw_input=raw,
            is_private=True,
        )

    if remainder.lstrip("-").isdigit() and remainder not in {"", "-"}:
        return ResolvedTarget(kind="tg_id", value=remainder.strip(), raw_input=raw)

    if remainder.startswith("@"):
        remainder = remainder[1:]

    if USERNAME_PATTERN.match(remainder):
        return ResolvedTarget(kind="username", value=remainder, raw_input=raw)

    if INVITE_PATTERN.match(remainder):
        return ResolvedTarget(
            kind="invite",
            value=remainder,
            raw_input=raw,
            is_private=True,
        )

    raise ValueError(f"无法识别：{raw}（支持 t.me/xxx、@xxx、t.me/+邀请码、数字 ID 或手机号）")


def resolve_many(raw_inputs: list[str]) -> list[ResolvedTarget]:
    """批量解析并去重（保留首次出现的顺序）。"""
    seen: set[str] = set()
    results: list[ResolvedTarget] = []
    for raw in raw_inputs:
        target = resolve_target(raw)
        if target.normalized_key in seen:
            continue
        seen.add(target.normalized_key)
        results.append(target)
    return results


def _strip_prefixes(text: str) -> str:
    lowered = text.lower()
    for prefix in _URL_PREFIXES:
        if lowered.startswith(prefix):
            return text[len(prefix) :].strip()
    return text
