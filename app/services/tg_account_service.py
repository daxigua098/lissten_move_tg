"""执行账号池服务：凭据加密存储、状态与健康度。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import (
    TELEGRAM_API_HASH_PATTERN,
    TELEGRAM_API_ID_MAX,
    AppConfig,
)
from app.core.errors import NotFoundError, UserExistsError, ValidationFailedError
from app.core.security import FieldCipher, mask_phone
from app.core.source_resolver import PHONE_PATTERN
from app.core.telegram_client import AccountProfile
from app.db.base import utc_now
from app.db.models import (
    ACCOUNT_ACTIVE,
    ACCOUNT_DISABLED,
    ACCOUNT_PENDING,
    ACCOUNT_RESTRICTED,
    ACCOUNT_STATUSES,
    TgAccount,
)

FAILURE_THRESHOLD = 3


async def list_accounts(
    session: AsyncSession,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[TgAccount], int]:
    """分页查询执行账号。"""
    statement = select(TgAccount).order_by(TgAccount.id)
    count_statement = select(func.count()).select_from(TgAccount)
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


async def get_default_account(session: AsyncSession) -> TgAccount | None:
    """返回默认执行账号；没有默认则退回第一个未停用账号。"""
    account = await session.scalar(
        select(TgAccount).where(TgAccount.is_default.is_(True)).order_by(TgAccount.id)
    )
    if account is not None:
        return account
    return await session.scalar(
        select(TgAccount)
        .where(TgAccount.status != ACCOUNT_DISABLED)
        .order_by(TgAccount.id)
        .limit(1)
    )


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
) -> TgAccount:
    """登记执行账号（凭据加密保存，登录另用 CLI 执行）。"""
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
    )
    session.add(account)
    await session.flush()
    if is_default:
        await _clear_other_defaults(session, account.id)
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
        await _clear_other_defaults(session, account.id)
        account.is_default = True
    elif is_default is False:
        account.is_default = False

    await session.commit()
    await session.refresh(account)
    return account


async def delete_account(session: AsyncSession, account_id: int) -> TgAccount:
    """删除执行账号。"""
    account = await get_account(session, account_id)
    if account is None:
        raise NotFoundError("执行账号不存在")
    await session.delete(account)
    await session.commit()
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


async def _clear_other_defaults(session: AsyncSession, keep_id: int) -> None:
    others = await session.scalars(
        select(TgAccount).where(TgAccount.id != keep_id, TgAccount.is_default.is_(True))
    )
    for item in others:
        item.is_default = False


def describe_status(status: str) -> str:
    """状态的中文描述。"""
    return {
        ACCOUNT_PENDING: "待登录",
        ACCOUNT_ACTIVE: "正常",
        ACCOUNT_RESTRICTED: "受限",
        ACCOUNT_DISABLED: "已停用",
    }.get(status, status)
