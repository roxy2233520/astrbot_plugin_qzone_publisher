"""一体化生活日程。

插件自己用 AI 生成「今日穿搭 + 今日日程」，按天缓存、懒加载并落盘到
``<插件数据目录>/life_schedule.json``，不依赖任何其他插件。

是否把日程注入 system prompt 由 ``life_inject_enabled`` 控制，**默认关闭**：
如果你另外装了会注入生活状态的插件，双份注入会让 Bot 出现两套矛盾的状态。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import random
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.core.star.context import Context

from .config import PluginConfig
from .llm import AIClient

WEEKDAYS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

DEFAULT_LIFE_PROMPT = """# Role: 生活日程规划
请为自己规划今天的生活安排。

## Context
- 日期：{date_str} {weekday} {holiday}
- 人设：{persona_desc}

## 生成前请先自己确认
- 上面的人设就是你的身份、职业、作息、兴趣与说话方式；
  **今天的安排必须像这个人真的会过的日子**，不能是任何人的通用模板。
- 如果你的人设是学生，就不要安排通勤上班；如果是上班族，就不要写上课；
  作息、常去的地方、在意的事都要贴合人设。

## 🎲 今日创意约束（必须遵循）
- 今日主题：【{daily_theme}】
- 心情色彩：【{mood_color}】
- 推荐穿搭风格：【{outfit_style}】
- 日程类型：【{schedule_type}】

## 🚫 最近几天已经安排过（避免重复）
{history_schedules}

## Tasks
1. outfit: 今日穿搭描述（从里到外、从上到下，含衣裤鞋袜与配饰，符合天气、心情与人设）
2. schedule: 今日日程，按时间段描述，具体到地点与在做什么，像真人一样自然

