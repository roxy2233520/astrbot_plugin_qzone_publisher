"""定时问候：按时间给指定用户发私聊问候（早安 / 晚安等）。

- **发给谁**：``greet_users`` 里的 QQ 号，逐个私聊发送；
- **什么时候发**：``greet_morning_cron`` / ``greet_night_cron`` 各自独立，留空即关闭该时段；
- **发什么**：文案池随机取，或用 AI 结合人设与当日生活日程生成（``greet_use_ai``）；
- **不重复**：同一天同一时段对同一个人只发一次（落盘 ``greet_state.json``），
  避免随机抖动或错过补偿导致重复问候；
- AI 调用只走 AstrBot 提供商（可用 ``llm_greet_provider_id`` 单独指定）。
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from astrbot.api import logger

from .config import PluginConfig

if TYPE_CHECKING:  # pragma: no cover - 仅用于类型标注
    from .llm import AIClient

DEFAULT_GREET_PROMPT = (
    "用你自己的说话风格，给一个你很在意的人写一句{slot}问候，"
    "一到两句话，自然、有温度，不要解释、不要加引号、不要提“问候”“说说”这类词。"
)


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
        sent: 成功发送人数。
        skipped: 跳过人数（今天已发过 / 没配目标）。
        errors: 出错信息。
    """

    slot: str
    text: str = ""
    sent: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """生成可读汇总。"""
        parts = [f"成功 {self.sent} 人", f"跳过 {self.skipped} 人"]
        text = "，".join(parts)
        if self.errors:
            text += "\n" + "\n".join(f"⚠️ {item}" for item in self.errors[:5])
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
        sender: Callable[[str, str], object] | None = None,
    ) -> None:
        """初始化服务。

        Args:
            config: 插件配置。
            ai: AI 客户端。
            platform_id_provider: 返回当前平台实例 id 的可调用对象，用于拼 UMO。
            life_context_provider: 可选的异步函数，返回当日日程文本。
            sender: 可选的异步发送函数 ``(umo, text) -> int``，便于测试注入。
        """
        self.cfg = config
        self.ai = ai
        self._platform_id_provider = platform_id_provider
        self._life_context_provider = life_context_provider
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
        """把 QQ 号拼成私聊 UMO。"""
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
        pool = [
            str(item).strip()
            for item in (getattr(self.cfg, slot.pool_key, None) or [])
            if str(item).strip()
        ]
        if not pool:
            raise RuntimeError(
                f"{slot.name}文案池为空，且未开启 AI 生成（greet_use_ai）"
            )
        return random.choice(pool)

    # ------------------------------------------------------------------
    # 发送
    # ------------------------------------------------------------------

    async def send(
        self,
        slot_key: str,
        *,
        targets: list[str] | None = None,
        force: bool = False,
    ) -> GreetResult:
        """生成并发送一次问候。

        Args:
            slot_key: 时段标识（morning / night）。
            targets: 覆盖本次目标；缺省用配置里的 greet_users。
            force: 为 True 时忽略今日去重（用于手动测试）。

        Returns:
            GreetResult 汇总。

        Raises:
            RuntimeError: 时段未定义或内容生成失败时抛出。
        """
        slot = self.slot_of(slot_key)
        if slot is None:
            raise RuntimeError(f"未知的问候时段: {slot_key}")

        result = GreetResult(slot=slot.key)
        watch = targets if targets is not None else self.targets
        if not watch:
            result.errors.append("未配置 greet_users，不知道要问候谁")
            return result

        result.text = await self.build_text(slot)
        await self._deliver(result, watch, slot.key, force=force)
        return result

    async def send_text(
        self,
        text: str,
        targets: list[str] | None = None,
        *,
        slot_key: str = "custom",
        force: bool = True,
    ) -> GreetResult:
        """发送一段已经定好的文本（草稿确认放行时使用）。

        Args:
            text: 要发送的内容。
            targets: 目标 QQ 号；缺省用配置里的 greet_users。
            slot_key: 记录用的标识。
            force: 为 True 时忽略当日去重。

        Returns:
            GreetResult 汇总。
        """
        result = GreetResult(slot=slot_key, text=text)
        watch = targets if targets is not None else self.targets
        if not watch:
            result.errors.append("未配置 greet_users，不知道要问候谁")
            return result

        await self._deliver(result, watch, slot_key, force=force)
        return result

    async def _deliver(
        self,
        result: GreetResult,
        watch: list[str],
        slot_key: str,
        *,
        force: bool,
    ) -> None:
        """逐个私聊发送并记录去重状态。"""
        for qq in watch:
            if not force and self._already_sent(slot_key, qq):
                result.skipped += 1
                continue
            umo = self.umo_for(qq)
            try:
                sent = await self._dispatch(umo, result.text)
            except Exception as e:
                result.errors.append(f"{qq}: {e}")
                continue
            if sent:
                result.sent += 1
                self._mark_sent(slot_key, qq)
            else:
                result.errors.append(f"{qq}: 发送未成功")

        self.save()
        logger.info(f"[greet] {slot_key} 发送完成：{result.summary()}")

    async def _dispatch(self, umo: str, text: str) -> int:
        """实际发送消息（默认走 AstrBot 的 StarTools.send_message）。"""
        if self._sender is not None:
            return int(await self._sender(umo, text))

        from astrbot.api.star import StarTools
        from astrbot.core.message.components import Plain
        from astrbot.core.message.message_event_result import MessageChain

        await StarTools.send_message(umo, MessageChain([Plain(text)]))
        return 1
