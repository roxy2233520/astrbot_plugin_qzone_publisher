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
        sent: 成功发送人数（AstrBot 确认找到平台、消息已交给协议端）。
        skipped: 跳过人数（今天已发过 / 没配目标）。
        record: 是否写入「今日已问候」记录；手动测试时为 False，
            否则手动发一次会把当天的自动问候名额用掉。
        targets_used: 实际使用的发送地址（QQ -> UMO），用于排查发不出去的问题。
        errors: 出错信息。
    """

    slot: str
    text: str = ""
    sent: int = 0
    skipped: int = 0
    record: bool = True
    targets_used: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """生成可读汇总。"""
        parts = [f"成功 {self.sent} 人", f"跳过 {self.skipped} 人"]
        if not self.record:
            parts.append("手动发送（不占用今日自动问候名额）")
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
        umo_resolver: Callable[[str], str] | None = None,
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
            sender: 可选的异步发送函数 ``(umo, text) -> bool``，便于测试注入。
        """
        self.cfg = config
        self.ai = ai
        self._platform_id_provider = platform_id_provider
        self._life_context_provider = life_context_provider
        self._umo_resolver = umo_resolver
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
        record: bool = True,
    ) -> GreetResult:
        """生成并发送一次问候。

        Args:
            slot_key: 时段标识（morning / night）。
            targets: 覆盖本次目标；缺省用配置里的 greet_users。
            force: 为 True 时忽略今日去重（用于手动测试）。
            record: 是否写入「今日已问候」记录。手动测试应传 False，
                否则会把当天的自动问候名额用掉，定时任务到点会直接跳过。

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
        await self._deliver(result, watch, slot.key, force=force, record=record)
        return result

    async def send_text(
        self,
        text: str,
        targets: list[str] | None = None,
        *,
        slot_key: str = "custom",
        force: bool = True,
        record: bool = True,
    ) -> GreetResult:
        """发送一段已经定好的文本（草稿确认放行时使用）。

        Args:
            text: 要发送的内容。
            targets: 目标 QQ 号；缺省用配置里的 greet_users。
            slot_key: 记录用的标识。
            force: 为 True 时忽略当日去重。
            record: 是否写入「今日已问候」记录。

        Returns:
            GreetResult 汇总。
        """
        result = GreetResult(slot=slot_key, text=text, record=record)
        watch = targets if targets is not None else self.targets
        if not watch:
            result.errors.append("未配置 greet_users，不知道要问候谁")
            return result

        await self._deliver(result, watch, slot_key, force=force, record=record)
        return result

    async def _deliver(
        self,
        result: GreetResult,
        watch: list[str],
        slot_key: str,
        *,
        force: bool,
        record: bool = True,
    ) -> None:
        """逐个私聊发送并记录去重状态。"""
        platform_id = str(self._platform_id_provider() or "").strip()
        if not platform_id and self._umo_resolver is None:
            result.errors.append(
                "没找到 aiocqhttp(OneBot) 平台实例，无法确定私聊地址，本次没有发送"
            )
            logger.error("[greet] 没找到平台实例，问候未发送")
            return

        for qq in watch:
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
