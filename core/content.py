"""自动发布的内容生成：文案池 / 文本文件 / AI 生成。

AI 生成时可选注入：Bot 人设、今日生活日程（穿搭+日程）、最近聊天记录、
以及 AstrBot 自带联网搜索查到的近期资料。

为了避免「每天的说说都很像」，AI 生成路径还会做三件事（缺一层效果都不明显）：

1. 把最近若干条已发说说当作「不要重复」的参照交给模型；
2. 每次随机抽一个**创作角度**注入提示词，并保证同一天内各条角度不同；
3. 告诉模型**当前时段**（早上 / 中午 / 下午 / 晚上 / 深夜），只写当下这一刻的事。

生成后还会与最近内容算一次相似度，超过阈值就自动重写一次。

所有 AI 调用统一走 :class:`~.llm.AIClient`，因此既可以用 AstrBot 里配好的提供商，
也可以在插件配置里直接填 API 密钥。
"""

from __future__ import annotations

import json
import random
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astrbot.api import logger
from astrbot.core.star.context import Context

from .config import PluginConfig
from .llm import AIClient
from .ui import ICON_WARN, kv, plain_receipt

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注，避免运行期循环导入
    from .life import LifeManager
    from .store import PublishStore
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

# 时段划分（按插件时区）：(起始小时, 结束小时, 名称)，左闭右开；深夜跨零点单独处理
_TIME_SLOTS: tuple[tuple[int, int, str], ...] = (
    (5, 11, "早上"),
    (11, 13, "中午"),
    (13, 17, "下午"),
    (17, 22, "晚上"),
)
_NIGHT_SLOT = "深夜"

# 创作角度文件：记录每天已用过的角度，保证同一天内各条不重复
_ANGLE_FILE = "publish_angles.json"
_ANGLE_KEEP_DAYS = 7


def time_slot_name(moment: datetime) -> str:
    """按小时判断当前时段名。

    Args:
        moment: 时刻。

    Returns:
        早上 / 中午 / 下午 / 晚上 / 深夜 之一。
    """
    hour = int(moment.hour)
    for start, end, name in _TIME_SLOTS:
        if start <= hour < end:
            return name
    return _NIGHT_SLOT


def text_similarity(left: str, right: str) -> float:
    """计算两段文本的相似度（字符 bigram 的 Jaccard 系数）。

    中文没有词边界，按字符二元组比较比按词更稳；两边都太短时退回「完全相同才算相似」。

    Args:
        left: 文本一。
        right: 文本二。

    Returns:
        0.0 ~ 1.0 的相似度。
    """

    def grams(text: str) -> set[str]:
        cleaned = re.sub(r"\s+", "", str(text or ""))
        if len(cleaned) < 2:
            return {cleaned} if cleaned else set()
        return {cleaned[index : index + 2] for index in range(len(cleaned) - 1)}

    left_grams = grams(left)
    right_grams = grams(right)
    if not left_grams or not right_grams:
        return 0.0
    union = left_grams | right_grams
    if not union:
        return 0.0
    return len(left_grams & right_grams) / len(union)


class AngleTracker:
    """记录每天已经用过的创作角度，保证同一天内不重复。"""

    def __init__(self, path: Path | None) -> None:
        """初始化。

        Args:
            path: 落盘路径；为 None 时只保存在内存里（便于测试或没有数据目录的场景）。
        """
        self.path = Path(path) if path else None
        self._used: dict[str, list[str]] = {}
        self.load()

    def load(self) -> None:
        """从磁盘读取记录。"""
        self._used = {}
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"创作角度记录读取失败，已忽略: {e}")
            return
        if not isinstance(raw, dict):
            return
        for day, items in raw.items():
            if isinstance(items, list):
                self._used[str(day)] = [str(item) for item in items if str(item)]

    def save(self) -> None:
        """原子写入记录，只保留最近 7 天。"""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            keys = sorted(self._used)[-_ANGLE_KEEP_DAYS:]
            payload = {key: self._used[key] for key in keys}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except Exception as e:
            logger.error(f"创作角度记录写入失败: {e}")

    @staticmethod
    def day_key(moment: datetime) -> str:
        """当天的日期串。"""
        return moment.strftime("%Y-%m-%d")

    def used_today(self, moment: datetime) -> list[str]:
        """今天已经用过的角度。"""
        return list(self._used.get(self.day_key(moment), []))

    def pick(self, pool: list[str], moment: datetime) -> str:
        """从角度池里挑一个今天没用过的角度。

        Args:
            pool: 角度池（已去空）。
            moment: 当前时刻，用于判断「今天」。

        Returns:
            选中的角度；角度池为空时返回空串；池子今天已用完时允许重复并记日志。
        """
        if not pool:
            return ""
        day = self.day_key(moment)
        used = self._used.setdefault(day, [])
        candidates = [item for item in pool if item not in used]
        if not candidates:
            logger.info(f"创作角度池今天已经用完（{len(pool)} 个），本次允许重复使用")
            candidates = list(pool)
        chosen = random.choice(candidates)
        used.append(chosen)
        self.save()
        return chosen


