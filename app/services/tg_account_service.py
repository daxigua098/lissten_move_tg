"""执行账号池服务：凭据加密存储、状态与健康度。"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.account_client_pool import drop_account_client
from app.core.config import (
    TELEGRAM_API_HASH_PATTERN,
    TELEGRAM_API_ID_MAX,
    AppConfig,
)
from app.core.errors import NotFoundError, UserExistsError, ValidationFailedError
from app.core.security import FieldCipher, mask_phone
from app.core.source_resolver import PHONE_PATTERN
from app.core.telegram_client import AccountProfile
from app.db.base import as_utc, utc_now
from app.db.models import (
    ACCOUNT_ACTIVE,
    ACCOUNT_DISABLED,
    ACCOUNT_PENDING,
    ACCOUNT_PURPOSE_LABELS,
    ACCOUNT_PURPOSE_LISTEN,
    ACCOUNT_PURPOSE_OUTREACH,
    ACCOUNT_PURPOSES,
    ACCOUNT_RESTRICTED,
    ACCOUNT_STATUSES,
    TenantLimit,
    TgAccount,
)
from app.db.tenant_context import scoped_tenant_id

FAILURE_THRESHOLD = 3


def validate_purpose(purpose: str) -> str:
    """校验账号用途代号。"""
    value = (purpose or "").strip()
    if value not in ACCOUNT_PURPOSES:
        raise ValidationFailedError(f"账号用途必须是 {'/'.join(ACCOUNT_PURPOSES)} 之一")
    return value


async def ensure_outreach_quota(session: AsyncSession) -> None:
    """发信息账号数量受租户额度限制（退役的不算）。"""
    tenant_id = scoped_tenant_id()
    limit = await session.get(TenantLimit, tenant_id)
    cap = getattr(limit, "max_outreach_accounts", None) if limit is not None else None
    if not cap:
        return
    used = int(
        await session.scalar(
            select(func.count())
            .select_from(TgAccount)
            .where(
                TgAccount.tenant_id == tenant_id,
                TgAccount.purpose == ACCOUNT_PURPOSE_OUTREACH,
                TgAccount.retired_at.is_(None),
            )
        )
        or 0
    )
    if used >= int(cap):
        raise ValidationFailedError(f"发信息账号数量已达上限（{cap} 个），请先停用或删除不用的账号")


async def list_accounts(
    session: AsyncSession,
    *,
    status: str | None = None,
    purpose: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_retired: bool = False,
) -> tuple[list[TgAccount], int]:
    """分页查询账号；``purpose`` 区分执行账号与发信息账号。

    已退役（软删）的发信息账号默认不出现在列表里，历史与联系档案仍在库里。
    """
    statement = select(TgAccount).order_by(TgAccount.id)
    count_statement = select(func.count()).select_from(TgAccount)
    if not include_retired:
        statement = statement.where(TgAccount.retired_at.is_(None))
        count_statement = count_statement.where(TgAccount.retired_at.is_(None))
    if purpose:
        statement = statement.where(TgAccount.purpose == purpose)
        count_statement = count_statement.where(TgAccount.purpose == purpose)
    if status:
        statement = statement.where(TgAccount.status == status)
        count_statement = count_statement.where(TgAccount.status == status)
    rows = list(await session.scalars(statement.limit(limit).offset(offset)))
    total = int(await session.scalar(count_statement) or 0)
    return rows, total


async def get_account(session: AsyncSession, account_id: int) -> TgAccount | None:
    """按 ID 查询执行账号。"""
    return await session.get(TgAccount, account_id)


async def get_account_by_name(session: AsyncSession, name: str) -> TgAccount | None:
    """按别名查询执行账号。"""
    return await session.scalar(select(TgAccount).where(TgAccount.name == (name or "").strip()))


async def get_default_account(
    session: AsyncSession,
    *,
    tenant_id: int | None = None,
    purpose: str = ACCOUNT_PURPOSE_LISTEN,
) -> TgAccount | None:
    """返回某用途下的默认账号；没有默认则退回第一个未停用账号。

    ``tenant_id`` 非空时只在这个租户里找（P1-06）：运行时按线路的租户取号，
    不能拿别的租户的账号去发帖。
    ``purpose`` 保证采集 / 搬运 / 监听运行时永远不会拿到发信息账号。
    """
    statement = (
        select(TgAccount)
        .where(TgAccount.purpose == purpose, TgAccount.is_default.is_(True))
        .order_by(TgAccount.id)
    )
    fallback = (
        select(TgAccount)
        .where(TgAccount.purpose == purpose, TgAccount.status != ACCOUNT_DISABLED)
        .order_by(TgAccount.id)
        .limit(1)
    )
    if tenant_id is not None:
        statement = statement.where(TgAccount.tenant_id == tenant_id)
        fallback = fallback.where(TgAccount.tenant_id == tenant_id)
    account = await session.scalar(statement)
    if account is not None:
        return account
    return await session.scalar(fallback)


def decrypt_credentials(config: AppConfig, account: TgAccount) -> tuple[str, int, str]:
    """解密账号凭据，返回 `(phone, api_id, api_hash)`。"""
    cipher = FieldCipher.from_config(config)
    phone = cipher.decrypt(account.phone_enc)
    api_id = int(cipher.decrypt(account.api_id_enc))
    api_hash = cipher.decrypt(account.api_hash_enc)
    return phone, api_id, api_hash


async def create_account(
    session: AsyncSession,
    config: AppConfig,
    *,
    name: str,
    phone: str,
    api_id: int | None = None,
    api_hash: str | None = None,
    session_name: str | None = None,
    is_default: bool = False,
    note: str | None = None,
    purpose: str = ACCOUNT_PURPOSE_LISTEN,
    owner_confirmed: bool = False,
    owner_confirmed_by: str | None = None,
    owner_confirm_version: str | None = None,
    code_url: str | None = None,
) -> TgAccount:
    """登记账号（凭据加密保存，登录另用 CLI / 网页完成）。

    发信息账号（``purpose=outreach``）必须由会员确认账号归属与授权后才能登记。
    """
    purpose = validate_purpose(purpose)
    if purpose == ACCOUNT_PURPOSE_OUTREACH:
        if not owner_confirmed:
            raise ValidationFailedError("请先确认该账号归你所有并已获授权用于发送消息")
        await ensure_outreach_quota(session)
    alias = (name or "").strip()
    if not alias:
        raise ValidationFailedError("请填写账号别名")
    if await get_account_by_name(session, alias) is not None:
        raise UserExistsError("该别名已存在")

    phone_text = (phone or "").strip()
    if not PHONE_PATTERN.match(phone_text):
        raise ValidationFailedError("手机号格式不正确（示例 +8613800001111）")
    # 单个账号可以自带一套 API 凭据；留空则回退到 .env 的默认凭据
    resolved_api_id = int(api_id) if api_id else int(config.telegram.api_id or 0)
    resolved_api_hash = (api_hash or "").strip() or config.telegram.api_hash.strip()
    if resolved_api_id <= 0 or not resolved_api_hash:
        raise ValidationFailedError(
            "缺少 API ID / API Hash：请在账号里填写，或在 .env 配置 TG_API_ID / TG_API_HASH"
        )
    if resolved_api_id > TELEGRAM_API_ID_MAX:
        too_big_hint = (
            f"API ID 超出范围（{resolved_api_id}）：必须是小于 {TELEGRAM_API_ID_MAX} 的数字"
            "（通常 7~8 位）。如果这是你的 Telegram 用户 ID，"
            "那是另一个东西（用户 ID 填在「控制 Bot 的管理员」里）"
        )
        raise ValidationFailedError(too_big_hint)
    if not TELEGRAM_API_HASH_PATTERN.match(resolved_api_hash):
        raise ValidationFailedError(
            f"API Hash 形态不对（当前 {len(resolved_api_hash)} 位）："
            "官方 api_hash 是 32 位十六进制字符，请到 my.telegram.org 复制"
        )

    cipher = FieldCipher.from_config(config)
    account = TgAccount(
        name=alias,
        phone_masked=mask_phone(phone_text),
        phone_enc=cipher.encrypt(phone_text),
        api_id_enc=cipher.encrypt(str(resolved_api_id)),
        api_hash_enc=cipher.encrypt(resolved_api_hash),
        session_name=(session_name or alias).strip(),
        is_default=False,
        status=ACCOUNT_PENDING,
        note=(note or "").strip() or None,
        purpose=purpose,
    )
    code_url_text = (code_url or "").strip()
    if code_url_text:
        # 地址不合法直接拒绝，别把坏数据存进来
        from app.core.logincode import parse_login_url

        parse_login_url(code_url_text)
    if purpose == ACCOUNT_PURPOSE_OUTREACH:
        account.code_url_enc = cipher.encrypt(code_url_text) if code_url_text else None
        account.owner_confirmed_at = utc_now()
        account.owner_confirmed_by = (owner_confirmed_by or "").strip() or None
        account.owner_confirm_version = (owner_confirm_version or "").strip() or None
    session.add(account)
    await session.flush()
    if is_default:
        await _clear_other_defaults(session, account.id, purpose)
        account.is_default = True
    await session.commit()
    await session.refresh(account)
    return account


async def update_account(
    session: AsyncSession,
    config: AppConfig,
    account_id: int,
    *,
    name: str | None = None,
    note: str | None = None,
    status: str | None = None,
    is_default: bool | None = None,
    phone: str | None = None,
    api_id: int | None = None,
    api_hash: str | None = None,
) -> TgAccount:
    """更新账号信息（凭据变更会重新加密）。"""
    account = await get_account(session, account_id)
    if account is None:
        raise NotFoundError("执行账号不存在")

    if name is not None:
        alias = name.strip()
        if not alias:
            raise ValidationFailedError("账号别名不能为空")
        existing = await get_account_by_name(session, alias)
        if existing is not None and existing.id != account.id:
            raise UserExistsError("该别名已存在")
        account.name = alias
    if note is not None:
        account.note = note.strip() or None
    if status is not None:
        if status not in ACCOUNT_STATUSES:
            raise ValidationFailedError(f"状态必须是 {'/'.join(ACCOUNT_STATUSES)} 之一")
        account.status = status

    cipher = FieldCipher.from_config(config)
    if phone is not None:
        phone_text = phone.strip()
        if not PHONE_PATTERN.match(phone_text):
            raise ValidationFailedError("手机号格式不正确（示例 +8613800001111）")
        account.phone_masked = mask_phone(phone_text)
        account.phone_enc = cipher.encrypt(phone_text)
    if api_id is not None:
        if api_id <= 0:
            raise ValidationFailedError("API ID 必须是正整数")
        account.api_id_enc = cipher.encrypt(str(api_id))
    if api_hash is not None:
        if not api_hash.strip():
            raise ValidationFailedError("API Hash 不能为空")
        account.api_hash_enc = cipher.encrypt(api_hash.strip())

    if is_default is True:
        await _clear_other_defaults(session, account.id, account.purpose)
        account.is_default = True
    elif is_default is False:
        account.is_default = False

    await session.commit()
    await session.refresh(account)
    if phone is not None or api_id is not None or api_hash is not None:
        # 凭据变了：池里那条旧连接作废
        await drop_account_client(account.id)
    return account


async def delete_account(session: AsyncSession, account_id: int) -> TgAccount:
    """删除执行账号。"""
    account = await get_account(session, account_id)
    if account is None:
        raise NotFoundError("执行账号不存在")
    await session.delete(account)
    await session.commit()
    # 连接池里那条连着旧凭据的连接必须丢掉，否则会拿着已删账号的 session 干活
    await drop_account_client(account_id)
    return account


async def refresh_credentials_from_config(
    session: AsyncSession,
    config: AppConfig,
    account: TgAccount,
    *,
    api_id: int | None = None,
    api_hash: str | None = None,
    source_label: str = ".env",
) -> TgAccount:
    """用指定（默认取配置里）的 API 凭据覆盖该账号已保存的凭据。

    加解密始终用内存里的配置（保证 SECRET_KEY 一致），只覆盖凭据本身。
    """
    target_id = int(api_id) if api_id else int(config.telegram.api_id or 0)
    target_hash = (api_hash or config.telegram.api_hash).strip()

    if target_id <= 0 or not target_hash:
        raise ValidationFailedError(f"`{source_label}` 里没有配置 TG_API_ID / TG_API_HASH")
    if target_id > TELEGRAM_API_ID_MAX:
        raise ValidationFailedError(
            f"`{source_label}` 里的 TG_API_ID 超出范围（{target_id}），"
            f"必须是小于 {TELEGRAM_API_ID_MAX} 的数字（通常 7~8 位）"
        )
    if not TELEGRAM_API_HASH_PATTERN.match(target_hash):
        raise ValidationFailedError(
            f"`{source_label}` 里的 TG_API_HASH 形态不对（{len(target_hash)} 位），"
            "应为 32 位十六进制字符"
        )

    cipher = FieldCipher.from_config(config)
    account.api_id_enc = cipher.encrypt(str(target_id))
    account.api_hash_enc = cipher.encrypt(target_hash)
    await session.commit()
    await session.refresh(account)
    return account


async def mark_login_success(
    session: AsyncSession,
    account: TgAccount,
    *,
    profile: AccountProfile,
) -> TgAccount:
    """登录成功后更新资料与健康度。"""
    account.status = ACCOUNT_ACTIVE
    account.tg_user_id = profile.tg_user_id
    account.username = profile.username
    account.health_score = 100
    account.consecutive_failures = 0
    account.last_error = None
    account.last_used_at = utc_now()
    await session.commit()
    await session.refresh(account)
    return account


async def mark_failure(session: AsyncSession, account: TgAccount, *, error: str) -> TgAccount:
    """记录一次失败；达到阈值自动标记为受限。"""
    account.consecutive_failures += 1
    account.health_score = max(0, 100 - account.consecutive_failures * 20)
    account.last_error = error[:500]
    if account.consecutive_failures >= FAILURE_THRESHOLD:
        account.status = ACCOUNT_RESTRICTED
    await session.commit()
    await session.refresh(account)
    return account


async def touch_account(session: AsyncSession, account: TgAccount) -> None:
    """记录一次使用时间。"""
    account.last_used_at = utc_now()
    await session.commit()


async def _clear_other_defaults(session: AsyncSession, keep_id: int, purpose: str) -> None:
    """清掉同一用途下的其他默认账号（执行账号与发信息账号各有一个默认）。"""
    others = await session.scalars(
        select(TgAccount).where(
            TgAccount.id != keep_id,
            TgAccount.purpose == purpose,
            TgAccount.is_default.is_(True),
        )
    )
    for item in others:
        item.is_default = False


CODE_COOLDOWN_MINUTES = 30


async def save_fetched_code(
    session: AsyncSession,
    config: AppConfig,
    account: TgAccount,
    *,
    code: str,
    password: str | None = None,
) -> TgAccount:
    """接码平台一返回就先落库：后续登录失败也能看到验证码 / 二级密码。"""
    cipher = FieldCipher.from_config(config)
    text = (code or "").strip()
    if text:
        account.last_code_enc = cipher.encrypt(text)
        account.last_code_at = utc_now()
    secret = (password or "").strip()
    if secret:
        account.last_2fa_enc = cipher.encrypt(secret)
    await session.commit()
    await session.refresh(account)
    return account


async def mark_code_cooldown(
    session: AsyncSession,
    account: TgAccount,
    *,
    minutes: int = CODE_COOLDOWN_MINUTES,
) -> TgAccount:
    """接码平台提示 30 分钟内没有新码：这个账号先挂起。"""
    account.code_cooldown_until = utc_now() + timedelta(minutes=int(minutes))
    await session.commit()
    await session.refresh(account)
    return account


def code_cooldown_remaining(account: TgAccount) -> int:
    """还剩多少秒冷却（0 表示可以继续）。"""
    until = as_utc(account.code_cooldown_until)
    if until is None:
        return 0
    left = int((until - utc_now()).total_seconds())
    return max(0, left)


def decrypt_code_url(config: AppConfig, account: TgAccount) -> str | None:
    """解密发信息账号的接码地址（没有则返回 None）。"""
    if not account.code_url_enc:
        return None
    return FieldCipher.from_config(config).decrypt(account.code_url_enc)


def describe_purpose(purpose: str) -> str:
    """用途的中文描述。"""
    return ACCOUNT_PURPOSE_LABELS.get(purpose, purpose)


def describe_status(status: str) -> str:
    """状态的中文描述。"""
    return {
        ACCOUNT_PENDING: "待登录",
        ACCOUNT_ACTIVE: "正常",
        ACCOUNT_RESTRICTED: "受限",
        ACCOUNT_DISABLED: "已停用",
    }.get(status, status)
