"""账号导入解析：每行「+手机号 接码地址」。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from app.core.source_resolver import PHONE_PATTERN

LINE_PATTERN = re.compile(
    r"^(?P<phone>\+?[0-9][0-9\s()\-]{5,})\s+(?P<url>https?://\S+)(?:\s+.*)?$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ImportedAccount:
    """一行导入记录。"""

    phone: str
    code_url: str
    code_host: str


def parse_account_lines(text: str) -> tuple[list[ImportedAccount], list[dict]]:
    """解析粘贴进来的账号文本，返回 ``(账号列表, 错误列表)``。

    每行格式：``+14135030718 https://logincode.xxx/?token=...``，
    手机号后面的内容一律当作接码地址。
    """
    accounts: list[ImportedAccount] = []
    errors: list[dict] = []
    seen: set[str] = set()

    for index, raw in enumerate((text or "").splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        match = LINE_PATTERN.match(line)
        if not match:
            errors.append({"line": index, "message": "格式应为：+手机号 接码地址"})
            continue
        phone = re.sub(r"[\s()\-]", "", match.group("phone"))
        if not phone.startswith("+"):
            errors.append({"line": index, "message": f"{phone} 缺少国家区号，请写成 +{phone}"})
            continue
        if not PHONE_PATTERN.match(phone):
            errors.append({"line": index, "message": f"{phone} 不是有效的手机号"})
            continue
        if phone in seen:
            errors.append({"line": index, "message": f"{phone} 在本批次里重复"})
            continue
        url = match.group("url").strip()
        seen.add(phone)
        accounts.append(
            ImportedAccount(
                phone=phone,
                code_url=url,
                code_host=urlparse(url).netloc,
            )
        )

    if not accounts and not errors:
        errors.append({"line": 0, "message": "没有可导入的内容"})
    return accounts, errors
