"""主动私聊消息：定时问候（早安 / 晚安）、传统节日祝福与日常闲聊。

- **发给谁**：``greet_users`` 里的 QQ 号，逐个私聊发送；开启
  ``active_msg_require_optin`` 时，只有明确接受过主动消息的用户才会收到（见
  :mod:`core.user_prefs`），未接受的人只计入「因未接受主动消息跳过」。
- **什么时候发**：``greet_morning_cron`` / ``greet_night_cron`` 各自独立，留空即关闭该时段；
  节日祝福走 ``holiday_cron``，当天不是内置节日就不发；日常闲聊走 ``chat_open_windows``
  （``HH:MM-HH:MM`` 时间窗口），在每个窗口内随机取一个时刻发一条。
- **发什么**：文案池随机取，或用 AI 结合人设与当日生活日程生成（``greet_use_ai``）；
  节日祝福用 ``holiday_prompt``（``{festival}`` 替换成节日名），失败回退 ``holiday_pool``；
  日常闲聊用 ``chat_open_prompt``，失败回退内置的三条温和文案。
- **不重复**：同一天同一时段对同一个人只发一次（落盘 ``greet_state.json``），
  节日祝福按 ``holiday:<日期>`` 记录、日常闲聊按 ``chat:<日期>:<QQ>`` 记录，
  避免抖动或补偿触发导致重复发送。
- 日常闲聊是**私聊**内容，且收件人已明确同意接收，因此**不走草稿确认**
  （与问候的 ``draft_for_greet`` 无关）：多一次确认会错过「闲聊」的时机。
- AI 调用只走 AstrBot 提供商（可用 ``llm_greet_provider_id`` / ``llm_holiday_provider_id`` /
  ``llm_chat_open_provider_id`` 单独指定）。
"""

from __future__ import annotations

import json
import random
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger

from .config import PluginConfig
from .holidays import as_date, festival_of, next_festival
from .ui import kv

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from .llm import AIClient

DEFAULT_GREET_PROMPT = (
    "用你自己的说话风格，给一个你很在意的人写一句{slot}问候，"
    "一到两句话，自然、有温度，不要解释、不要加引号、不要提“问候”“说说”这类词。"
)

# 节日祝福的功能标识（与用户偏好里的 features 键一致）
HOLIDAY_KEY = "holiday"

# 日常闲聊的功能标识（与用户偏好里的 features 键一致）
CHAT_KEY = "chat"

DEFAULT_HOLIDAY_PROMPT = (
    "用你自己的说话风格，给一个你很在意的人写一句{festival}祝福，"
    "一到两句话，自然、有温度，不要解释、不要加引号、不要分点或罗列。"
)

DEFAULT_CHAT_OPEN_PROMPT = (
    "用你自己的说话风格，主动给一个熟悉的人发一两句轻松的搭话。\n"
    "语气要求：轻松自然，不冒犯对方，不打探对方的隐私与私事，不给对方压力；"
    "不要用“在吗”“干嘛呢”这类没有内容的空招呼；只写一到两句话；"
    "可以自然提一句你今天在做什么；"
    "给对方留出不理会的余地，不要追问对方为什么不回复；"
    "不要解释、不要加引号、不要分点或罗列。"
)

# AI 不可用或返回为空时的兜底文案（内置，不占配置项）
CHAT_FALLBACK_POOL: tuple[str, ...] = (
    "刚忙完手头的事，忽然想起你，最近还好吧。",
    "我这边刚泡了杯茶，安静一会儿，你那边今天过得怎么样。",
    "今天天气挺舒服的，我这边一切都好，你也要照顾好自己。",
)

# 时间窗口写法：HH:MM-HH:MM（分隔符可用 - ~ ～ — 至 到）
_WINDOW_PATTERN = re.compile(
    r"^(\d{1,2}):(\d{1,2})\s*[-~～—至到]\s*(\d{1,2}):(\d{1,2})$"
)


