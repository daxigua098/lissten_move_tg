"""冷私聊抓取规则：可触达路径与授权判定。"""

from __future__ import annotations

from app.core.outreach_capture import (
    CAPTURE_MODE_COLD,
    CAPTURE_MODE_STRICT,
    CONSENT_EXPLICIT_DM_INVITE,
    CONSENT_NONE,
    ROUTE_PHONE,
    ROUTE_SHARED_GROUP,
    ROUTE_USERNAME,
    consent_satisfies_capture,
    detect_consent_type,
    detect_reachable_routes,
    parse_reachable_routes,
    reachable_routes_json,
)


def test_detect_reachable_routes_requires_a_real_contact_path() -> None:
    assert (
        detect_reachable_routes(
            username=None,
            phone=None,
            has_peer_reference=False,
            has_source_account=False,
        )
        == ()
    )

    assert detect_reachable_routes(
        username="seller01",
        phone=None,
        has_peer_reference=False,
        has_source_account=False,
    ) == (ROUTE_USERNAME,)


def test_reachable_routes_distinguishes_account_bound_routes() -> None:
    routes = detect_reachable_routes(
        username="seller01",
        phone="+8613800138000",
        has_peer_reference=True,
        has_source_account=True,
    )

    assert ROUTE_USERNAME in routes
    assert ROUTE_PHONE in routes
    assert ROUTE_SHARED_GROUP in routes
    assert parse_reachable_routes(reachable_routes_json(routes)) == list(routes)


def test_explicit_invite_is_recognised_but_plain_chat_is_not() -> None:
    assert detect_consent_type("有意的话私信我") == CONSENT_EXPLICIT_DM_INVITE
    assert detect_consent_type("今天天气不错") == CONSENT_NONE


def test_strict_capture_requires_consent() -> None:
    assert consent_satisfies_capture(CAPTURE_MODE_COLD, CONSENT_NONE) is True
    assert consent_satisfies_capture(CAPTURE_MODE_STRICT, CONSENT_NONE) is False
    assert consent_satisfies_capture(CAPTURE_MODE_STRICT, CONSENT_EXPLICIT_DM_INVITE) is True