## Output Format
只返回 JSON，不要任何解释：
{{
  "outfit": "...",
  "schedule": "..."
}}"""


@dataclass(slots=True)
class LifeState:
    """某一天的生活状态。

    Attributes:
        date: 日期（yyyy-mm-dd）。
        outfit: 当日穿搭。
        schedule: 当日日程。
        source: 数据来源标识（当前固定为 builtin，由插件自己生成）。
        status: ok 或 failed。
    """

    date: str
    outfit: str = ""
    schedule: str = ""
    source: str = "builtin"
    status: str = "ok"

    @classmethod
    def from_dict(cls, data: dict) -> LifeState:
        """由字典构造，忽略未知字段。"""
        return cls(
            date=str(data.get("date") or ""),
            outfit=str(data.get("outfit") or ""),
            schedule=str(data.get("schedule") or ""),
            source=str(data.get("source") or "builtin"),
            status=str(data.get("status") or "ok"),
        )

    def to_line(self) -> str:
        """压成一行，便于拼进提示词。"""
        return f"穿搭：{self.outfit}｜日程：{self.schedule}"


def time_desc(hour: int | None = None) -> str:
    """把小时数转成中文时段。

    Args:
        hour: 小时数，缺省取当前时间。

    Returns:
        深夜 / 清晨 / 上午 / 中午 / 下午 / 晚上。
    """
    value = (hour if hour is not None else datetime.datetime.now().hour) % 24
    if value < 6:
        return "深夜"
    if value < 9:
        return "清晨"
    if value < 12:
        return "上午"
    if value < 14:
        return "中午"
    if value < 18:
        return "下午"
    if value < 22:
        return "晚上"
    return "深夜"


def extract_json_object(text: str) -> dict[str, Any] | None:
    """从模型输出里抠出第一个完整的 JSON 对象。

    自己数花括号而不是正则，这样字符串里出现的 ``{}`` 不会破坏解析。

    Args:
        text: 模型原始输出。

    Returns:
        解析成功的字典；失败返回 None。
    """
    cleaned = re.sub(r"^```[a-zA-Z0-9_-]*\s*|\s*```$", "", text.strip()).strip()
    start = cleaned.find("{")
    if start == -1:
        return None

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(cleaned)):
        char = cleaned[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    data = json.loads(cleaned[start : index + 1])
                except ValueError:
                    return None
                return data if isinstance(data, dict) else None
    return None


class LifeManager:
    """生活日程管理器（生成 / 读取 / 缓存 / 注入文本）。"""

    def __init__(self, config: PluginConfig, context: Context, ai: AIClient) -> None:
        """初始化管理器。

        Args:
            config: 插件配置。
            context: AstrBot 插件上下文。
            ai: AI 客户端。
        """
        self.cfg = config
        self.context = context
        self.ai = ai
        self.file = Path(config.data_dir) / "life_schedule.json"
        self._states: dict[str, LifeState] = {}
        self._lock = asyncio.Lock()
        self.load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def load(self) -> None:
        """从磁盘加载历史日程。"""
        self._states = {}
        if not self.file.exists():
            return
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"生活日程文件读取失败，已忽略: {e}")
            return
        if not isinstance(raw, dict):
            return
        for date_str, item in raw.items():
            if isinstance(item, dict):
                state = LifeState.from_dict({**item, "date": date_str})
                self._states[date_str] = state

    def save(self) -> None:
        """原子写入磁盘，只保留最近 30 天。"""
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            keys = sorted(self._states)[-30:]
            payload = {key: asdict(self._states[key]) for key in keys}
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.file)
        except Exception as e:
            logger.error(f"生活日程写入失败: {e}")

    # ------------------------------------------------------------------
    # 读取
    # ------------------------------------------------------------------

    def cached(self, date: datetime.date) -> LifeState | None:
        """取缓存中的某天状态（不触发生成）。"""
        return self._states.get(date.isoformat())

    async def get_state(
        self, date: datetime.date | None = None, *, force: bool = False
    ) -> LifeState | None:
        """获取某天的生活状态，必要时生成。

        Args:
            date: 目标日期，缺省为今天。
            force: 为 True 时忽略缓存重新生成。

        Returns:
            生活状态；生成失败时 status 为 failed。

        Raises:
            RuntimeError: AI 不可用时在内部记录并返回 failed 状态。
        """
        target = date or datetime.date.today()
        key = target.isoformat()

        def usable(state: LifeState | None) -> bool:
            """缓存是否可直接复用（只复用成功的结果）。"""
            return state is not None and state.status == "ok"

        if not force and usable(cached := self._states.get(key)):
            return cached

        async with self._lock:
            if not force and usable(cached := self._states.get(key)):
                return cached

            state = await self._generate(target)
            self._states[key] = state
            self.save()
            return state

    # ------------------------------------------------------------------
    # 生成
    # ------------------------------------------------------------------

    async def _generate(self, date: datetime.date) -> LifeState:
        """用 AI 生成当天的穿搭与日程。"""
        context = {
            "date_str": date.strftime("%Y年%m月%d日"),
            "weekday": WEEKDAYS[date.weekday()],
            "holiday": self._holiday(date),
            "persona_desc": await self._persona(),
            "history_schedules": self._history(date),
            **self._pick_pool(),
        }

        template = str(self.cfg.life_prompt or "").strip() or DEFAULT_LIFE_PROMPT
        filled = self._fill(template, context)

        try:
            text = await self.ai.chat(
                system_prompt=(
                    "你要为上面这个人设安排今天的生活：先自己确认这个人是谁"
                    "（职业、作息、兴趣、说话方式），再按他的真实生活写日程与穿搭，"
                    "不要套用与身份不符的通用模板。"
                    "只输出要求的 JSON，不要输出解释、标题或代码块标记。"
                ),
                prompt=filled,
                provider_id=str(self.cfg.llm_life_provider_id or ""),
                feature="日程",
            )
        except Exception as e:
            logger.error(f"生活日程生成失败: {e}")
            return LifeState(date=date.isoformat(), status="failed", source="builtin")

        parsed = extract_json_object(text)
        if parsed:
            state = LifeState(
                date=date.isoformat(),
                outfit=str(parsed.get("outfit") or "").strip(),
                schedule=str(parsed.get("schedule") or "").strip(),
                source="builtin",
            )
        else:
            # 模型没按 JSON 返回时，把整段文本当日程，避免功能直接失效
            logger.warning("生活日程未按 JSON 返回，已降级为纯文本日程")
            state = LifeState(
                date=date.isoformat(),
                outfit="日常休闲装",
                schedule=text.strip(),
                source="builtin",
            )

        if not state.schedule:
            state.status = "failed"
        logger.info(f"生活日程生成完成: {state.date} -> {state.to_line()[:80]}")
        return state

    def _fill(self, template: str, context: dict[str, str]) -> str:
        """填充模板占位符，模板里有但上下文没有的字段留空。"""
        names = set(re.findall(r"\{(\w+)\}", template))
        values = {name: context.get(name, "") for name in names}
        try:
            return template.format(**values)
        except Exception as e:
            logger.warning(f"生活日程模板填充失败，已改用默认模板: {e}")
            return DEFAULT_LIFE_PROMPT.format(
                **{
                    **{
                        k: ""
                        for k in (
                            "daily_theme",
                            "mood_color",
                            "outfit_style",
                            "schedule_type",
                        )
                    },
                    **context,
                }
            )

    def _pick_pool(self) -> dict[str, str]:
        """从创意池里各随机抽一个，池子为空时返回空串。"""
        pool = self.cfg.life_pool
        pool = pool if isinstance(pool, dict) else {}

        def pick(key: str) -> str:
            values = pool.get(key)
            if isinstance(values, list):
                cleaned = [str(item).strip() for item in values if str(item).strip()]
                if cleaned:
                    return random.choice(cleaned)
            return ""

        return {
            "daily_theme": pick("daily_themes"),
            "mood_color": pick("mood_colors"),
            "outfit_style": pick("outfit_styles"),
            "schedule_type": pick("schedule_types"),
        }

    def _history(self, date: datetime.date) -> str:
        """取最近几天的日程，让模型避免重复。"""
        days = int(self.cfg.life_reference_days or 0)
        if days <= 0:
            return "（无历史记录）"

        lines: list[str] = []
        for offset in range(1, days + 1):
            past = date - datetime.timedelta(days=offset)
            state = self._states.get(past.isoformat())
            if not state or state.status != "ok":
                continue
            lines.append(f"[{state.date}] {state.to_line()[:120]}")
        return "\n".join(lines) if lines else "（无历史记录）"

    @staticmethod
    def _holiday(date: datetime.date) -> str:
        """取中国节假日名称，未安装 holidays 库时返回空串。"""
        try:
            import holidays
        except Exception:
            return ""

        try:
            name = holidays.CN().get(date)
        except Exception as e:
            logger.debug(f"查节假日失败: {e}")
            return ""
        return f"今天是 {name}" if name else ""

    async def _persona(self) -> str:
        """取 AstrBot 当前全局人格的提示词（只读 AstrBot 里配置好的那份）。"""
        return (await self.ai.fetch_persona()).get("prompt", "")

    # ------------------------------------------------------------------
    # 给其它模块用
    # ------------------------------------------------------------------

    async def prompt_context(self, date: datetime.date | None = None) -> str:
        """取一段可拼进其它提示词的日程上下文。

        Args:
            date: 目标日期，缺省今天。

        Returns:
            形如「时间：晚上｜穿搭：...｜日程：...」的文本；拿不到数据时返回空串。
        """
        try:
            state = await self.get_state(date)
        except Exception as e:
            logger.warning(f"获取生活日程失败: {e}")
            return ""

        if not state or state.status != "ok":
            return ""
        return (
            f"当前时间：{time_desc()}\n"
            f"今日穿搭：{state.outfit}\n"
            f"今日日程：{state.schedule}"
        )

    async def injection_text(self, date: datetime.date | None = None) -> str:
        """拼装注入 system prompt 的文本。"""
        content = await self.prompt_context(date)
        if not content:
            return ""
        return (
            "\n\n[内在状态 - 仅供理解角色当前心境，无需在对话中提及]\n"
            f"{content}\n\n"
            "[对话原则]\n"
            "- 专注于用户当前的话题和需求\n"
            "- 像真人一样自然对话，不解释自己在做什么、穿什么、在哪里\n"
            "- 只有当用户明确问起时，才简短回应相关问题\n"
            "- 上述内在状态只影响你的语气和情绪色彩，不作为对话内容"
        )