@dataclass(frozen=True, slots=True)
class ChatWindow:
    """一个主动闲聊时间窗口。

    Attributes:
        text: 用户填写的原始写法，例如 ``19:00-22:00``。
        start_minute: 窗口起点（当天 0 点起的分钟数，0~1439）。
        span_minutes: 窗口长度（分钟）。跨零点的窗口按跨天计算，例如
            ``23:00-01:00`` 的跨度为 120 分钟。
    """

    text: str
    start_minute: int
    span_minutes: int

    @property
    def start_cron(self) -> str:
        """窗口起点对应的 5 段 Cron（配合抖动即可落在窗口内）。"""
        return f"{self.start_minute % 60} {self.start_minute // 60} * * *"

    @property
    def start_text(self) -> str:
        """窗口起点的 ``HH:MM`` 写法。"""
        return f"{self.start_minute // 60:02d}:{self.start_minute % 60:02d}"

    @property
    def duration_seconds(self) -> int:
        """窗口长度（秒），用作该窗口任务的随机抖动上限。"""
        return self.span_minutes * 60

    def contains(self, moment: datetime, *, grace_seconds: int = 0) -> bool:
        """判断某个时刻是否落在窗口内（跨零点窗口同样适用）。

        Args:
            moment: 待判断的时刻。
            grace_seconds: 允许的宽限秒数，用于补偿触发（错过一点点也算在窗口内）。

        Returns:
            在窗口内返回 True。
        """
        minutes = moment.hour * 60 + moment.minute + moment.second / 60.0
        delta = (minutes - self.start_minute) % (24 * 60)
        return delta < self.span_minutes + max(int(grace_seconds), 0) / 60.0


def parse_windows(spec: object) -> tuple[list[ChatWindow], list[str]]:
    """把「HH:MM-HH:MM」窗口配置解析成窗口列表。

    列表入参与逗号 / 顿号 / 分号 / 空格分隔的字符串都支持；无法识别的项会被忽略，
    由调用方写 warning 日志（面板里填错不至于让整个功能失效）。

    Args:
        spec: 配置值（列表或字符串）。

    Returns:
        (可用窗口列表, 无法识别的原始写法列表)。
    """
    if isinstance(spec, (list, tuple)):
        items = [str(item).strip() for item in spec]
    else:
        items = [part.strip() for part in re.split(r"[,，、;；\s]+", str(spec or ""))]

    windows: list[ChatWindow] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        match = _WINDOW_PATTERN.match(item)
        if not match:
            invalid.append(item)
            continue
        start_hour, start_minute, end_hour, end_minute = (
            int(p) for p in match.groups()
        )
        if not (
            0 <= start_hour <= 23
            and 0 <= start_minute <= 59
            and 0 <= end_hour <= 23
            and 0 <= end_minute <= 59
        ):
            invalid.append(item)
            continue
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        span = (end - start) % (24 * 60)
        if span <= 0:
            invalid.append(item)
            continue
        windows.append(ChatWindow(text=item, start_minute=start, span_minutes=span))
    return windows, invalid


def describe_windows(windows: list[ChatWindow]) -> str:
    """把窗口列表说成一行给人看的话。"""
    if not windows:
        return "未配置"
    return "、".join(window.text for window in windows)


@dataclass(slots=True)
class GreetSlot:
    """一个问候时段。

    Attributes:
        key: 时段标识（morning / night）。
        name: 中文名，用于日志与提示词。
        cron_key: 对应的时间配置项名。
        pool_key: 对应的文案池配置项名。
    """

    key: str
    name: str
    cron_key: str
    pool_key: str


SLOTS: tuple[GreetSlot, ...] = (
    GreetSlot("morning", "早安", "greet_morning_cron", "greet_morning_pool"),
    GreetSlot("night", "晚安", "greet_night_cron", "greet_night_pool"),
)


