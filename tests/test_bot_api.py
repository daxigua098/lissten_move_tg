"""Bot API 客户端：chat_id 转换与错误处理。"""

from __future__ import annotations

import httpx
import pytest

from app.core.bot_api import BotApiClient, BotApiError, bot_api_chat_id


def test_chat_id_conversion_matches_bot_api_rules() -> None:
    # 频道与超级群要加 -100 前缀
    assert bot_api_chat_id(3906649240, "channel") == "-1003906649240"
    assert bot_api_chat_id(4491725476, "supergroup") == "-1004491725476"
    # 普通群只要负号
    assert bot_api_chat_id(5590277516, "group") == "-5590277516"
    # 类型缺失时按普通群处理（Telegram 会报错，至少不会发错目标）
    assert bot_api_chat_id(1234, None) == "-1234"


async def test_send_message_raises_readable_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "chat not found"})

    api = BotApiClient("123456:TEST-TOKEN-VALUE", transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(BotApiError, match="chat not found"):
            await api.send_message("-100123", "hi")
    finally:
        await api.close()


async def test_send_message_returns_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/sendMessage")
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    api = BotApiClient("123456:TEST-TOKEN-VALUE", transport=httpx.MockTransport(handler))
    try:
        result = await api.send_message("-100123", "hi")
    finally:
        await api.close()

    assert result["message_id"] == 42
