"""QQ空间登录态管理。

Cookie 优先复用 OneBot（aiocqhttp）平台的登录态：直接调用 `get_cookies`
取回 `user.qzone.qq.com` 域的 Cookie，因此无需手动抓包；
若平台不支持该接口，可在插件配置中填写手动 Cookie 兜底。
"""

import asyncio
from collections.abc import Callable
from http.cookies import SimpleCookie
from time import monotonic
from typing import Any

from astrbot.api import logger

from ..config import PluginConfig
from .model import QzoneContext


class QzoneSession:
    """QQ空间登录上下文。

    Attributes:
        DOMAIN: 需要获取 Cookie 的目标域。
    """

    DOMAIN = "user.qzone.qq.com"

    def __init__(
        self,
        config: PluginConfig,
        client_provider: Callable[[], Any | None] | None = None,
    ) -> None:
        """初始化登录态管理器。

        Args:
            config: 插件配置。
            client_provider: 返回 OneBot 客户端实例的可调用对象，可为 None。
        """
        self.cfg = config
        self._client_provider = client_provider
        self._ctx: QzoneContext | None = None
        self._refreshed_at: float = 0.0
        self._source: str = ""
        self._lock = asyncio.Lock()

    @property
    def source(self) -> str:
        """本次登录态的 Cookie 来源：onebot / manual / 空。"""
        return self._source

    async def get_ctx(self) -> QzoneContext:
        """获取登录上下文，必要时自动刷新。

        Returns:
            当前可用的登录上下文。

        Raises:
            RuntimeError: 无法获取有效 Cookie 时抛出。
        """
        async with self._lock:
            if self._ctx is None or self._expired():
                self._ctx = await self._load_ctx()
            return self._ctx

    async def get_uin(self) -> int:
        """获取当前登录的 QQ 号。"""
        return (await self.get_ctx()).uin

    async def get_nickname(self) -> str:
        """获取当前登录 QQ 的昵称，失败时回退为 QQ 号。"""
        ctx = await self.get_ctx()
        client = self._client_provider() if self._client_provider else None
        if client is None:
            return str(ctx.uin)
        try:
            info = await client.get_login_info()
            if isinstance(info, dict):
                return str(info.get("nickname") or ctx.uin)
        except Exception as e:
            logger.debug(f"获取 QQ 昵称失败，回退为 QQ 号: {e}")
        return str(ctx.uin)

    async def invalidate(self) -> None:
        """使当前登录态失效，下次请求时重新获取。"""
        async with self._lock:
            self._ctx = None
            self._refreshed_at = 0.0

    async def refresh(self) -> QzoneContext:
        """强制刷新登录态。

        Returns:
            刷新后的登录上下文。
        """
        await self.invalidate()
        return await self.get_ctx()

    def _expired(self) -> bool:
        """判断缓存的 Cookie 是否超过配置的 TTL。"""
        ttl = int(self.cfg.cookie_ttl or 0)
        if ttl <= 0:
            return False
        if self._refreshed_at <= 0:
            return True
        return monotonic() - self._refreshed_at >= ttl

    async def _load_ctx(self) -> QzoneContext:
        """按优先级加载登录态：手动 Cookie -> OneBot Cookie。

        Returns:
            解析出的登录上下文。

        Raises:
            RuntimeError: 两条路径都拿不到可用 Cookie 时抛出。
        """
        manual = str(self.cfg.cookie or "").strip()
        if manual:
            ctx = self._parse_cookie(manual)
            self._source = "manual"
            self._refreshed_at = monotonic()
            logger.info(f"QQ空间登录态已就绪（手动 Cookie），uin={ctx.uin}")
            return ctx

        cookie_str = await self._fetch_cookie_from_onebot()
        ctx = self._parse_cookie(cookie_str)
        self._source = "onebot"
        self._refreshed_at = monotonic()
        logger.info(f"QQ空间登录态已就绪（OneBot），uin={ctx.uin}")
        return ctx

    async def _fetch_cookie_from_onebot(self) -> str:
        """调用 OneBot 的 get_cookies 接口取回空间域 Cookie。

        Returns:
            Cookie 字符串。

        Raises:
            RuntimeError: 平台不可用或未返回 Cookie 时抛出。
        """
        client = self._client_provider() if self._client_provider else None
        if client is None:
            raise RuntimeError(
                "未检测到 aiocqhttp(OneBot) 平台实例，且未配置手动 Cookie，"
                "无法登录 QQ空间"
            )

        payload: Any = None
        for kwargs in ({"domain": self.DOMAIN}, {}):
            try:
                payload = await client.get_cookies(**kwargs)
            except Exception as e:
                logger.debug(f"OneBot get_cookies{kwargs} 调用失败: {e}")
                continue
            cookie_str = self._extract_cookie_str(payload)
            if cookie_str:
                return cookie_str

        raise RuntimeError(
            "OneBot 的 get_cookies 未返回可用 Cookie。请确认 QQ 客户端"
            "（NapCat / Lagrange 等）已登录且支持该接口，或在插件配置中填写手动 Cookie"
        )

    @staticmethod
    def _extract_cookie_str(payload: Any) -> str:
        """从 OneBot 返回结构中提取 Cookie 字符串。"""
        if isinstance(payload, str):
            return payload.strip()
        if isinstance(payload, dict):
            for key in ("cookies", "cookie"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            inner = payload.get("data")
            if isinstance(inner, dict):
                for key in ("cookies", "cookie"):
                    value = inner.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return ""

    @staticmethod
    def _parse_cookie(cookie_str: str) -> QzoneContext:
        """把 Cookie 字符串解析为登录上下文。

        Args:
            cookie_str: 形如 `uin=o123456; skey=xxx; p_skey=yyy` 的 Cookie。

        Returns:
            解析出的登录上下文。

        Raises:
            RuntimeError: Cookie 缺少 uin 或 skey 时抛出。
        """
        jar = {k: v.value for k, v in SimpleCookie(cookie_str).items()}
        if not jar:
            raise RuntimeError(
                "Cookie 解析失败，请检查格式是否为 key=value; key2=value2"
            )

        uin_text = str(jar.get("uin") or "").strip()
        if uin_text[:1].lower() == "o":
            uin_text = uin_text[1:]
        if not uin_text.isdigit() or int(uin_text) <= 0:
            raise RuntimeError("Cookie 中缺少合法的 uin 字段")

        skey = str(jar.get("skey") or "").strip()
        if not skey:
            raise RuntimeError("Cookie 中缺少 skey 字段，无法通过 QQ空间校验")

        p_skey = str(jar.get("p_skey") or "").strip() or skey
        return QzoneContext(uin=int(uin_text), skey=skey, p_skey=p_skey)
