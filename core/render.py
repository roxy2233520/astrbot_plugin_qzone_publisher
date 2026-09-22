"""回执图渲染：把通知/草稿文本渲染成图片。

只使用 AstrBot 自带的渲染能力（``html_renderer``），不引入任何字体或图像依赖：

- ``render_t2i(text, use_network=False)`` 默认走**本地渲染**（内容不出本机）；
  ``notify_render_network`` 打开时走面板配置的 t2i 端点，网络失败 AstrBot 会自动回退本地；
- 渲染失败一律返回 None，由调用方降级为纯文本，绝不让回执整体失败。
"""

from __future__ import annotations

from pathlib import Path

from astrbot.api import logger

from .config import PluginConfig

# 过短的文本没必要做成图（渲染开销与观感都不划算）
MIN_RENDER_CHARS = 40


class ReceiptRenderer:
    """把文本渲染成图片路径（失败返回 None）。"""

    def __init__(self, config: PluginConfig) -> None:
        """初始化渲染器。

        Args:
            config: 插件配置。
        """
        self.cfg = config

    @property
    def enabled(self) -> bool:
        """是否开启回执图。"""
        return bool(self.cfg.notify_render_image)

    def status_text(self) -> str:
        """给 /空间状态 用的一行描述。"""
        if not self.enabled:
            return "关闭"
        mode = "网络 t2i" if bool(self.cfg.notify_render_network) else "本地渲染"
        return f"开启（{mode}）"

    async def render(self, text: str) -> str | None:
        """渲染文本为图片。

        Args:
            text: 回执文本。

        Returns:
            本地文件路径或图片 URL；未开启、文本过短或渲染失败时返回 None。
        """
        if not self.enabled:
            return None

        content = str(text or "").strip()
        if len(content) < MIN_RENDER_CHARS:
            logger.debug("回执文本过短，跳过渲染")
            return None

        try:
            from astrbot.api import html_renderer
        except Exception as e:  # pragma: no cover - 取决于 AstrBot 版本
            logger.warning(f"当前 AstrBot 没有可用的渲染器: {e}")
            return None

        use_network = bool(self.cfg.notify_render_network)
        try:
            result = await html_renderer.render_t2i(
                content, use_network=use_network, return_url=False
            )
        except Exception as e:
            logger.warning(f"回执图渲染失败，改用纯文本: {e}")
            return None

        target = str(result or "").strip()
        if not target:
            logger.warning("回执图渲染没有返回内容，改用纯文本")
            return None

        if target.startswith(("http://", "https://")):
            return target
        if Path(target).is_file():
            return target

        logger.warning(f"回执图渲染结果不可用（不是文件也不是 URL）: {target[:120]}")
        return None