@dataclass(slots=True)
class GreetResult:
    """一次问候发送的汇总。

    Attributes:
        slot: 时段标识。
        text: 实际发送的内容。
        sent: 成功发送人数（AstrBot 确认找到平台、消息已交给协议端）。
        skipped: 跳过人数（今天已发过 / 没配目标）。
        blocked: 因用户未接受主动消息而跳过的人数。
        record: 是否写入「今日已问候」记录；手动测试时为 False，
            否则手动发一次会把当天的自动问候名额用掉。
        targets_used: 实际使用的发送地址（QQ -> UMO），用于排查发不出去的问题。
        errors: 出错信息。
    """

    slot: str
    text: str = ""
    sent: int = 0
    skipped: int = 0
    blocked: int = 0
    record: bool = True
    targets_used: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """生成可读汇总（统一排版：首行为统计，其余为键值行）。"""
        parts = [f"成功 {self.sent} 人，跳过 {self.skipped} 人"]
        if self.blocked:
            parts.append(kv("因未接受主动消息跳过", f"{self.blocked} 人"))
        if not self.record:
            parts.append(kv("说明", "手动发送（不占用今日自动问候名额）"))
        text = "\n".join(parts)
        if self.errors:
            text += "\n" + "\n".join(kv("失败", item) for item in self.errors[:5])
        return text


