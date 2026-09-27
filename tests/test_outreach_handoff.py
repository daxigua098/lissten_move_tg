"""冷触达 P4：Bot 转交（一次性令牌 / 归属转移 / 防串号）。"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from conftest import ADMIN_API_TOKEN, auth_header
from sqlalchemy import func, select


def _headers() -> dict[str, str]:
    return auth_header(ADMIN_API_TOKEN)


class FakeClient:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def get_entity(self, identifier):
        return SimpleNamespace(id=777)

    async def send_message(self, entity, text, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(id=3001)


async def _prepare(session, *, tg_user_id: int = 900001, replied: bool = True):
    """建账号 + 承接 Bot + 已回复且已归属账号的联系人，并打开 Bot 转交。"""
    from app.core.outreach_capture import OUTREACH_REPLIED
    from app.db.base import utc_now
    from app.db.models import ControlBot, OutreachAccountState, OutreachContact, TgAccount
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
    bot = ControlBot(
        name="承接 Bot",
        bot_username="handoff_bot",
        token_enc="x",
        enabled=True,
    )
    session.add_all([account, bot])
    await session.flush()
    session.add(
        OutreachAccountState(
            account_id=account.id,
            tenant_id=account.tenant_id,
            tier="STANDARD",
            state="READY",
        )
    )
    contact = OutreachContact(
        tg_user_id=tg_user_id,
        username="seller03",
        display_name="卖家三号",
        identity_confirmed=True,
        contact_state=OUTREACH_REPLIED if replied else "CONTACTED",
        reply_state="REPLIED" if replied else None,
        owner_type="account" if replied else "account",
        owner_account_id=account.id if replied else None,
        last_contact_at=utc_now(),
    )
    session.add(contact)
    await session.commit()
    await session.refresh(account)
    await session.refresh(bot)
    await session.refresh(contact)
    await outreach_settings_service.update_settings(
        session,
        account.tenant_id,
        handoff_bot_enabled=True,
        handoff_bot_id=bot.id,
    )
    return account, bot, contact


async def test_handoff_requires_enabled_and_replied(admin_client) -> None:
    from app.core.errors import ValidationFailedError
    from app.db.models import OutreachContact
    from app.db.session import session_scope
    from app.services import outreach_handoff_service as handoff

    async with session_scope() as session:
        _account, _bot, contact = await _prepare(session, tg_user_id=900002)

        # 关掉开关 → 不允许
        from app.services import outreach_settings_service

        await outreach_settings_service.update_settings(
            session,
            contact.tenant_id,
            handoff_bot_enabled=False,
        )
        settings = await outreach_settings_service.read_settings(session, contact.tenant_id)
        with pytest.raises(ValidationFailedError):
            await handoff.create_handoff(session, contact=contact, settings=settings)

        # 没有归属账号的会话 → 不允许
        await outreach_settings_service.update_settings(
            session,
            contact.tenant_id,
            handoff_bot_enabled=True,
        )
        not_replied = OutreachContact(
            tg_user_id=900003,
            identity_confirmed=True,
            contact_state="CONTACTED",
        )
        session.add(not_replied)
        await session.commit()
        await session.refresh(not_replied)
        settings = await outreach_settings_service.read_settings(session, not_replied.tenant_id)
        with pytest.raises(ValidationFailedError):
            await handoff.create_handoff(session, contact=not_replied, settings=settings)


async def test_send_handoff_message_and_consume(admin_client) -> None:
    from app.db.models import OutreachHandoffToken, OutreachMessage
    from app.db.session import session_scope
    from app.services import outreach_handoff_service as handoff
    from app.services import outreach_settings_service

    async with session_scope() as session:
        account, bot, contact = await _prepare(session, tg_user_id=900004)
        settings = await outreach_settings_service.read_settings(session, contact.tenant_id)
        client = FakeClient()

        result = await handoff.send_handoff(
            session,
            client=client,
            account=account,
            contact=contact,
            settings=settings,
            created_by="admin",
        )

        assert result["bot_username"] == "handoff_bot"
        assert result["link"].startswith("https://t.me/handoff_bot?start=")
        assert client.sent and "https://t.me/handoff_bot?start=" in client.sent[0]

        row = await session.scalar(
            select(OutreachHandoffToken).where(OutreachHandoffToken.contact_id == contact.id)
        )
        assert row is not None and row.status == "pending"
        token = row.token

        transferred = await handoff.consume(session, token, tg_user_id=contact.tg_user_id)
        assert transferred is not None
        await session.refresh(contact)
        await session.refresh(row)
        assert contact.owner_type == "bot"
        assert contact.owner_bot_id == bot.id
        assert contact.owner_account_id is None
        assert row.status == "used" and row.used_at is not None

        outbound = await session.scalar(
            select(func.count())
            .select_from(OutreachMessage)
            .where(OutreachMessage.contact_id == contact.id)
        )
        assert outbound >= 1

        # 一次性：再用同一个令牌无效
        assert await handoff.consume(session, token, tg_user_id=contact.tg_user_id) is None


async def test_consume_rejects_wrong_user_and_expired(admin_client) -> None:
    from app.db.base import utc_now
    from app.db.session import session_scope
    from app.services import outreach_handoff_service as handoff
    from app.services import outreach_settings_service

    async with session_scope() as session:
        _account, _bot, contact = await _prepare(session, tg_user_id=900005)
        settings = await outreach_settings_service.read_settings(session, contact.tenant_id)

        row, _bot_row, _link = await handoff.create_handoff(
            session,
            contact=contact,
            settings=settings,
        )
        await session.commit()
        token = row.token

        # 令牌被转发给别人：作废，不改归属
        assert await handoff.consume(session, token, tg_user_id=123456) is None
        await session.refresh(contact)
        await session.refresh(row)
        assert row.status == "expired"
        assert contact.owner_type == "account"

        expired_row, _bot_row, _link = await handoff.create_handoff(
            session,
            contact=contact,
            settings=settings,
        )
        expired_row.expires_at = utc_now() - timedelta(hours=1)
        await session.commit()
        assert await handoff.consume(session, expired_row.token) is None
        await session.refresh(expired_row)
        assert expired_row.status == "expired"


async def test_handoff_api_paths(admin_client) -> None:
    from app.db.models import OutreachContact
    from app.db.session import session_scope
    from app.services import outreach_handoff_service as handoff
    from app.services import outreach_settings_service

    # 没有归属账号 → 400
    async with session_scope() as session:
        plain = OutreachContact(tg_user_id=900006, identity_confirmed=True)
        session.add(plain)
        await session.commit()
        await session.refresh(plain)
        plain_id = plain.id

    blocked = await admin_client.post(
        f"/api/outreach/contacts/{plain_id}/handoff",
        headers=_headers(),
    )
    assert blocked.status_code == 400

    # 令牌消费接口
    async with session_scope() as session:
        _account, _bot, contact = await _prepare(session, tg_user_id=900007)
        settings = await outreach_settings_service.read_settings(session, contact.tenant_id)
        row, _bot_row, _link = await handoff.create_handoff(
            session,
            contact=contact,
            settings=settings,
        )
        await session.commit()
        token = row.token

    consumed = await admin_client.post(
        "/api/outreach/handoff/consume",
        headers=_headers(),
        json={"token": token},
    )
    assert consumed.status_code == 200
    assert consumed.json()["owner_type"] == "bot"

    missing = await admin_client.post(
        "/api/outreach/handoff/consume",
        headers=_headers(),
        json={"token": "0" * 32},
    )
    assert missing.status_code == 404
