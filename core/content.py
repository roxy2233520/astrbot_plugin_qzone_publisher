"""自动发布的内容生成：文案池 / 文本文件 / AI 生成。

AI 生成时可选注入：Bot 人设、今日生活日程（穿搭+日程）、最近聊天记录、
以及 AstrBot 自带联网搜索查到的近期资料。
所有 AI 调用统一走 :class:`~.llm.AIClient`，因此既可以用 AstrBot 里配好的提供商，
也可以在插件配置里直接填 API 密钥。
"""

from __future__ import annotations

import json
import random
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.core.star.context import Context

from .config import PluginConfig
from .llm import AIClient

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免运行期循环导入
    from .life import LifeManager
    from .web import WebSearchBridge

_CODE_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")

DEFAULT_POST_PROMPT = (
    "请以第一人称写一条发在QQ空间的说说，内容贴近你的日常与心情，"
    "简短自然，不要出现“说说”“动态”等词，不要加引号、标题或解释。"
)

DEFAULT_SEARCH_QUERY_PROMPT = (
    "结合你此刻的生活状态，想一个你真心好奇、适合搜一下的话题，"
    "只输出一句搜索关键词（不要引号、不要解释、不要标点结尾）。"
)


class ContentGenerator:
    """按配置生成待发布的文本内容。"""

    def __init__(
        self,
        config: PluginConfig,
        ai: AIClient,
        life: LifeManager | None = None,
        web: WebSearchBridge | None = None,
    ) -> None:
        """初始化内容生成器。

        Args:
            config: 插件配置。
            ai: AI 客户端。
            life: 生活日程管理器，可为 None（不注入日程）。
            web: AstrBot 内置联网搜索桥，可为 None（不使用联网素材）。
        """
        self.cfg = config
        self.ai = ai
        self.life = life
        self.web = web
        self.context: Context | None = getattr(ai, "context", None)
        self.last_generation: dict[str, Any] = {}
        self._last_umo = ""

    def remember_umo(self, umo: str) -> None:
        """记录最近一次触发指令的会话，用于参考聊天记录。

        Args:
            umo: 统一消息来源标识。
        """
        if umo:
            self._last_umo = str(umo)

    async def generate(self) -> tuple[str, str]:
        """按配置生成一条发布内容。

        Returns:
            二元组 (文本, 来源标识)。

        Raises:
            RuntimeError: 配置无效或生成失败时抛出。
        """
        source = str(self.cfg.content_source or "pool").strip().lower()
        if source == "llm":
            return await self.rewrite(), "llm"
        if source == "file":
            return self._from_file(), "file"
        if source != "pool":
            logger.warning(f"未知的内容来源 {source}，已回退为文案池")
        return self._from_pool(), "pool"

    async def rewrite(self, previous: str = "") -> str:
        """强制用 AI 重新生成一版内容。

        Args:
            previous: 上一版内容，传入后会让模型避开重复表达。

        Returns:
            生成好的文本。

        Raises:
            RuntimeError: AI 不可用或返回为空时抛出。
        """
        parts: list[str] = []
        basis: dict[str, Any] = {
            "persona": "",
            "life": False,
            "web": False,
            "chat": False,
            "warnings": [],
        }

        if bool(self.cfg.llm_use_persona):
            persona = await self.ai.fetch_persona()
            if persona.get("prompt"):
                basis["persona"] = persona.get("name") or "（未命名）"
                parts.append(f"# 人设\n{persona['prompt']}")
            else:
                basis["warnings"].append("没取到 AstrBot 人格")
                logger.warning("没取到 AstrBot 人格，本次生成不带人设")

        task = str(self.cfg.llm_prompt or "").strip() or DEFAULT_POST_PROMPT
        parts.append(f"# 任务\n{task}")

        if bool(self.cfg.llm_use_life_context) and self.life is not None:
            life_context = await self.life.prompt_context()
            if life_context:
                basis["life"] = True
                parts.append(
                    "# 你此刻的生活状态（说说要与之自然呼应）\n" + life_context
                )
            else:
                # 日程生成失败（例如 AI 不可用）时不该静默：记下来给用户看
                basis["warnings"].append("今日日程不可用，本次生成未参考日程")
                logger.warning("今日日程不可用，本次说说未参考日程")

        material = await self._web_material()
        if material:
            basis["web"] = True
            parts.append("# 刚刚联网查到的近期资料（仅供取材）\n" + material)
            parts.append(
                "# 联网素材使用规则\n"
                "- 只依据上面的资料陈述事实，不要编造资料之外的细节\n"
                "- 不要照抄标题或原文句子，用自己的话重写；不要贴链接\n"
                "- 资料与你想写的话题无关时，直接忽略它"
            )

        parts.append(
            "# 输出要求\n"
            "只输出可以直接发布的说说正文，"
            "不要引号、标题、解释、前缀或列表符号。"
        )
        if previous:
            parts.append(
                "# 上一版（已被否决，不要重复同样的表达）\n" + previous.strip()[:200]
            )

        # 让 AI 自己先确认「我是谁、我今天在做什么」，再把它们引用进正文
        if basis["life"] and bool(self.cfg.llm_life_must_reference):
            parts.append(
                "# 生成前请先自己确认（不用输出确认过程）\n"
                "- 我是谁：上面的「人设」就是你的身份、性格与说话方式，"
                "语气、用词、在意的事都必须符合它\n"
                "- 我今天在做什么：上面的「你此刻的生活状态」就是你正在过的这一天，"
                "不是别人写的设定\n"
                "\n# 引用要求\n"
                "- 正文里要自然带出今天行程中的**具体细节**（正在做的事、去过的地方、"
                "身上的穿搭等），至少一处，让这条说说与你今天的生活真正连得上\n"
                "- 不要罗列日程表、不要写成流水账、也不要解释你在引用日程，"
                "像随手记一句那样提一下即可\n"
                "- 细节可以与日程呼应但要用你自己的话，不要照抄日程原文"
            )
        elif bool(self.cfg.llm_use_life_context):
            # 今天还没生成日程（或生成失败）时，让 AI 自己先安排再写
            parts.append(
                "# 今天还没有安排\n"
                "- 先按你的身份与性格，为自己安排一下今天（几点做什么、穿什么），"
                "再据此写这条说说\n"
                "- 安排要符合人设的作息、职业与兴趣，不要凭空捏造与身份不符的生活"
            )

        system_prompt = "\n\n".join(parts)

        prompt: str | None = None
        if bool(self.cfg.llm_reference_chat):
            chat = await self._recent_chat()
            if chat:
                basis["chat"] = True
                prompt = f"以下是我最近的聊天记录，可作为写说说的素材：\n{chat}"

        text = self._clean(
            await self.ai.chat(
                system_prompt=system_prompt, prompt=prompt, feature="说说"
            )
        )
        if not text:
            raise RuntimeError("AI 生成的说说内容为空")

        basis["tokens"] = dict(self.ai.last_call)
        self.last_generation = basis
        logger.info(
            f"AI 生成的说说内容: {text}｜依据: 人设={basis['persona'] or '无'}"
            f"，日程={'有' if basis['life'] else '无'}"
            f"，联网={'有' if basis['web'] else '无'}"
            f"，约 {basis['tokens'].get('total', 0)} tokens"
        )
        return text

    # ------------------------------------------------------------------
    # 联网素材（走 AstrBot 自带的联网搜索）
    # ------------------------------------------------------------------

    async def _web_material(self) -> str:
        """按需联网取素材。

        任何失败（AstrBot 未开联网、没填密钥、搜索报错）都只记日志并返回空串，
        绝不影响正常生成。

        Returns:
            可直接拼进提示词的素材文本；不需要或失败时返回空串。
        """
        if not bool(self.cfg.web_search_enabled):
            return ""
        if self.web is None:
            logger.warning("已开启联网素材，但联网搜索桥未初始化")
            return ""

        query = await self._search_query()
        if not query:
            return ""

        outcome = await self.web.search(
            query,
            count=int(self.cfg.web_search_count or 5),
            umo=self._last_umo,
        )
        if not outcome:
            logger.warning(f"联网素材获取失败，本次不使用素材：{outcome.error}")
            return ""
        return self.web.format_for_prompt(outcome.hits)

    async def _search_query(self) -> str:
        """决定这一次搜什么。

        Returns:
            搜索词；无法确定时返回空串（调用方会跳过联网）。
        """
        pool = [
            str(item).strip()
            for item in (self.cfg.web_search_query_pool or [])
            if str(item).strip()
        ]
        mode = str(self.cfg.web_search_query_mode or "ai").strip().lower()

        if mode != "ai":
            if not pool:
                logger.warning("联网搜索关键词池为空，跳过联网")
                return ""
            return random.choice(pool)

        hints = f"\n可以参考这些方向：{'、'.join(pool)}" if pool else ""
        life_context = ""
        if self.life is not None:
            life_context = await self.life.prompt_context()

        prompt = (f"{life_context}{hints}").strip() or "随便想一个你好奇的话题"
        system_prompt = (
            str(self.cfg.web_search_query_prompt or "").strip()
            or DEFAULT_SEARCH_QUERY_PROMPT
        )
        try:
            raw = await self.ai.chat(
                system_prompt=system_prompt, prompt=prompt, feature="搜索词"
            )
        except Exception as e:
            logger.warning(f"生成搜索词失败，回退到关键词池: {e}")
            return random.choice(pool) if pool else ""

        query = " ".join(raw.split()).strip("\"'“”‘’")
        if len(query) > 40:
            query = query[:40]
        if query:
            logger.info(f"本次联网搜索词: {query!r}")
        return query

    # ------------------------------------------------------------------
    # 非 AI 来源
    # ------------------------------------------------------------------

    def _from_pool(self) -> str:
        """从文案池随机取一条。"""
        pool = [
            str(item).strip()
            for item in (self.cfg.text_pool or [])
            if str(item).strip()
        ]
        if not pool:
            raise RuntimeError("文案池为空，请补充配置 text_pool 或改用其他内容来源")
        return random.choice(pool)

    def _from_file(self) -> str:
        """从文本文件随机取一行（忽略空行与 # 注释）。"""
        path_text = str(self.cfg.content_file or "").strip()
        if not path_text:
            raise RuntimeError("未配置文案文件路径 content_file")

        path = Path(path_text).expanduser()
        if not path.is_file():
            raise RuntimeError(f"文案文件不存在: {path}")

        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except Exception as e:
            raise RuntimeError(f"文案文件读取失败: {e}") from e

        candidates = [
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        ]
        if not candidates:
            raise RuntimeError(f"文案文件没有可用内容: {path}")
        return random.choice(candidates)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    async def _persona_prompt(self) -> str:
        """读取默认人设的提示词。"""
        if self.context is None:
            return ""
        try:
            persona = await self.context.persona_manager.get_default_persona_v3()
        except Exception as e:
            logger.debug(f"获取人设失败: {e}")
            return ""

        if isinstance(persona, dict):
            prompt = persona.get("prompt")
        else:
            prompt = getattr(persona, "prompt", None)
        return str(prompt or "").strip()

    def _chat_umo(self) -> str:
        """返回参考聊天记录所用的会话标识。"""
        return str(self.cfg.llm_chat_umo or "").strip() or self._last_umo

    async def _recent_chat(self) -> str:
        """读取指定会话的最近聊天记录文本。"""
        count = int(self.cfg.llm_chat_count or 0)
        umo = self._chat_umo()
        if self.context is None or not umo or count <= 0:
            return ""

        try:
            manager = self.context.conversation_manager
            conversation_id = await manager.get_curr_conversation_id(umo)
            if not conversation_id:
                return ""
            conversation = await manager.get_conversation(umo, conversation_id)
            if not conversation or not conversation.history:
                return ""
            history = json.loads(conversation.history)
        except Exception as e:
            logger.warning(f"读取参考聊天记录失败: {e}")
            return ""

        if not isinstance(history, list):
            return ""

        lines: list[str] = []
        for message in history[-count:]:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            speaker = "用户" if message.get("role") == "user" else "我"
            lines.append(f"{speaker}: {content.strip()}")
        return "\n".join(lines)

    def _clean(self, text: str) -> str:
        """清洗 AI 输出：去掉代码围栏、包裹引号，并按配置截断。"""
        text = _CODE_FENCE.sub("", text.strip()).strip()
        text = text.strip("\"'“”‘’")
        text = re.sub(r"\n{3,}", "\n\n", text)

        limit = int(self.cfg.llm_max_chars or 0)
        if limit > 0 and len(text) > limit:
            text = text[:limit].rstrip()
        return text.strip()