class GreetingService:
    """问候内容生成与发送。"""

    def __init__(
        self,
        config: PluginConfig,
        ai: AIClient,
        platform_id_provider: Callable[[], str],
        *,
        life_context_provider: Callable[[], object] | None = None,
        umo_resolver: Callable[[str], str] | None = None,
        opted_in_checker: Callable[[str, str], bool] | None = None,
        sender: Callable[[str, str], object] | None = None,
    ) -> None:
        """初始化服务。

        Args:
            config: 插件配置。
            ai: AI 客户端。
            platform_id_provider: 返回当前平台实例 id 的可调用对象，用于拼 UMO。
            life_context_provider: 可选的异步函数，返回当日日程文本。
            umo_resolver: 可选的 ``(qq) -> umo``，优先用「最近一次真实私聊会话地址」，
                拿不到时返回空串表示回退到按平台 id 拼装。
            opted_in_checker: 可选的 ``(qq, feature) -> bool``，判断该用户是否接受
                这个功能的主动消息；返回 False 的人会被跳过并计入 blocked。
            sender: 可选的异步发送函数 ``(umo, text) -> bool``，便于测试注入。
        """
        self.cfg = config
        self.ai = ai
        self._platform_id_provider = platform_id_provider
        self._life_context_provider = life_context_provider
        self._umo_resolver = umo_resolver
        self._optin = opted_in_checker
        self._sender = sender
        self.file = Path(config.data_dir) / "greet_state.json"
        self._sent: dict[str, list[str]] = {}
        self.load()

    # ------------------------------------------------------------------
    # 发送记录（去重）
    # ------------------------------------------------------------------

    def load(self) -> None:
        """加载今日已发送记录。"""
        self._sent = {}
        if not self.file.exists():
            return
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"问候记录读取失败，已忽略: {e}")
            return
        if isinstance(raw, dict):
            self._sent = {
                str(key): [str(item) for item in value]
                for key, value in raw.items()
                if isinstance(value, list)
            }

    def save(self) -> None:
        """原子写入记录，只保留最近 7 天。"""
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            keys = sorted(self._sent)[-7:]
            payload = {key: self._sent[key] for key in keys}
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.file)
        except Exception as e:
            logger.error(f"问候记录写入失败: {e}")

    @staticmethod
    def _today() -> str:
        """今天的日期串。"""
        return datetime.now().strftime("%Y-%m-%d")

    def _already_sent(self, slot: str, qq: str) -> bool:
        """今天该时段是否已经给这个人发过。"""
        return f"{slot}:{qq}" in self._sent.get(self._today(), [])

    def _mark_sent(self, slot: str, qq: str) -> None:
        """标记今天该时段已给这个人发过。"""
        today = self._today()
        records = self._sent.setdefault(today, [])
        key = f"{slot}:{qq}"
        if key not in records:
            records.append(key)

    def sent_today(self, slot: str) -> int:
        """今天该时段已发送的人数。"""
        prefix = f"{slot}:"
        return len(
            [
                item
                for item in self._sent.get(self._today(), [])
                if item.startswith(prefix)
            ]
        )

    # ------------------------------------------------------------------
    # 目标与内容
    # ------------------------------------------------------------------

    @property
    def targets(self) -> list[str]:
        """问候目标 QQ 号列表。"""
        values = self.cfg.greet_users or []
        return [str(item).strip() for item in values if str(item).strip()]

    def umo_for(self, qq: str) -> str:
        """把 QQ 号拼成私聊 UMO。

        优先用「最近一次与该 QQ 的真实私聊会话地址」——那是 AstrBot 自己用的地址，
        一定和平台对得上；拿不到时才按平台实例 id 拼 ``平台:FriendMessage:QQ``。
        """
        if self._umo_resolver is not None:
            try:
                resolved = str(self._umo_resolver(qq) or "").strip()
            except Exception as e:
                logger.debug(f"解析 {qq} 的会话地址失败: {e}")
                resolved = ""
            if resolved:
                return resolved
        return f"{self._platform_id_provider()}:FriendMessage:{qq}"

    def slot_of(self, key: str) -> GreetSlot | None:
        """按时段标识取配置。"""
        wanted = str(key or "").strip().lower()
        for slot in SLOTS:
            if slot.key == wanted:
                return slot
        return None

    async def build_text(self, slot: GreetSlot) -> str:
        """生成这个时段的问候内容。

        Args:
            slot: 问候时段。

        Returns:
            问候文本。

        Raises:
            RuntimeError: 文案池为空且 AI 也不可用时抛出。
        """
        if not bool(self.cfg.greet_use_ai):
            return self._from_pool(slot)

        prompt_template = (
            str(self.cfg.greet_prompt or "").strip() or DEFAULT_GREET_PROMPT
        )
        try:
            hint = prompt_template.format(slot=slot.name)
        except Exception:
            hint = prompt_template

        life_context = ""
        if self._life_context_provider is not None:
            try:
                life_context = str(await self._life_context_provider() or "")
            except Exception as e:
                logger.debug(f"获取问候用的日程上下文失败: {e}")

        try:
            text = await self.ai.chat(
                system_prompt=(
                    f"{hint}\n\n# 输出要求\n只输出问候正文，"
                    "不要引号、不要解释、不要换行分段。"
                ),
                prompt=life_context or None,
                provider_id=str(self.cfg.llm_greet_provider_id or ""),
                feature="问候",
            )
        except Exception as e:
            logger.warning(f"AI 生成问候失败，改用文案池: {e}")
            return self._from_pool(slot)

        cleaned = " ".join(text.split()).strip("\"'“”‘’")
        if not cleaned:
            return self._from_pool(slot)
        return cleaned

    def _from_pool(self, slot: GreetSlot) -> str:
        """从对应文案池随机取一条。"""
        return self._from_pool_key(
            slot.pool_key, slot.name, "且未开启 AI 生成（greet_use_ai）"
        )

    def _from_pool_key(
        self, pool_key: str, label: str, why: str = "且 AI 不可用"
    ) -> str:
        """从指定文案池随机取一条。

        Args:
            pool_key: 文案池配置项名。
            label: 出错提示里用的名字。
            why: 出错提示里的补充说明。

        Returns:
            文案池里的一条文案。

        Raises:
            RuntimeError: 文案池为空时抛出。
        """
        pool = [
            str(item).strip()
            for item in (getattr(self.cfg, pool_key, None) or [])
            if str(item).strip()
        ]
        if not pool:
            raise RuntimeError(f"{label}文案池为空，{why}")
        return random.choice(pool)

    @staticmethod
    def _fill_festival(text: str, festival: str) -> str:
        """把文案里的 ``{festival}`` 占位符替换成节日名。

        Args:
            text: 含占位符的文案。
            festival: 节日名。

        Returns:
            替换后的文案；占位符写法异常时原样返回。
        """
        try:
            return text.format(festival=festival)
        except Exception:
            return text.replace("{festival}", festival)

    async def build_holiday_text(self, festival: str) -> str:
        """生成节日祝福内容。

        Args:
            festival: 节日名，用于替换提示词里的 ``{festival}``。
            value_date: 不参与生成，仅用于调用方语义清晰。

        Returns:
            祝福文本；AI 不可用或返回为空时回退 ``holiday_pool``。

        Raises:
            RuntimeError: 节日文案池为空且 AI 也不可用时抛出。
        """
        name = str(festival or "").strip() or "节日"
        prompt_template = (
            str(self.cfg.holiday_prompt or "").strip() or DEFAULT_HOLIDAY_PROMPT
        )
        try:
            hint = prompt_template.format(festival=name)
        except Exception:
            hint = prompt_template

        life_context = ""
        if self._life_context_provider is not None:
            try:
                life_context = str(await self._life_context_provider() or "")
            except Exception as e:
                logger.debug(f"获取节日祝福用的日程上下文失败: {e}")

        try:
            text = await self.ai.chat(
                system_prompt=(
                    f"{hint}\n\n# 输出要求\n只输出祝福正文，"
                    "不要引号、不要解释、不要换行分段。"
                ),
                prompt=life_context or None,
                provider_id=str(self.cfg.llm_holiday_provider_id or ""),
                feature="节日祝福",
            )
        except Exception as e:
            logger.warning(f"AI 生成节日祝福失败，改用文案池: {e}")
            return self._fill_festival(
                self._from_pool_key("holiday_pool", "节日祝福"), name
            )

        cleaned = " ".join(text.split()).strip("\"'“”‘’")
        if not cleaned:
            return self._fill_festival(
                self._from_pool_key("holiday_pool", "节日祝福"), name
            )
        return cleaned

    # ------------------------------------------------------------------
    # 主动闲聊（定时主动开口）
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        """当前时刻（按插件配置的时区）。"""
        try:
            return datetime.now(self.cfg.timezone)
        except Exception:
            return datetime.now()

    @property
    def chat_windows(self) -> list[ChatWindow]:
        """配置里的主动闲聊时间窗口；无法识别的项会被忽略并写 warning。"""
        windows, invalid = parse_windows(self.cfg.chat_open_windows)
        if invalid:
            logger.warning(f"[chat] 忽略无法识别的时间窗口：{'、'.join(invalid)}")
        return windows

    @property
    def chat_per_day(self) -> int:
        """每天最多主动开口几次（至少 1，实际还受可用窗口数限制）。"""
        try:
            value = int(self.cfg.chat_open_per_day or 1)
        except Exception:
            value = 1
        return max(value, 1)

    @property
    def chat_max_chars(self) -> int:
        """搭话字数上限。"""
        try:
            value = int(self.cfg.chat_open_max_chars or 40)
        except Exception:
            value = 40
        return max(value, 1)

    @staticmethod
    def select_windows(windows: list[ChatWindow], per_day: int, day: date) -> list[int]:
        """当天实际开口的窗口下标。

        窗口数多于每天条数时，按日期做一次随机挑选（同一天结果固定，重启后不变）；
        窗口数不足时按窗口数来（每个窗口各发一次）。

        Args:
            windows: 可用窗口列表。
            per_day: 每天最多主动开口几次。
            day: 用于随机挑选的日期。

        Returns:
            升序排列的窗口下标列表。
        """
        if not windows:
            return []
        count = max(int(per_day or 1), 1)
        if count >= len(windows):
            return list(range(len(windows)))
        rng = random.Random(int(day.strftime("%Y%m%d")))
        return sorted(rng.sample(range(len(windows)), count))

    @staticmethod
    def _clean_chat(text: str, limit: int) -> str:
        """清洗搭话内容：去掉引号与多余空白，并按上限截断。"""
        cleaned = " ".join(str(text or "").split()).strip("\"'“”‘’「」")
        if limit > 0 and len(cleaned) > limit:
            cleaned = cleaned[:limit].rstrip()
        return cleaned

    def chat_fallback_text(self) -> str:
        """内置兜底搭话文案（AI 不可用或返回为空时使用）。"""
        return self._clean_chat(random.choice(CHAT_FALLBACK_POOL), self.chat_max_chars)

    async def build_chat_text(self) -> str:
        """生成一句主动搭话的内容。

        人设取自 AstrBot 当前的人格，日程取当日生活日程；提示词里已写明语气约束
        （轻松、不冒犯、不打探隐私、不用空招呼、留出不理会的余地）。

        Returns:
            搭话文本；AI 失败或返回为空时回退内置文案。
        """
        limit = self.chat_max_chars
        hint = str(self.cfg.chat_open_prompt or "").strip() or DEFAULT_CHAT_OPEN_PROMPT
        parts = [hint]
        try:
            persona = await self.ai.fetch_persona()
        except Exception as e:
            logger.debug(f"获取主动闲聊用的人设失败: {e}")
            persona = {}
        if persona.get("prompt"):
            parts.append(f"# 人设\n{persona['prompt']}")

        life_context = ""
        if self._life_context_provider is not None:
            try:
                life_context = str(await self._life_context_provider() or "")
            except Exception as e:
                logger.debug(f"获取主动闲聊用的日程上下文失败: {e}")
        if life_context:
            parts.append(
                f"# 你此刻的生活状态（可以自然提一句，不必照抄）\n{life_context}"
            )
        parts.append(
            f"# 输出要求\n只输出要说的话，不要引号、不要解释、不要换行分段，不超过 {limit} 字。"
        )

        try:
            text = await self.ai.chat(
                system_prompt="\n\n".join(parts),
                prompt=None,
                provider_id=str(self.cfg.llm_chat_open_provider_id or ""),
                feature="主动闲聊",
            )
        except Exception as e:
            logger.warning(f"AI 生成主动闲聊失败，改用内置文案: {e}")
            return self.chat_fallback_text()

        cleaned = self._clean_chat(text, limit)
        return cleaned or self.chat_fallback_text()

    def chat_slot_key(self, day: date | None = None) -> str:
        """当天闲聊去重用的记录标识，形如 ``chat:<日期>``。"""
        target = day or self._now().date()
        return f"{CHAT_KEY}:{target.isoformat()}"

    async def send_chat_open(
        self,
        *,
        targets: list[str] | None = None,
        force: bool = False,
        record: bool = True,
        check_optin: bool = True,
        limit: int | None = None,
    ) -> GreetResult:
        """发一条主动闲聊（一次只发给一个人）。

        约束：同一个人每天最多收到一条；全局每天最多 ``limit``（缺省为
        ``chat_open_per_day``）条。主动闲聊是私聊内容且收件人已同意接收，
        因此**不走草稿确认**。

        Args:
            targets: 候选收件人；缺省用配置里的对象。
            force: 为 True 时忽略每天上限与当日去重（管理员手动测试用）。
            record: 是否写入当日记录。
            check_optin: 为 False 时跳过偏好检查。
            limit: 覆盖每天上限；留空用配置值。

        Returns:
            GreetResult 汇总（``sent`` 为 1 表示已发给某个人）。
        """
        day = self._now().date()
        slot_key = self.chat_slot_key(day)
        quota = self.chat_per_day if limit is None else max(int(limit), 0)
        result = GreetResult(slot=slot_key, record=record)

        watch = [
            str(item).strip()
            for item in (targets if targets is not None else self.targets)
            if str(item).strip()
        ]
        if not watch:
            result.errors.append(
                "没有可发送对象：可在面板里填写对象，或让用户用 /私聊开 接受"
            )
            return result

        sent_today = self.sent_today(slot_key)
        if not force and quota and sent_today >= quota:
            result.errors.append(
                f"今天已经主动开口 {sent_today} 次，达到每天上限 {quota}"
            )
            return result

        if force:
            candidates = list(watch)
        else:
            candidates = [qq for qq in watch if not self._already_sent(slot_key, qq)]
        if not candidates:
            result.skipped = len(watch)
            result.errors.append("今天这些对象都已经收到过闲聊，跳过")
            return result

        random.shuffle(candidates)
        result.text = await self.build_chat_text()
        # 最多尝试 3 个人：地址不对或发送失败时换下一个候选，避免一次失败就整天不发
        for attempt, qq in enumerate(candidates):
            if attempt >= 3:
                break
            await self._deliver(
                result,
                [qq],
                slot_key,
                force=force,
                record=record,
                feature=CHAT_KEY,
                check_optin=check_optin,
            )
            if result.sent:
                break
        return result

    def _allowed(self, qq: str, feature: str) -> bool:
        """按用户偏好判断是否允许给这个人发这个功能的主动消息。

        Args:
            qq: 目标 QQ 号。
            feature: 功能标识（morning / night / holiday / chat）。

        Returns:
            允许时返回 True；没有配置检查器、或检查异常时按「不允许」处理，
            避免在拿不准的情况下打扰用户。
        """
        if self._optin is None:
            return True
        try:
            return bool(self._optin(qq, feature))
        except Exception as e:
            logger.warning(f"检查 {qq} 的主动消息偏好失败，按不允许处理: {e}")
            return False

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    async def send(
        self,
        slot_key: str,
        *,
        targets: list[str] | None = None,
        force: bool = False,
        record: bool = True,
        feature: str = "",
        check_optin: bool = True,
    ) -> GreetResult:
        """生成并发送一次问候。

        Args:
            slot_key: 时段标识（morning / night）。
            targets: 覆盖本次目标；缺省用配置里的 greet_users。
            force: 为 True 时忽略今日去重（用于手动测试）。
            record: 是否写入「今日已问候」记录。手动测试应传 False，
                否则会把当天的自动问候名额用掉，定时任务到点会直接跳过。
            feature: 用于用户偏好检查的功能标识；留空表示不检查偏好。
            check_optin: 为 False 时跳过偏好检查（管理员手动指定对象时使用）。

        Returns:
            GreetResult 汇总。

        Raises:
            RuntimeError: 时段未定义或内容生成失败时抛出。
        """
        slot = self.slot_of(slot_key)
        if slot is None:
            raise RuntimeError(f"未知的问候时段: {slot_key}")

        result = GreetResult(slot=slot.key, record=record)
        watch = targets if targets is not None else self.targets
        if not watch:
            result.errors.append("未配置 greet_users，不知道要问候谁")
            return result

        result.text = await self.build_text(slot)
        await self._deliver(
            result,
            watch,
            slot.key,
            force=force,
            record=record,
            feature=feature or slot.key,
            check_optin=check_optin,
        )
        return result

    async def build_holiday_preview(
        self,
        value_date: date | datetime | str | None = None,
        *,
        force: bool = False,
    ) -> tuple[str, str] | None:
        """生成节日祝福内容但不发送（草稿模式使用）。

        Args:
            value_date: 目标日期；缺省用今天。
            force: 为 True 时忽略「今天不是节日」（手动测试用）。

        Returns:
            (记录标识, 祝福文本)；当天不是内置节日且未 force 时返回 None。

        Raises:
            RuntimeError: 内容生成失败（文案池为空且 AI 不可用）时抛出。
        """
        day = as_date(value_date)
        festival = festival_of(day)
        if not festival and not force:
            logger.info(f"[greet] {day.isoformat()} 不是内置传统节日，跳过节日祝福")
            return None
        if not festival:
            # 手动测试落在非节日：用下一个节日的名字占位，避免提示词里没有节日名
            upcoming = next_festival(day)
            festival = upcoming[0] if upcoming else "节日"
        text = await self.build_holiday_text(festival)
        return f"{HOLIDAY_KEY}:{day.isoformat()}", text

    async def send_holiday(
        self,
        *,
        value_date: date | datetime | str | None = None,
        targets: list[str] | None = None,
        force: bool = False,
        record: bool = True,
        check_optin: bool = True,
    ) -> GreetResult:
        """发送一次传统节日祝福。

        Args:
            value_date: 目标日期；缺省用今天（按系统时区）。
            targets: 覆盖本次目标；缺省用配置里的 greet_users。
            force: 为 True 时忽略「今天不是节日」与当日去重（手动测试用）。
            record: 是否写入当日记录。
            check_optin: 为 False 时跳过偏好检查（管理员手动指定对象时使用）。

        Returns:
            GreetResult 汇总；当天不是节日且未 force 时，结果里只有一条说明。

        Raises:
            RuntimeError: 内容生成失败（文案池为空且 AI 不可用）时抛出。
        """
        day = as_date(value_date)
        result = GreetResult(slot=f"{HOLIDAY_KEY}:{day.isoformat()}", record=record)

        preview = await self.build_holiday_preview(value_date, force=force)
        if preview is None:
            result.errors.append("今天不是内置的传统节日，未发送节日祝福")
            return result
        slot_key, text = preview

        watch = targets if targets is not None else self.targets
        if not watch:
            result.errors.append("未配置 greet_users，不知道要问候谁")
            return result

        result.text = text
        await self._deliver(
            result,
            watch,
            slot_key,
            force=force,
            record=record,
            feature=HOLIDAY_KEY,
            check_optin=check_optin,
        )
        return result

    async def send_text(
        self,
        text: str,
        targets: list[str] | None = None,
        *,
        slot_key: str = "custom",
        force: bool = True,
        record: bool = True,
        feature: str = "",
    ) -> GreetResult:
        """发送一段已经定好的文本（草稿确认放行时使用）。

        Args:
            text: 要发送的内容。
            targets: 目标 QQ 号；缺省用配置里的 greet_users。
            slot_key: 记录用的标识。
            force: 为 True 时忽略当日去重。
            record: 是否写入「今日已问候」记录。
            feature: 用于用户偏好检查的功能标识；留空表示不检查偏好。

        Returns:
            GreetResult 汇总。
        """
        result = GreetResult(slot=slot_key, text=text, record=record)
        watch = targets if targets is not None else self.targets
        if not watch:
            result.errors.append("未配置 greet_users，不知道要问候谁")
            return result

        await self._deliver(
            result, watch, slot_key, force=force, record=record, feature=feature
        )
        return result

    async def _deliver(
        self,
        result: GreetResult,
        watch: list[str],
        slot_key: str,
        *,
        force: bool,
        record: bool = True,
        feature: str = "",
        check_optin: bool = True,
    ) -> None:
        """逐个私聊发送并记录去重状态。

        Args:
            result: 本次汇总。
            watch: 收件人列表。
            slot_key: 去重用的时段标识。
            force: 为 True 时忽略当日去重。
            record: 是否写入当日记录。
            feature: 用于偏好检查的功能标识；留空表示不检查。
            check_optin: 为 False 时跳过偏好检查（管理员手动指定对象时使用）。
        """
        platform_id = str(self._platform_id_provider() or "").strip()
        if not platform_id and self._umo_resolver is None:
            result.errors.append(
                "没找到 aiocqhttp(OneBot) 平台实例，无法确定私聊地址，本次没有发送"
            )
            logger.error("[greet] 没找到平台实例，问候未发送")
            return

        for qq in watch:
            if check_optin and feature and not self._allowed(qq, feature):
                result.blocked += 1
                logger.info(f"[greet] {qq} 未接受主动消息（{feature}），本次跳过")
                continue
            if not force and self._already_sent(slot_key, qq):
                result.skipped += 1
                continue
            umo = self.umo_for(qq)
            result.targets_used[qq] = umo
            try:
                sent = bool(await self._dispatch(umo, result.text))
            except Exception as e:
                result.errors.append(f"{qq}: 发送异常 {e}")
                logger.error(f"[greet] 发送给 {qq} 失败（umo={umo}）: {e}")
                continue
            if sent:
                result.sent += 1
                if record:
                    self._mark_sent(slot_key, qq)
                logger.info(f"[greet] 已发送给 {qq}（umo={umo}）")
            else:
                result.errors.append(
                    f"{qq}: AstrBot 没找到匹配的会话（umo={umo}），消息没有发出"
                )
                logger.warning(f"[greet] 未发出：AstrBot 没有找到平台会话 umo={umo}")

        self.save()
        logger.info(f"[greet] {slot_key} 发送完成：{result.summary()}")

    async def _dispatch(self, umo: str, text: str) -> bool:
        """实际发送消息，返回「是否已交给平台」。

        返回 False 表示 AstrBot 没找到匹配的平台会话（消息根本没发出去），
        绝不能当成成功——否则会记进「今日已问候」而整天不再重试。
        """
        if self._sender is not None:
            return bool(await self._sender(umo, text))

        from astrbot.api.star import StarTools
        from astrbot.core.message.components import Plain
        from astrbot.core.message.message_event_result import MessageChain

        return bool(await StarTools.send_message(umo, MessageChain([Plain(text)])))
