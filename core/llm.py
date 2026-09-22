"""统一的 AI 文本生成接入层。

插件里所有「需要动脑子」的内容（说说文案、生活日程、评论、问候语）都从这里出，
并且**只使用 AstrBot 面板里已经配置好的 LLM 提供商**：

- 密钥、模型、超时、重试全部由 AstrBot 管理，插件不保存任何密钥；
- 插件自己不发 HTTP 请求，避免出现「绕过 AstrBot 提供商」的第二套通道。

``llm_provider_id`` 用于指定提供商（留空则用 AstrBot 当前默认提供商）；
个别功能（日程 / 评论 / 问候）还可以单独指定，但仍然必须是 AstrBot 里的提供商。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.star.context import Context

from .config import PluginConfig
from .usage import UsageStore, estimate_messages, estimate_tokens


class AIClient:
    """AstrBot 提供商的调用封装。

    Attributes:
        cfg: 插件配置。
        context: AstrBot 插件上下文。
        usage: Token 用量估算统计。
        last_call: 最近一次调用的估算信息。
    """

    def __init__(self, config: PluginConfig, context: Context) -> None:
        """初始化客户端。

        Args:
            config: 插件配置。
            context: AstrBot 插件上下文。
        """
        self.cfg = config
        self.context = context
        self.usage = UsageStore(Path(config.data_dir) / "token_usage.json")
        self.last_call: dict[str, Any] = {}
        self._umo = ""

    def remember_umo(self, umo: str) -> None:
        """记录最近活跃会话，用于按会话解析默认提供商。

        Args:
            umo: 统一消息来源标识。
        """
        if umo:
            self._umo = str(umo)

    # ------------------------------------------------------------------
    # 提供商解析
    # ------------------------------------------------------------------

    def effective_provider_id(self, override: str | None = None) -> str:
        """确定本次调用使用的提供商 id。

        Args:
            override: 某项功能单独指定的提供商 id，留空表示沿用全局设置。

        Returns:
            最终使用的提供商 id（空串表示用 AstrBot 当前默认提供商）。
        """
        override_id = str(override or "").strip()
        if override_id:
            return override_id
        return str(self.cfg.llm_provider_id or "").strip()

    def resolve_provider(self, override: str | None = None) -> Any | None:
        """从 AstrBot 取提供商实例。

        Args:
            override: 某项功能单独指定的提供商 id。

        Returns:
            提供商实例；找不到时返回 None。
        """
        provider_id = self.effective_provider_id(override)
        provider = None
        if provider_id:
            try:
                provider = self.context.get_provider_by_id(provider_id)
            except Exception as e:
                logger.warning(f"指定的 LLM 提供商 {provider_id} 不可用: {e}")

        if provider is None:
            try:
                provider = (
                    self.context.get_using_provider(self._umo)
                    if self._umo
                    else self.context.get_using_provider()
                )
            except Exception as e:
                logger.debug(f"获取默认 LLM 提供商失败: {e}")
                provider = None

        return provider if hasattr(provider, "text_chat") else None

    def available(self, override: str | None = None) -> bool:
        """当前是否有可用的 AstrBot 提供商。"""
        return self.resolve_provider(override) is not None

    def describe(self) -> str:
        """给 /空间状态 用的可读描述。"""
        if not self.available():
            return (
                "不可用：请先在 AstrBot 面板「服务提供商」里配置并启用一个 LLM 提供商"
            )
        provider_id = (
            str(self.cfg.llm_provider_id or "").strip() or "AstrBot 当前默认提供商"
        )
        return f"AstrBot 提供商（{provider_id}）"

    def overrides_text(self) -> str:
        """列出被单独指定了提供商的功能项。"""
        parts: list[str] = []
        for label, key in (
            ("日程", "llm_life_provider_id"),
            ("评论", "llm_comment_provider_id"),
            ("问候", "llm_greet_provider_id"),
        ):
            value = str(getattr(self.cfg, key) or "").strip()
            if value:
                parts.append(f"{label}={value}")
        return "，".join(parts)

    async def fetch_persona(self) -> dict[str, str]:
        """读取 AstrBot 当前的全局人格。

        插件只使用 AstrBot 里配置好的人格（``agent_runner`` 里选定的那个），
        不自己维护第二套人设。

        Returns:
            含 name / prompt 的字典；取不到时返回空字典。
        """
        try:
            persona = await self.context.persona_manager.get_default_persona_v3()
        except Exception as e:
            logger.debug(f"获取 AstrBot 人格失败: {e}")
            return {}

        if isinstance(persona, dict):
            return {
                "name": str(persona.get("name") or ""),
                "prompt": str(persona.get("prompt") or ""),
            }
        return {
            "name": str(getattr(persona, "name", "") or ""),
            "prompt": str(getattr(persona, "prompt", "") or ""),
        }

    async def persona_name(self) -> str:
        """取当前全局人格的名字，取不到时返回空串。"""
        return (await self.fetch_persona()).get("name", "")

    # ------------------------------------------------------------------
    # 调用
    # ------------------------------------------------------------------

    async def chat(
        self,
        *,
        system_prompt: str,
        prompt: str | None = None,
        contexts: list[dict[str, Any]] | None = None,
        provider_id: str | None = None,
        feature: str = "其他",
    ) -> str:
        """生成一段文本。

        Args:
            system_prompt: 系统提示词（人设、任务、输出要求）。
            prompt: 本条用户输入，与 contexts 二选一或叠加使用。
            contexts: 额外的多轮上下文。
            provider_id: 本次调用单独使用的 AstrBot 提供商 id；
                留空则沿用插件配置里的全局提供商。
            feature: 功能名，仅用于 Token 用量统计。

        Returns:
            模型返回的纯文本（已 strip）。

        Raises:
            RuntimeError: 没有可用提供商、调用失败或返回为空时抛出。
        """
        provider = self.resolve_provider(provider_id)
        if provider is None:
            raise RuntimeError(
                "没有可用的 AI：请在 AstrBot 面板「服务提供商」里配置并启用一个 LLM 提供商"
            )

        try:
            resp = await provider.text_chat(
                system_prompt=system_prompt,
                prompt=prompt,
                contexts=contexts or None,
            )
        except Exception as e:
            raise RuntimeError(f"调用 AstrBot 提供商失败: {e}") from e

        text = str(getattr(resp, "completion_text", "") or "").strip()
        if not text:
            raise RuntimeError("AI 返回内容为空")

        # 估算并累计 token 用量，供 /空间状态 与 /空间用量 展示
        prompt_tokens = estimate_messages(system_prompt, prompt, contexts)
        completion_tokens = estimate_tokens(text)
        self.usage.record(feature, prompt_tokens, completion_tokens)
        self.last_call = {
            "feature": feature,
            "prompt": prompt_tokens,
            "completion": completion_tokens,
            "total": prompt_tokens + completion_tokens,
        }
        logger.info(
            f"[{feature}] 生成完成，估算用量 输入 {prompt_tokens} + 输出 "
            f"{completion_tokens} tokens"
        )
        return text

    def last_call_text(self) -> str:
        """最近一次调用的可读描述。"""
        if not self.last_call:
            return "暂无记录"
        return (
            f"{self.last_call['feature']}：约 {self.last_call['prompt']} + "
            f"{self.last_call['completion']} = {self.last_call['total']} tokens（估算）"
        )
