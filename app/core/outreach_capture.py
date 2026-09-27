"""冷私聊抓取资格：可触达路径、授权类型与联系状态。"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable

CAPTURE_MODE_COLD = "cold"
CAPTURE_MODE_STRICT = "strict"
CAPTURE_MODES = (CAPTURE_MODE_COLD, CAPTURE_MODE_STRICT)

ROUTE_USERNAME = "USERNAME"
ROUTE_PHONE = "PHONE"
ROUTE_PEER_REFERENCE = "PEER_REFERENCE"
ROUTE_SHARED_GROUP = "SHARED_GROUP"
ROUTE_TYPES = (
    ROUTE_USERNAME,
    ROUTE_PHONE,
    ROUTE_PEER_REFERENCE,
    ROUTE_SHARED_GROUP,
)

CONSENT_NONE = "NONE"
CONSENT_EXPLICIT_DM_INVITE = "EXPLICIT_DM_INVITE"
CONSENT_PRIOR_REPLY = "PRIOR_REPLY"
CONSENT_MEMBER = "MEMBER_CONSENT"
CONSENT_TYPES = (
    CONSENT_NONE,
    CONSENT_EXPLICIT_DM_INVITE,
    CONSENT_PRIOR_REPLY,
    CONSENT_MEMBER,
)

OUTREACH_WAITING_SENDER_ACCOUNT = "WAITING_SENDER_ACCOUNT"
OUTREACH_CONTACTED = "CONTACTED"
OUTREACH_REPLIED = "REPLIED"
OUTREACH_REFUSED = "REFUSED"
OUTREACH_ACCOUNT_LIMITED = "ACCOUNT_LIMITED"
OUTREACH_STATUSES = (
    OUTREACH_WAITING_SENDER_ACCOUNT,
    OUTREACH_CONTACTED,
    OUTREACH_REPLIED,
    OUTREACH_REFUSED,
    OUTREACH_ACCOUNT_LIMITED,
)
OUTREACH_BLOCKED_STATUSES = frozenset(
    {
        OUTREACH_CONTACTED,
        OUTREACH_REPLIED,
        OUTREACH_REFUSED,
        OUTREACH_ACCOUNT_LIMITED,
    }
)

_EXPLICIT_INVITE_PATTERNS = (
    re.compile(r"私信我|私聊我|联系我|电报我", re.IGNORECASE),
    re.compile(r"\b(?:dm|pm|message|contact)\s+me\b", re.IGNORECASE),
)


def reachable_routes_json(routes: Iterable[str]) -> str:
    """把可触达路径规范化成稳定的 JSON 数组。"""
    selected = set(routes)
    ordered = [item for item in ROUTE_TYPES if item in selected]
    return json.dumps(ordered, ensure_ascii=False)


def parse_reachable_routes(raw: str | None) -> list[str]:
    """读取可触达路径；损坏的存储值按空列表处理。"""
    try:
        payload = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if item in ROUTE_TYPES]


def detect_reachable_routes(
    *,
    username: str | None,
    phone: str | None,
    has_peer_reference: bool,
    has_source_account: bool,
) -> tuple[str, ...]:
    """判断监听时刻已经存在的潜在联系路径。

    ``USERNAME`` 与 ``PHONE`` 可以跨发送账号使用；``PEER_REFERENCE`` 和
    ``SHARED_GROUP`` 属于当前监听账号，后续发送时必须由持有该关系的账号处理。
    """
    routes: list[str] = []
    if username:
        routes.append(ROUTE_USERNAME)
    if phone:
        routes.append(ROUTE_PHONE)
    if has_peer_reference:
        routes.append(ROUTE_PEER_REFERENCE)
    if has_source_account:
        routes.append(ROUTE_SHARED_GROUP)
    return tuple(routes)


def detect_consent_type(
    text: str | None,
    *,
    prior_reply: bool = False,
    member_consent: bool = False,
) -> str:
    """识别当前消息能证明的联系依据。"""
    if member_consent:
        return CONSENT_MEMBER
    if prior_reply:
        return CONSENT_PRIOR_REPLY
    body = text or ""
    if any(pattern.search(body) for pattern in _EXPLICIT_INVITE_PATTERNS):
        return CONSENT_EXPLICIT_DM_INVITE
    return CONSENT_NONE


def consent_satisfies_capture(mode: str, consent_type: str) -> bool:
    """严格模式必须存在授权；普通冷触达模式允许无授权候选。"""
    return mode != CAPTURE_MODE_STRICT or consent_type != CONSENT_NONE
