"""Telegram Bot API（HTTP）最小客户端。

为什么机器人发言不走 Telethon：MTProto 里往超级群/频道发消息需要 access_hash，
而机器人拿不到——机器人调用 GetDialogs 会被 Telegram 直接拒绝
（实测报 BotMethodInvalidError）。Bot API 只认 chat_id，只要机器人是群成员就能发，
不需要解析实体，也不需要维护会话文件。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

DEFAULT_BASE_URL = "https://api.telegram.org"
# 连接快、读写慢：机器人上传视频动辄几十兆，读/写超时给足
DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=300.0, write=300.0, pool=30.0)

# 与 chats.chat_type 对齐
_CHANNEL_TYPES = ("channel", "supergroup")

MEDIA_PHOTO = "photo"
MEDIA_VIDEO = "video"
MEDIA_DOCUMENT = "document"


class BotApiError(RuntimeError):
    """Bot API 调用失败（带上 Telegram 的 description，便于排查）。"""


def bot_api_chat_id(tg_id: int, chat_type: str | None = None) -> str:
    """把系统里存的 tg_id 转成 Bot API 的 chat_id。

    频道 / 超级群要加 ``-100`` 前缀，普通群是 ``-id``。
    """
    if chat_type in _CHANNEL_TYPES:
        return f"-100{int(tg_id)}"
    return f"-{int(tg_id)}"


def media_method(kind: str) -> tuple[str, str]:
    """按内容类型选 Bot API 方法与表单字段名。"""
    if kind == MEDIA_PHOTO:
        return "sendPhoto", "photo"
    if kind == MEDIA_VIDEO:
        return "sendVideo", "video"
    return "sendDocument", "document"


class BotApiClient:
    """一个机器人 Token 对应一个客户端（httpx 长连接）。"""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: Any = DEFAULT_TIMEOUT,
        transport: Any = None,
    ) -> None:
        self.token = token
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/bot{token}",
            timeout=timeout,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def call(
        self,
        method: str,
        *,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
    ) -> Any:
        """调用一个 Bot API 方法，失败时抛 BotApiError。"""
        try:
            response = await self._client.post(f"/{method}", data=data, files=files)
        except httpx.HTTPError as exc:
            raise BotApiError(f"请求 Bot API 失败：{exc}") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise BotApiError(f"Bot API 返回了非 JSON 内容：{response.text[:200]}") from exc
        if not payload.get("ok"):
            description = payload.get("description") or response.text[:200]
            raise BotApiError(f"Telegram 拒绝了 {method}：{description}")
        return payload.get("result")

    async def get_me(self) -> dict[str, Any]:
        return await self.call("getMe") or {}

    async def get_chat(self, chat_id: str) -> dict[str, Any]:
        """取群资料（也用来确认机器人是否在群里）。"""
        return await self.call("getChat", data={"chat_id": chat_id}) or {}

    async def get_chat_member(self, chat_id: str, user_id: int) -> dict[str, Any]:
        """取成员身份，用来判断机器人自己有没有发言权限。"""
        return (
            await self.call(
                "getChatMember",
                data={"chat_id": chat_id, "user_id": int(user_id)},
            )
            or {}
        )

    async def copy_message(
        self,
        from_chat_id: str,
        message_id: int,
        to_chat_id: str,
        *,
        caption: str | None = None,
    ) -> dict[str, Any]:
        """把一条消息复制到目标（不显示「转发自」，也不下载文件）。

        前提是机器人能读到源消息（即它也在源群里）；读不到时调用方要退回
        「下载再上传」。
        """
        data: dict[str, Any] = {
            "chat_id": to_chat_id,
            "from_chat_id": from_chat_id,
            "message_id": int(message_id),
        }
        if caption is not None:
            data["caption"] = caption
        return await self.call("copyMessage", data=data) or {}

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        buttons: list[list[dict[str, Any]]] | None = None,
        disable_preview: bool = True,
    ) -> dict[str, Any]:
        """发文本（可选内联按钮）。"""
        data: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text or "",
            "disable_web_page_preview": str(bool(disable_preview)).lower(),
        }
        if buttons:
            data["reply_markup"] = json.dumps(
                {"inline_keyboard": buttons},
                ensure_ascii=False,
            )
        return await self.call("sendMessage", data=data) or {}

    async def send_media(
        self,
        chat_id: str,
        *,
        content: bytes,
        filename: str,
        caption: str | None = None,
        kind: str = MEDIA_DOCUMENT,
        buttons: list[list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """上传媒体（图片/视频/文件），可带文案。"""
        method, field = media_method(kind)
        data: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if buttons:
            data["reply_markup"] = json.dumps(
                {"inline_keyboard": buttons},
                ensure_ascii=False,
            )
        files = {field: (filename, content)}
        return await self.call(method, data=data, files=files) or {}
