"""QQ空间 HTTP 传输层：统一携带登录态、解析响应、失效重登。"""

from typing import Any

import aiohttp
from astrbot.api import logger

from .constants import (
    HTTP_STATUS_FORBIDDEN,
    HTTP_STATUS_UNAUTHORIZED,
    QZONE_CODE_FORBIDDEN,
    QZONE_CODE_LOGIN_EXPIRED,
    QZONE_CODE_LOGIN_REQUIRED,
    QZONE_INTERNAL_HTTP_STATUS_KEY,
    QZONE_INTERNAL_META_KEY,
    QZONE_MSG_FORBIDDEN,
)
from .parser import QzoneParser
from .session import QzoneSession

# 只有这些才算「登录态真的失效了」，值得重新获取 Cookie 并重试一次；
# 「返回的是页面」（-3002）与 403（-3003）都不在其中：重登帮不上忙，只会多打一次请求。
_LOGIN_REQUIRED_CODES = (QZONE_CODE_LOGIN_EXPIRED, QZONE_CODE_LOGIN_REQUIRED)
# 失败时保留在 meta 里的原始响应片段长度（供上层日志诊断）
_SNIPPET_LIMIT = 300


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
        page_is_expected: bool = False,
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
            page_is_expected: 该接口本来就可能返回页面（回复接口成功时也回页面）：
                此时页面响应不写 error 日志，也不影响结论，由调用方自行判定。

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

        parsed = QzoneParser.parse_response(text, page_is_expected=page_is_expected)
        meta = parsed.get(QZONE_INTERNAL_META_KEY)
        if not isinstance(meta, dict):
            meta = {}
            parsed[QZONE_INTERNAL_META_KEY] = meta
        meta[QZONE_INTERNAL_HTTP_STATUS_KEY] = status
        # 失败时把原始响应片段留在 meta 里，供上层日志诊断（成功时不写，避免噪音）
        if parsed.get("message"):
            meta["snippet"] = QzoneParser.visible_snippet(text, _SNIPPET_LIMIT)

        # HTTP 403 单独判定：请求被拒绝，重取登录态解决不了，也不该被误当成登录失效
        if status == HTTP_STATUS_FORBIDDEN:
            logger.warning(
                f"QQ空间请求被拒绝（403）: {method} {url}｜响应片段: {meta.get('snippet')}"
            )
            denied = QzoneParser.error_payload(
                QZONE_MSG_FORBIDDEN, code=QZONE_CODE_FORBIDDEN
            )
            denied[QZONE_INTERNAL_META_KEY] = meta
            return denied

        # 明确登录失效（401 / -3000 / 解析层判定的登录页 -3001）时，
        # 重新获取 Cookie 并重试一次；发布、点赞、评论、回复等路径都由这里统一覆盖。
        # 注意「返回的是页面」（-3002）不在这里：重登帮不上忙。
        if (
            status == HTTP_STATUS_UNAUTHORIZED
            or parsed.get("code") in _LOGIN_REQUIRED_CODES
        ):
            if retry >= 1:
                raise RuntimeError(
                    "登录态可能已失效或被风控拦截，已自动重取登录态后仍然失败，"
                    "请用 /空间重登 重取后再试"
                )
            logger.warning(
                "QQ空间登录态可能已失效或被风控拦截，正在重新获取 Cookie 并重试"
            )
            await self.session.invalidate()
            return await self.request(
                method,
                url,
                params=params,
                data=data,
                headers=headers,
                timeout=timeout,
                retry=retry + 1,
                page_is_expected=page_is_expected,
            )

        return parsed
