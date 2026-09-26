"""目录站抓取用的 HTTP 客户端（F-R20 / F-R21）。

只做"拿页面的字符串"，解析在 ``directory_sites``，落库在
``directory_sync_service``。HTTP 客户端可注入，测试用替身跑，不真的出网。

合规约定（需求书 3.3「不绕限制」）：带常规 UA、不伪造指纹、限速由同步服务控制；
遇到质询 / 4xx / 5xx 直接抛错并记录，不做任何绕过。
"""

from __future__ import annotations

from typing import Any

import httpx

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
DEFAULT_TIMEOUT = httpx.Timeout(connect=15.0, read=60.0, write=60.0, pool=30.0)


class DirectoryFetchError(RuntimeError):
    """目录站抓取失败（网络错误或非 2xx）。"""


class DirectoryFetcher:
    """一个目录站抓取器（httpx 长连接）。"""

    def __init__(
        self,
        *,
        timeout: Any = DEFAULT_TIMEOUT,
        user_agent: str = DEFAULT_USER_AGENT,
        client: Any = None,
    ) -> None:
        self.user_agent = user_agent
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )

    async def fetch(self, url: str) -> str:
        """取一个页面 / 接口，返回文本。失败一律抛 ``DirectoryFetchError``。"""
        try:
            response = await self._client.get(url)
        except httpx.HTTPError as exc:
            raise DirectoryFetchError(f"请求失败：{exc}") from exc
        status = int(getattr(response, "status_code", 0))
        text = getattr(response, "text", "") or ""
        if status < 200 or status >= 300:
            # 403 + Cf-Mitigated 就是质询：明确报出来，不重试不绕过
            mitigated = ""
            headers = getattr(response, "headers", {}) or {}
            if headers.get("cf-mitigated"):
                mitigated = "（站点要求人机校验，按合规约定不绕过）"
            raise DirectoryFetchError(f"HTTP {status}{mitigated}：{url}")
        return text

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if close is not None:
            await close()

    async def __aenter__(self) -> DirectoryFetcher:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()
