"""2925.com 邮箱取码。

账号把接收验证码的邮箱改成 2925 子邮箱后，用主邮箱登录 IMAP 读最新一封
Telegram 验证码邮件：

- IMAP：``imap.2925.com:993``（SSL），账号是 **主邮箱 + 密码**；
- 子邮箱地址：``{主邮箱前缀}_{手机号数字}@2925.com``；
- 只认最近 30 分钟内、收件人是该子邮箱、且与 Telegram 有关的邮件。
"""

from __future__ import annotations

import asyncio
import contextlib
import email
import imaplib
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from app.core.errors import ValidationFailedError

MAIL2925_HOST = "imap.2925.com"
MAIL2925_PORT = 993
DEFAULT_MAILBOX = "INBOX"
DEFAULT_SINCE_MINUTES = 30
DEFAULT_MAX_MESSAGES = 120

CODE_PATTERNS = (
    re.compile(
        r"(?:login\s*code|verification\s*code|code|验证码|登录代码)\s*[:：]?\s*([A-Za-z0-9]{4,8})",
        re.IGNORECASE,
    ),
    re.compile(r"\b([0-9]{5})\b"),
)


@dataclass(frozen=True)
class Mail2925Config:
    main_email: str
    password: str
    host: str = MAIL2925_HOST
    port: int = MAIL2925_PORT
    mailbox: str = DEFAULT_MAILBOX


def derive_alias(main_email: str, phone: str) -> str:
    """主邮箱 + 手机号 → 该账号的 2925 子邮箱。"""
    address = (main_email or "").strip().lower()
    local, _, domain = address.rpartition("@")
    if not local or domain != "2925.com":
        raise ValidationFailedError("请输入 @2925.com 主邮箱")
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) < 6:
        raise ValidationFailedError("手机号信息不足，无法推算 2925 子邮箱")
    return f"{local}_{digits}@2925.com"


def normalize_config(
    main_email: str,
    password: str,
    *,
    mailbox: str | None = None,
) -> Mail2925Config:
    address = (main_email or "").strip().lower()
    if not address.endswith("@2925.com"):
        raise ValidationFailedError("请输入 @2925.com 主邮箱")
    if not password:
        raise ValidationFailedError("请输入 2925 邮箱密码")
    return Mail2925Config(
        main_email=address,
        password=password,
        mailbox=(mailbox or DEFAULT_MAILBOX).strip() or DEFAULT_MAILBOX,
    )


def extract_telegram_code(text: str | None) -> str:
    """从邮件正文里抠出验证码。"""
    body = (text or "").replace("\r", "")
    for pattern in CODE_PATTERNS:
        match = pattern.search(body)
        if match:
            return match.group(1)
    return ""


def _decode(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except (UnicodeDecodeError, LookupError):
        return str(value)


def _addresses(message: email.message.Message, field: str) -> list[str]:
    return [str(item).strip().lower() for item in (message.get_all(field) or []) if item]


def _body_text(message: email.message.Message) -> str:
    chunks: list[str] = []
    for part in message.walk():
        if part.get_content_maintype() == "multipart":
            continue
        if part.get_content_type() not in {"text/plain", "text/html"}:
            continue
        payload = part.get_payload(decode=True)
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        with contextlib.suppress(LookupError, UnicodeDecodeError):
            chunks.append(payload.decode(charset, errors="replace"))
    return "\n".join(chunks)


def _matches_telegram(subject: str, sender: str, text: str) -> bool:
    if re.search(r"telegram", sender, re.IGNORECASE):
        return True
    if re.search(r"telegram", subject, re.IGNORECASE):
        return True
    return bool(re.search(r"telegram", text, re.IGNORECASE))


def fetch_latest_code_sync(
    config: Mail2925Config,
    *,
    alias: str,
    since: datetime | None = None,
    max_messages: int = DEFAULT_MAX_MESSAGES,
) -> dict:
    """同步读一次子邮箱的最新验证码邮件（找不到返回空 dict）。"""
    target = (alias or "").strip().lower()
    if not target:
        raise ValidationFailedError("缺少 2925 子邮箱地址")
    moment = since or (datetime.now(UTC) - timedelta(minutes=DEFAULT_SINCE_MINUTES))

    client = imaplib.IMAP4_SSL(config.host, config.port)
    try:
        client.login(config.main_email, config.password)
        client.select(config.mailbox)
        # IMAP 的 SINCE 只精确到天，具体时间下面再用邮件头过滤
        criteria = moment.astimezone(UTC).strftime("%d-%b-%Y")
        status, data = client.search(None, "SINCE", criteria)
        if status != "OK" or not data or not data[0]:
            return {}
        ids = data[0].split()[-max(1, min(int(max_messages), 300)) :]
        for message_id in reversed(ids):
            status, payload = client.fetch(message_id, "(RFC822)")
            if status != "OK" or not payload:
                continue
            raw = payload[0][1] if isinstance(payload[0], tuple) else None
            if not raw:
                continue
            message = email.message_from_bytes(raw)
            recipients = (
                _addresses(message, "To")
                + _addresses(message, "Cc")
                + _addresses(message, "Delivered-To")
            )
            if target not in recipients:
                continue
            sent_at = None
            with contextlib.suppress(TypeError, ValueError):
                sent_at = parsedate_to_datetime(message.get("Date"))
            if sent_at is not None:
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=UTC)
                if sent_at < moment:
                    continue
            subject = _decode(message.get("Subject"))
            sender = _decode(message.get("From"))
            text = f"{subject}\n{_body_text(message)}"
            if not _matches_telegram(subject, sender, text):
                continue
            code = extract_telegram_code(text)
            if not code:
                continue
            return {
                "code": code,
                "alias": target,
                "subject": subject,
                "from": sender,
                "received_at": sent_at,
            }
        return {}
    except imaplib.IMAP4.error as exc:
        raise ValidationFailedError(f"2925 邮箱登录或读取失败：{exc}") from exc
    finally:
        with contextlib.suppress(Exception):
            client.logout()


async def fetch_latest_code(
    config: Mail2925Config,
    *,
    alias: str,
    since: datetime | None = None,
    max_messages: int = DEFAULT_MAX_MESSAGES,
) -> dict:
    """异步包装：IMAP 是阻塞库，放线程里跑。"""
    return await asyncio.to_thread(
        fetch_latest_code_sync,
        config,
        alias=alias,
        since=since,
        max_messages=max_messages,
    )
