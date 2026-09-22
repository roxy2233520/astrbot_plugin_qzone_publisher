"""QQ空间 HTTP 传输层：统一携带登录态、解析响应、失效重登。"""

from typing import Any

import aiohttp
from astrbot.api import logger

from .constants import (
    HTTP_STATUS_UNAUTHORIZED,
    QZONE_CODE_LOGIN_EXPIRED,
    QZONE_INTERNAL_HTTP_STATUS_KEY,
    QZONE_INTERNAL_META_KEY,
)
from .parser import QzoneParser
from .session import QzoneSession


class QzoneHttpClient:
    """带登录态的 HTTP 客户端基类。

    Attributes:
        session: QQ空间登录态管理器。
        timeout: 默认请求超时（秒）。
    """

    def __init__(self, session: QzoneSession, timeout: int = 15) -> None:
        self.session = session
        self.timeout = max(int(timeout), 1)
        self._http: aiohttp.ClientSession | None = None

    async def _get_http(self) -> aiohttp.ClientSession:
        """惰性创建并复用 aiohttp 会话。"""
        if self._http is None or self._http.closed:
            self._http = aiohttp.ClientSession()
        return self._http

    async def close(self) -> None:
        """关闭底层 HTTP 会话。"""
        if self._http is not None and not self._http.closed:
            await self._http.close()
        self._http = None

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: int | None = None,
        retry: int = 0,
    ) -> dict[str, Any]:
        """发送一次带登录态的请求并返回解析后的响应。

        Args:
            method: HTTP 方法。
            url: 请求地址。
            params: URL 查询参数。
            data: 表单数据。
            headers: 额外请求头，默认使用登录态的请求头。
            timeout: 本次请求超时（秒），默认使用客户端超时。
            retry: 内部重试计数，调用方无需传入。

        Returns:
            解析后的响应字典，附带内部 HTTP 状态码。

        Raises:
            RuntimeError: 登录态反复失效时抛出。
        """
        ctx = await self.session.get_ctx()
        http = await self._get_http()

        async with http.request(
            method,
            url,
            params=params,
            data=data,
            headers=headers or ctx.headers(),
            cookies=ctx.cookies(),
            timeout=aiohttp.ClientTimeout(total=timeout or self.timeout),
        ) as resp:
            status = resp.status
            text = await resp.text()

        parsed = QzoneParser.parse_response(text)
        meta = parsed.get(QZONE_INTERNAL_META_KEY)
        if not isinstance(meta, dict):
            meta = {}
            parsed[QZONE_INTERNAL_META_KEY] = meta
        meta[QZONE_INTERNAL_HTTP_STATUS_KEY] = status

        # 仅在明确登录失效时重新获取 Cookie 并重试一次
        if (
            status == HTTP_STATUS_UNAUTHORIZED
            or parsed.get("code") == QZONE_CODE_LOGIN_EXPIRED
        ):
            if retry >= 1:
                raise RuntimeError("QQ空间登录态失效，重新获取 Cookie 后仍然失败")
            logger.warning("QQ空间登录态失效，正在重新获取 Cookie 并重试")
            await self.session.invalidate()
            return await self.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                timeout=timeout,
                retry=retry + 1,
            )

        return parsed
