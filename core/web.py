"""接入 AstrBot 自带的联网搜索。

本模块**不实现自己的搜索后端**，而是直接调用 AstrBot 内置的联网搜索工具
（``web_search_tavily`` / ``web_search_bocha`` / …），好处是：

- 服务商与密钥只在 AstrBot 面板里维护一份，插件不重复要 key；
- 搜索实现、密钥轮换、失败重试都跟随 AstrBot 升级，插件不用跟着改；
- 用户在 AstrBot 里换服务商，插件自动跟随。

调用链：``context.get_llm_tool_manager().get_builtin_tool(name)`` 拿到工具实例，
再用一个最小的 agent 上下文调用它。工具内部只从上下文里取配置与会话标识，
因此没有真实消息事件时也能安全调用（定时发布场景）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from astrbot.api import logger
from astrbot.core.star.context import Context

from .config import PluginConfig

# AstrBot 的联网搜索服务商 -> (内置工具名, 条数参数名, 密钥配置项)
PROVIDERS: dict[str, tuple[str, str | None, str]] = {
    "tavily": ("web_search_tavily", "max_results", "websearch_tavily_key"),
    "bocha": ("web_search_bocha", "count", "websearch_bocha_key"),
    "brave": ("web_search_brave", "count", "websearch_brave_key"),
    "firecrawl": ("web_search_firecrawl", None, "websearch_firecrawl_key"),
    "baidu_ai_search": ("web_search_baidu", None, "websearch_baidu_app_builder_key"),
    "exa": ("web_search_exa", None, "websearch_exa_key"),
    "anysearch": ("web_search_anysearch", "max_results", "websearch_anysearch_key"),
}

# 尽量复用 AstrBot 自己的旧配置归一化（单个字符串 -> 列表），避免旧配置取到错误的值
try:  # pragma: no cover - 取决于 AstrBot 版本
    from astrbot.core.tools.web_search_tools import normalize_legacy_web_search_config
except Exception:  # pragma: no cover
    normalize_legacy_web_search_config = None


@dataclass(slots=True)
class SearchHit:
    """一条搜索结果。

    Attributes:
        title: 标题。
        url: 链接。
        snippet: 摘要。
    """

    title: str
    url: str
    snippet: str

    @property
    def domain(self) -> str:
        """链接的域名，用于在不贴原文链接的前提下标注来源。"""
        try:
            return urlparse(self.url).netloc or ""
        except Exception:
            return ""


@dataclass(slots=True)
class SearchOutcome:
    """一次搜索的结果。

    Attributes:
        hits: 命中的结果。
        error: 失败原因，成功时为空串。
    """

    hits: list[SearchHit]
    error: str = ""

    def __bool__(self) -> bool:
        return bool(self.hits)


def key_ready(value: Any) -> bool:
    """判断 AstrBot 里某个搜索密钥配置项是否已填写。

    Args:
        value: 配置值，可能是列表（新格式）或字符串（旧格式）。

    Returns:
        是否至少有一个非空密钥。
    """
    if isinstance(value, (list, tuple, set)):
        return any(str(item).strip() for item in value)
    return bool(str(value or "").strip())


class WebSearchBridge:
    """AstrBot 内置联网搜索的调用桥。

    Attributes:
        last_error: 最近一次调用失败的原因，供状态展示。
    """

    def __init__(self, config: PluginConfig, context: Context) -> None:
        """初始化桥接器。

        Args:
            config: 插件配置。
            context: AstrBot 插件上下文。
        """
        self.cfg = config
        self.context = context
        self.last_error = ""
        self._umo = ""

    def remember_umo(self, umo: str) -> None:
        """记录最近活跃会话，用于按会话读取 AstrBot 配置。

        Args:
            umo: 统一消息来源标识。
        """
        if umo:
            self._umo = str(umo)

    # ------------------------------------------------------------------
    # 读取 AstrBot 的联网搜索配置
    # ------------------------------------------------------------------

    def _provider_settings(self, umo: str | None = None) -> dict:
        """取 AstrBot 配置里的 provider_settings。"""
        target = umo or self._umo
        cfg = None
        try:
            cfg = (
                self.context.get_config(target) if target else self.context.get_config()
            )
        except Exception as e:
            logger.debug(f"读取按会话配置失败，回退全局配置: {e}")
            try:
                cfg = self.context.get_config()
            except Exception as e2:
                logger.warning(f"读取 AstrBot 配置失败: {e2}")
                return {}

        # 复用 AstrBot 自己的旧配置归一化，避免旧格式（字符串）导致取错值
        if normalize_legacy_web_search_config is not None and cfg is not None:
            try:
                normalize_legacy_web_search_config(cfg)
            except Exception as e:
                logger.debug(f"归一化联网搜索配置失败（忽略）: {e}")

        settings = cfg.get("provider_settings", {}) if hasattr(cfg, "get") else {}
        return settings if isinstance(settings, dict) else {}

    def settings(self, umo: str | None = None) -> dict[str, Any]:
        """汇总当前生效的联网搜索设置。

        Args:
            umo: 会话标识，用于读取按会话覆盖的配置。

        Returns:
            含 enabled / provider / tool_name / count_param / key_field / key_ready
            等字段的字典。
        """
        settings = self._provider_settings(umo)
        provider = str(settings.get("websearch_provider") or "").strip()
        tool_name, count_param, key_field = PROVIDERS.get(provider, ("", None, ""))
        return {
            "enabled": bool(settings.get("web_search", False)),
            "provider": provider,
            "tool_name": tool_name,
            "count_param": count_param,
            "key_field": key_field,
            "key_ready": key_ready(settings.get(key_field)) if key_field else False,
            "supported": provider in PROVIDERS,
        }

    def readiness(self, umo: str | None = None) -> tuple[bool, str]:
        """判断现在能否调用 AstrBot 的联网搜索。

        Args:
            umo: 会话标识。

        Returns:
            二元组 (是否可用, 原因或说明)。
        """
        info = self.settings(umo)
        if not info["enabled"]:
            return False, "AstrBot 里未开启「联网搜索」（provider_settings.web_search）"
        if not info["supported"]:
            provider = info["provider"] or "未设置"
            return False, f"AstrBot 的联网搜索服务商「{provider}」不在已知列表内"
        if not info["key_ready"]:
            return False, (
                f"AstrBot 未填写 {info['provider']} 的搜索密钥（{info['key_field']}）"
            )
        if self._tool(info["tool_name"]) is None:
            # 内置联网搜索工具是 AstrBot 4.26 起才提供的
            return False, (
                f"未取到内置工具 {info['tool_name']}"
                "（内置联网搜索需要 AstrBot ≥4.26，本插件在该版本以下会自动跳过联网）"
            )
        return True, f"AstrBot 自带联网搜索（{info['provider']}）"

    def status_text(self, umo: str | None = None) -> str:
        """给 /空间状态 用的一行描述。"""
        info = self.settings(umo)
        if not bool(self.cfg.web_search_enabled):
            return f"关闭（AstrBot 侧：{info['provider'] or '未设置'}）"
        ok, reason = self.readiness(umo)
        text = reason if ok else f"不可用：{reason}"
        if self.last_error:
            text += f"｜最近失败：{self.last_error[:60]}"
        return text

    # ------------------------------------------------------------------
    # 调用内置工具
    # ------------------------------------------------------------------

    def _agent_context(self, umo: str | None, event: Any | None = None) -> Any:
        """构造调用工具所需的 agent 上下文。

        工具内部只用到 ``context.context.get_config(...)`` 与
        ``context.event.unified_msg_origin``，所以没有真实事件时用最小对象即可。

        Args:
            umo: 会话标识。
            event: 真实消息事件（有则优先使用，类型更真实）。

        Returns:
            可传给 ``tool.call()`` 的上下文对象。
        """
        if event is not None:
            try:  # pragma: no cover - 依赖 AstrBot 内部类型
                from astrbot.core.astr_agent_context import AstrAgentContext

                return AstrAgentContext(context=self.context, event=event)
            except Exception as e:
                logger.debug(f"构造 AstrAgentContext 失败，改用轻量上下文: {e}")

        return SimpleNamespace(
            context=self.context,
            event=SimpleNamespace(unified_msg_origin=umo or self._umo or ""),
        )

    def _tool(self, tool_name: str) -> Any | None:
        """从 AstrBot 的工具管理器取内置搜索工具实例。

        AstrBot 4.26 以下没有这套工具管理器，此时返回 None（调用方会降级处理）。
        """
        try:
            manager = self.context.get_llm_tool_manager()
        except Exception as e:
            logger.debug(f"获取 AstrBot LLM 工具管理器失败: {e}")
            return None

        try:
            return manager.get_builtin_tool(tool_name)
        except Exception as e:
            logger.debug(f"获取内置工具 {tool_name} 失败: {e}")
            return None

    @staticmethod
    def _result_text(result: Any) -> str:
        """把工具返回值统一成字符串（工具返回 str 或 MCP 结果对象）。"""
        if result is None:
            return ""
        if isinstance(result, str):
            return result.strip()
        for attr in ("content", "text", "output", "result"):
            value = getattr(result, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return str(result).strip()

    async def search(
        self,
        query: str,
        *,
        count: int | None = None,
        umo: str | None = None,
        event: Any | None = None,
    ) -> SearchOutcome:
        """调用 AstrBot 内置联网搜索。

        Args:
            query: 搜索词。
            count: 期望条数；不同服务商参数名不同，插件会自动适配。
            umo: 会话标识。
            event: 真实消息事件（可选）。

        Returns:
            SearchOutcome：成功时 hits 非空，失败时 error 说明原因。
        """
        self.last_error = ""
        text = str(query or "").strip()
        if not text:
            return SearchOutcome([], "搜索词为空")

        ok, reason = self.readiness(umo)
        if not ok:
            self.last_error = reason
            logger.warning(f"跳过联网搜索：{reason}")
            return SearchOutcome([], reason)

        info = self.settings(umo)
        tool = self._tool(info["tool_name"])
        if tool is None:
            self.last_error = f"未取到内置工具 {info['tool_name']}"
            logger.warning(self.last_error)
            return SearchOutcome([], self.last_error)

        kwargs: dict[str, Any] = {"query": text}
        if info["count_param"] and count:
            kwargs[info["count_param"]] = max(int(count), 1)

        try:
            raw = await tool.call(self._agent_context(umo, event), **kwargs)
        except Exception as e:
            self.last_error = f"调用 {info['tool_name']} 异常: {e}"
            logger.warning(self.last_error)
            return SearchOutcome([], self.last_error)

        payload = self._result_text(raw)
        if not payload:
            self.last_error = "联网搜索没有返回内容"
            return SearchOutcome([], self.last_error)
        if payload.startswith(("Error", "error")):
            self.last_error = payload[:160]
            logger.warning(f"联网搜索返回错误: {self.last_error}")
            return SearchOutcome([], self.last_error)

        hits = self._parse_hits(payload)
        if not hits:
            self.last_error = "联网搜索返回内容无法解析"
            return SearchOutcome([], self.last_error)

        logger.info(f"联网搜索完成：query={text!r}，命中 {len(hits)} 条")
        return SearchOutcome(hits)

    @staticmethod
    def _parse_hits(payload: str) -> list[SearchHit]:
        """解析内置工具返回的 JSON。"""
        try:
            data = json.loads(payload)
        except ValueError:
            logger.debug(f"联网搜索返回非 JSON：{payload[:120]}")
            return []

        items = data.get("results") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []

        hits: list[SearchHit] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            hits.append(
                SearchHit(
                    title=str(item.get("title") or "").strip(),
                    url=str(item.get("url") or "").strip(),
                    snippet=str(item.get("snippet") or "").strip(),
                )
            )
        return [hit for hit in hits if hit.title or hit.snippet]

    # ------------------------------------------------------------------
    # 拼装给模型看的素材
    # ------------------------------------------------------------------

    @staticmethod
    def format_for_prompt(hits: list[SearchHit], *, max_chars: int = 1200) -> str:
        """把搜索结果整理成提示词素材。

        只给标题、来源域名与摘要，**不给完整链接**，减少模型把链接贴进说说的概率。

        Args:
            hits: 搜索结果。
            max_chars: 素材总长度上限。

        Returns:
            可直接拼进提示词的文本；没有内容时返回空串。
        """
        lines: list[str] = []
        for index, hit in enumerate(hits, 1):
            title = hit.title or "（无标题）"
            domain = f"｜来源：{hit.domain}" if hit.domain else ""
            snippet = hit.snippet.replace("\n", " ")[:200]
            lines.append(f"{index}. {title}{domain}\n   {snippet}")

        text = "\n".join(lines)
        if len(text) > max_chars:
            text = text[:max_chars].rstrip()
        return text

    @staticmethod
    def format_hits(hits: list[SearchHit]) -> str:
        """把搜索结果整理成给人看的列表（含完整链接，便于人工核对）。"""
        if not hits:
            return "没有搜到结果"
        lines: list[str] = []
        for index, hit in enumerate(hits, 1):
            lines.append(f"{index}. {hit.title or '（无标题）'}")
            if hit.url:
                lines.append(f"   {hit.url}")
            if hit.snippet:
                snippet = hit.snippet.replace("\n", " ")[:120]
                lines.append(f"   {snippet}")
        return "\n".join(lines)
