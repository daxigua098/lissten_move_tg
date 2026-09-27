"""冷触达 P3：B 模式自动回复（默认关闭 / 转人工 / 轮次与频率）。"""

from __future__ import annotations

from types import SimpleNamespace

from conftest import ADMIN_API_TOKEN, auth_header


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def get_entity(self, identifier):
        return SimpleNamespace(id=999)

    async def send_message(self, entity, text, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(id=2000 + len(self.sent))


async def _prepare(session, *, tg_user_id: int = 890001, auto_rounds: int = 3):
    """建账号 + 自动回复话术 + 已回复的联系人。"""
    from app.core.outreach_capture import OUTREACH_REPLIED
    from app.db.base import utc_now
    from app.db.models import (
        OutreachAccountState,
        OutreachContact,
        OutreachTemplate,
        TgAccount,
    )
    from app.services import outreach_settings_service

    account = TgAccount(
        name="冷聊号",
        phone_masked="+86***1111",
        phone_enc="x",
        api_id_enc="x",
        api_hash_enc="x",
        session_name="cold",
        status="active",
        purpose="outreach",
    )
    session.add(account)
    await session.flush()
    session.add(
        OutreachAccountState(
            account_id=account.id,
            tenant_id=account.tenant_id,
            tier="STANDARD",
            state="READY",
        )
    )
    session.add(
        OutreachTemplate(
            tenant_id=account.tenant_id,
            name="自动回复",
            kind="auto_reply",
            text="{称呼}你好，稍后有人跟进。",
        )
    )
    contact = OutreachContact(
        tg_user_id=tg_user_id,
        username="seller02",
        display_name="卖家二号",
        identity_confirmed=True,
        contact_state=OUTREACH_REPLIED,
        reply_state="REPLIED",
        last_contact_at=utc_now(),
    )
    session.add(contact)
    await session.commit()
    await session.refresh(account)
    await session.refresh(contact)
    await outreach_settings_service.update_settings(
        session,
        account.tenant_id,
        reply_mode="auto",
        auto_reply_enabled=True,
        auto_reply_max_rounds=auto_rounds,
    )
    return account, contact


async def test_auto_reply_off_by_default(admin_client) -> None:
    from app.db.models import OutreachContact
    from app.db.session import session_scope
    from app.services import outreach_reply_service as reply

    async with session_scope() as session:
        contact = OutreachContact(tg_user_id=890010, identity_confirmed=True)
        session.add(contact)
        await session.commit()
        await session.refresh(contact)

        settings = await reply.outreach_settings_service.read_settings(session, contact.tenant_id)
        assert settings["auto_reply_enabled"] is False
        assert settings["reply_mode"] == "human"
        reason = await reply.should_auto_reply(session, settings=settings, contact=contact)
        assert reason == reply.BLOCK_DISABLED


async def test_auto_reply_sends_and_logs(admin_client) -> None:
    from sqlalchemy import func, select

    from app.db.models import OutreachMessage
    from app.db.session import session_scope
    from app.services import outreach_reply_service as reply

    async with session_scope() as session:
        account, contact = await _prepare(session)
        client = FakeClient()

        result = await reply.auto_reply(
            session,
            client=client,
            account=account,
            contact=contact,
            incoming_text="你好",
        )

        assert result["status"] == "SENT"
        assert client.sent == ["卖家二号你好，稍后有人跟进。"]
        auto_count = await session.scalar(
            select(func.count())
            .select_from(OutreachMessage)
            .where(OutreachMessage.generated_by == "auto")
        )
        assert auto_count == 1
        await session.refresh(contact)
        assert contact.last_outbound_at is not None


async def test_sensitive_text_hands_over_to_human(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_reply_service as reply

    async with session_scope() as session:
        account, contact = await _prepare(session, tg_user_id=890002)
        client = FakeClient()

        result = await reply.auto_reply(
            session,
            client=client,
            account=account,
            contact=contact,
            incoming_text="你们多少钱？",
        )

        assert result["status"] == "HANDOVER"
        assert client.sent == []
        await session.refresh(contact)
        assert contact.reply_state == "HUMAN"


async def test_takeover_and_cooldown_block_auto_reply(admin_client) -> None:
    from app.db.base import utc_now
    from app.db.session import session_scope
    from app.services import outreach_reply_service as reply

    async with session_scope() as session:
        account, contact = await _prepare(session, tg_user_id=890003)
        settings = await reply.outreach_settings_service.read_settings(session, contact.tenant_id)

        contact.reply_state = "HUMAN"
        await session.commit()
        assert (
            await reply.should_auto_reply(session, settings=settings, contact=contact)
            == reply.BLOCK_HUMAN_TAKEN
        )

        contact.reply_state = "REPLIED"
        contact.last_outbound_at = utc_now()
        await session.commit()
        assert (
            await reply.should_auto_reply(session, settings=settings, contact=contact)
            == reply.BLOCK_COOLDOWN
        )


async def test_max_rounds_blocks_further_replies(admin_client) -> None:
    from app.db.session import session_scope
    from app.services import outreach_reply_service as reply

    async with session_scope() as session:
        account, contact = await _prepare(session, tg_user_id=890004, auto_rounds=1)
        client = FakeClient()

        first = await reply.auto_reply(
            session,
            client=client,
            account=account,
            contact=contact,
            incoming_text="在吗",
        )
        assert first["status"] == "SENT"

        await session.refresh(contact)
        contact.last_outbound_at = None  # 绕过频率限制，只验证轮次上限
        await session.commit()
        second = await reply.auto_reply(
            session,
            client=client,
            account=account,
            contact=contact,
            incoming_text="还在吗",
        )
        assert second["status"] == "SKIPPED"
        assert second["reason"] == reply.BLOCK_MAX_ROUNDS
        assert len(client.sent) == 1


async def test_settings_and_resume_auto_via_api(admin_client) -> None:
    from app.db.models import OutreachContact
    from app.db.session import session_scope

    updated = await admin_client.patch(
        "/api/outreach/settings",
        headers=_headers(),
        json={
            "reply_mode": "auto",
            "auto_reply_enabled": True,
            "auto_reply_max_rounds": 5,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["reply_mode"] == "auto"
    assert updated.json()["auto_reply_enabled"] is True
    assert updated.json()["auto_reply_max_rounds"] == 5

    bad = await admin_client.patch(
        "/api/outreach/settings",
        headers=_headers(),
        json={"reply_mode": "robot"},
    )
    assert bad.status_code == 422

    async with session_scope() as session:
        contact = OutreachContact(
            tg_user_id=890005,
            identity_confirmed=True,
            reply_state="HUMAN",
        )
        session.add(contact)
        await session.commit()
        await session.refresh(contact)
        contact_id = contact.id

    resumed = await admin_client.post(
        f"/api/outreach/contacts/{contact_id}/resume-auto",
        headers=_headers(),
    )
    assert resumed.status_code == 200
    assert resumed.json()["reply_state"] == "REPLIED"


async def test_auto_reply_template_may_contain_link(admin_client) -> None:
    """首条不能带链接，自动回复可以（会话已经建立）。"""
    created = await admin_client.post(
        "/api/outreach/templates",
        headers=_headers(),
        json={
            "name": "自动回复带链接",
            "kind": "auto_reply",
            "text": "详情看这里 https://example.com",
        },
    )
    assert created.status_code == 201
    assert created.json()["kind"] == "auto_reply"