class ContentGenerator:
    """按配置生成待发布的文本内容。"""

    def __init__(
        self,
        config: PluginConfig,
        ai: AIClient,
        life: LifeManager | None = None,
        web: WebSearchBridge | None = None,
        store: PublishStore | None = None,
    ) -> None:
        """初始化内容生成器。

        Args:
            config: 插件配置。
            ai: AI 客户端。
            life: 生活日程管理器，可为 None（不注入日程）。
            web: AstrBot 内置联网搜索桥，可为 None（不使用联网素材）。
            store: 发布历史，可为 None（不做「避免重复」的参照）。
        """
        self.cfg = config
        self.ai = ai
        self.life = life
        self.web = web
        self.store = store
        self.context: Context | None = getattr(ai, "context", None)
        self.last_generation: dict[str, Any] = {}
        self._last_umo = ""
        data_dir = getattr(config, "data_dir", None)
        self.angles = AngleTracker((Path(data_dir) / _ANGLE_FILE) if data_dir else None)

    def remember_umo(self, umo: str) -> None:
        """记录最近一次触发指令的会话，用于参考聊天记录。

        Args:
            umo: 统一消息来源标识。
        """
        if umo:
            self._last_umo = str(umo)

    # ------------------------------------------------------------------
    # 避免重复：最近内容 / 创作角度 / 当前时段
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        """当前时刻（按插件时区）。"""
        try:
            return datetime.now(self.cfg.timezone)
        except Exception:
            return datetime.now()

    def recent_texts(self) -> list[str]:
        """最近若干条已发说说的正文（用于「不要重复」）。

        Returns:
            正文列表，最新在前，每条截断到 60 字；未配置条数或没有历史时返回空列表。
        """
        count = int(self.cfg.publish_avoid_repeat_count or 0)
        if count <= 0 or self.store is None:
            return []
        return [item[:60] for item in self.store.recent_success_texts(count)]

    @property
    def angle_pool(self) -> list[str]:
        """创作角度池（去空、去重）。"""
        pool: list[str] = []
        for item in self.cfg.publish_angle_pool or []:
            text = str(item).strip()
            if text and text not in pool:
                pool.append(text)
        return pool

    def pick_angle(self) -> str:
        """挑一个今天没用过的创作角度。"""
        return self.angles.pick(self.angle_pool, self._now())

    def _avoid_repeat_parts(self, recent: list[str]) -> list[str]:
        """拼「不要重复」相关提示词段落。"""
        if not recent:
            return []
        listing = "\n".join(f"- {item}" for item in recent)
        return [
            "# 最近已经发过的内容（不要重复）\n"
            f"{listing}\n"
            "- 不要重复上面用过的意象、句式、开头方式与结尾方式\n"
            "- 换一个完全不同的切入角度写这一条"
        ]

    def _now_parts(self, angle: str) -> list[str]:
        """拼「创作角度 + 当前时段」提示词段落。"""
        parts: list[str] = []
        if angle:
            parts.append(
                "# 本次的写作切入角度\n"
                f"- {angle}\n"
                "- 只从这个角度写，不要面面俱到，也不要写成一天的总结"
            )
        slot = time_slot_name(self._now())
        parts.append(
            f"# 当前时段\n- 现在是{slot}，只写当下这个时段发生的事与感受，不要写一整天"
        )
        return parts

    def repeat_report(self, text: str, recent: list[str]) -> tuple[float, str]:
        """把生成的文本与最近内容比对。

        Args:
            text: 本次生成的正文。
            recent: 最近已发过的正文。

        Returns:
            二元组 (最高相似度 0~1, 最相似的那条)；没有可比较对象时为 (0.0, "")。
        """
        best = 0.0
        closest = ""
        for item in recent:
            score = text_similarity(text, item)
            if score > best:
                best, closest = score, item
        return best, closest

    def warning_note(self) -> str:
        """把「避免重复」相关的提醒拼成一行，供回执与状态展示。"""
        warning = str(self.last_generation.get("repeat_warning") or "").strip()
        if not warning:
            return ""
        return "\n" + plain_receipt(
            "需要注意", [kv("避免重复", warning)], icon=ICON_WARN
        )

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------

    async def generate(self) -> tuple[str, str]:
        """按配置生成一条发布内容。

        Returns:
            二元组 (文本, 来源标识)。

        Raises:
            RuntimeError: 配置无效或生成失败时抛出。
        """
        source = str(self.cfg.content_source or "pool").strip().lower()
        if source == "llm":
            return await self._generate_llm(), "llm"
        if source == "file":
            return self._from_file(), "file"
        if source != "pool":
            logger.warning(f"未知的内容来源 {source}，已回退为文案池")
        return self._from_pool(), "pool"

    async def _generate_llm(self) -> str:
        """AI 生成一条内容，并在必要时做一次「太像了就重写」。

        Returns:
            生成好的文本。

        Raises:
            RuntimeError: AI 不可用或返回为空时抛出。
        """
        recent = self.recent_texts()
        angle = self.pick_angle()
        text = await self.rewrite(angle=angle)

        threshold = max(min(int(self.cfg.publish_repeat_threshold or 0), 100), 0)
        checked = bool(recent) and 0 < threshold < 100
        if not checked:
            # 没开检查：也要把角度与参考条数留下来给状态指令看
            basis = self.last_generation
            basis["angle"] = angle
            basis["recent"] = len(recent)
            basis["repeat"] = 0
            basis["repeat_checked"] = False
            return text

        score, closest = self.repeat_report(text, recent)
        if score * 100 < threshold:
            basis = self.last_generation
            basis["angle"] = angle
            basis["recent"] = len(recent)
            basis["repeat"] = round(score * 100)
            basis["repeat_checked"] = True
            return text

        logger.warning(
            f"生成的内容与最近一条相似度 {score * 100:.0f}%（阈值 {threshold}%），"
            "自动重写一次"
        )
        hint = (
            "这次生成的内容与最近这条过于相似"
            f"：「{closest[:30]}」。换一个完全不同的切入角度重写，"
            "不要沿用它的意象、句式、开头与结尾。"
        )
        rewritten = await self.rewrite(previous=text, repeat_hint=hint, angle=angle)
        new_score, new_closest = self.repeat_report(rewritten, recent)
        # 重写会重新写一份生成依据，这里取最新的一份再补上「避免重复」的结果
        basis = self.last_generation
        basis["angle"] = angle
        basis["recent"] = len(recent)
        basis["repeat"] = round(new_score * 100)
        basis["repeat_checked"] = True
        basis["repeat_rewritten"] = True
        if new_score * 100 >= threshold:
            note = (
                f"重写后仍与最近的内容相似（{new_score * 100:.0f}%，阈值 {threshold}%），"
                "本次照常发布"
            )
            basis["repeat_warning"] = note
            basis["warnings"].append(note)
            logger.warning(f"{note}｜最相似的一条：{new_closest[:30]}")
        else:
            logger.info(
                f"重写后相似度降到 {new_score * 100:.0f}%（阈值 {threshold}%），采用重写结果"
            )
        return rewritten

    async def rewrite(
        self, previous: str = "", *, repeat_hint: str = "", angle: str = ""
    ) -> str:
        """强制用 AI 重新生成一版内容。

        Args:
            previous: 上一版内容，传入后会让模型避开重复表达。
            repeat_hint: 「与最近某条过于相似」的额外要求，重写时使用。
            angle: 本次的创作切入角度；留空表示不注入角度。

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

        repeat_parts = self._avoid_repeat_parts(self.recent_texts())
        parts.extend(repeat_parts)
        parts.extend(self._now_parts(angle))
        basis["avoid_repeat"] = bool(repeat_parts)
        basis["slot"] = time_slot_name(self._now())
        basis["angle"] = angle

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
        if repeat_hint:
            parts.append("# 必须避免的重复\n" + repeat_hint)

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
                "- 只取与当前时段相符的一处细节，不要罗列整天的日程表、不要写成流水账、"
                "也不要解释你在引用日程\n"
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
            f"，角度={basis.get('angle') or '无'}"
            f"，时段={basis.get('slot') or '未知'}"
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
