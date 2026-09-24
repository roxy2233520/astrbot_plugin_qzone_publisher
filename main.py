"""AstrBot QQ空间定时发布插件入口。

功能概览：
- 复用 OneBot 登录态自动获取 QQ空间 Cookie，无需手动抓包；
- 手动指令发布说说（支持附带图片）；
- 定时自动发布，内容来自文案池、文本文件或 AI 生成；
- 草稿确认模式：自动发布前先发给管理员/指定会话确认；
- 自动读好友说说（默认只读），可选点赞与 AI 评论；
- 一体化生活日程：自己生成或读取 life_scheduler 的数据，供写说说与提示词使用；
- 联网素材：接入 AstrBot 自带的「联网搜索」，把搜到的资料作为写说说的素材。
"""

from __future__ import annotations

import asyncio
import base64
import time
from datetime import date, datetime
from itertools import pairwise
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import Image, Plain
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.star.filter.command import GreedyStr

from .core.config import PluginConfig
from .core.content import ContentGenerator
from .core.draft import Draft, DraftBox
from .core.greet import CHAT_KEY, HOLIDAY_KEY, GreetingService, describe_windows
from .core.holidays import days_until, table_range_text
from .core.interact import InteractService
from .core.life import LifeManager, time_desc
from .core.llm import AIClient
from .core.qzone import QzoneAPI, QzoneParser, QzoneSession
from .core.render import ReceiptRenderer
from .core.scheduler import (
    CronTask,
    CronTaskGroup,
    _parse_field,
    describe_cron,
    describe_cron_windows,
    next_cron_moment,
    normalize_cron,
    split_times,
)
from .core.store import PublishRecord, PublishStore
from .core.ui import (
    DIVIDER,
    ICON_FAIL,
    ICON_INFO,
    ICON_OK,
    ICON_WARN,
    LIMIT_BLOCK_LINES,
    LIMIT_RECEIPT,
    Section,
    kv,
    pair,
    plain_receipt,
    quote_command,
)
from .core.user_prefs import FEATURE_LABELS, UserPrefStore
from .core.web import WebSearchBridge

_ON_FLAGS = {"on", "开", "开启", "true", "1", "yes"}
_OFF_FLAGS = {"off", "关", "关闭", "false", "0", "no", "none", "disable"}
_RENEW_FLAGS = {"renew", "regen", "重写", "重新生成", "重新生成日程"}
_NOW_FLAGS = {"now", "run", "立刻", "立即", "现在"}
_PUBLISH_TASK = "qzone_auto_publish"
_INTERACT_TASK = "qzone_interact"
_REPLY_TASK = "qzone_reply"
_GREET_MORNING_TASK = "qzone_greet_morning"
_GREET_NIGHT_TASK = "qzone_greet_night"
_HOLIDAY_TASK = "qzone_greet_holiday"
_CHAT_TASK = "qzone_chat_open"
# 主动闲聊允许「窗口已过一点点」的补偿触发（AstrBot 重启或卡顿后仍算在窗口内）
_CHAT_WINDOW_GRACE_SECONDS = 600


class QzonePublisherPlugin(Star):
    """QQ空间定时发布插件。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        """初始化插件组件。

        Args:
            context: AstrBot 插件上下文。
            config: 插件配置对象。
        """
        super().__init__(context)
        self.context = context
        self.cfg = PluginConfig(config, context)

        self.store = PublishStore(
            self.cfg.history_file, limit=int(self.cfg.history_limit or 200)
        )
        self.drafts = DraftBox(self.cfg.draft_file)
        self.session = QzoneSession(self.cfg, self._get_onebot_client)
        self.api = QzoneAPI(self.session, timeout=int(self.cfg.timeout or 15))
        self.ai = AIClient(self.cfg, context)
        self.life = LifeManager(self.cfg, context, self.ai)
        self.web = WebSearchBridge(self.cfg, context)
        self.content = ContentGenerator(
            self.cfg, self.ai, self.life, self.web, store=self.store
        )
        self.interact = InteractService(
            self.cfg,
            self.ai,
            self.api,
            self.drafts,
            reply_optin_checker=self._opted_in_allowed,
            opted_in_counter=self._opted_in_count,
        )
        self.render = ReceiptRenderer(self.cfg)
        # 最近一次与某个 QQ 的真实私聊会话地址（umo），问候优先用它，避免地址拼错
        self._private_umos: dict[str, str] = {}
        # 私聊用户偏好：谁接受机器人的主动消息、接受哪些功能
        self.prefs = UserPrefStore(self.cfg)
        self.greet = GreetingService(
            self.cfg,
            self.ai,
            self._platform_id,
            life_context_provider=self.life.prompt_context,
            umo_resolver=self._greet_umo,
            opted_in_checker=self._active_msg_allowed,
            client_provider=self._get_onebot_client,
            prefs_provider=self._greet_prefs_text,
            interaction_provider=self._greet_interaction_text,
            feeds_provider=self._greet_feeds,
        )

        self.publish_task = CronTaskGroup.from_config(
            self.cfg,
            name=_PUBLISH_TASK,
            job=self._auto_publish,
            times_key="publish_times",
            per_day_key="publish_per_day",
            cron_key="publish_cron",
            jitter_key="publish_jitter",
            enabled_key="auto_publish_enabled",
        )
        self.interact_task = CronTask.from_config(
            self.cfg,
            name=_INTERACT_TASK,
            job=self._auto_interact,
            cron_key="interact_cron",
            jitter_key="interact_jitter",
            enabled_key="interact_enabled",
        )
        # 回复自己的评论：独立任务 + 自己的时间窗口，默认每 5 分钟轮询一次
        self.reply_task = CronTask.from_config(
            self.cfg,
            name=_REPLY_TASK,
            job=self._auto_reply,
            cron_key="interact_reply_cron",
            jitter_key="interact_reply_jitter",
            enabled_key="interact_reply_enabled",
        )
        self.greet_morning_task = CronTask.from_config(
            self.cfg,
            name=_GREET_MORNING_TASK,
            job=self._greet_morning,
            cron_key="greet_morning_cron",
            jitter_key="greet_jitter",
            enabled_key="greet_enabled",
        )
        self.greet_night_task = CronTask.from_config(
            self.cfg,
            name=_GREET_NIGHT_TASK,
            job=self._greet_night,
            cron_key="greet_night_cron",
            jitter_key="greet_jitter",
            enabled_key="greet_enabled",
        )
        self.holiday_task = CronTask.from_config(
            self.cfg,
            name=_HOLIDAY_TASK,
            job=self._greet_holiday,
            cron_key="holiday_cron",
            jitter_key="holiday_jitter",
            enabled_key="holiday_enabled",
        )
        # 主动闲聊：每个时间窗口一个任务，窗口起点 + 窗口长度内的随机抖动
        # （与其它定时任务一样，构造时不启动，等 initialize() 里统一 start）
        self.chat_open_tasks: list[CronTask] = []
        self._client: Any = None
        self._draft_timer: asyncio.Task | None = None

    async def initialize(self) -> None:
        """插件加载时启动所有定时任务，并接上未处理完的草稿计时。"""
        self.publish_task.start()
        self.interact_task.start()
        self.reply_task.start()
        self.greet_morning_task.start()
        self.greet_night_task.start()
        self.holiday_task.start()
        self._rebuild_chat_tasks()
        await self._resume_pending_draft()

    async def _resume_pending_draft(self) -> None:
        """重启后处理遗留草稿：超过超时时间就立即放行，否则补上剩余计时。"""
        draft = self.drafts.pending
        if draft is not None and draft.kind == "reply":
            # 回复一律直接发出，旧版本遗留的回复草稿不再需要确认，直接丢弃
            self.drafts.pop()
            logger.info("已丢弃旧版本遗留的回复草稿（回复不再走草稿确认）")
            draft = self.drafts.pending
        if draft is None:
            return

        minutes = int(self.cfg.draft_timeout_minutes or 0)
        if minutes <= 0:
            return

        age_minutes = (time.time() - draft.created_time) / 60
        if age_minutes >= minutes:
            logger.info(f"发现已超时 {age_minutes:.0f} 分钟的草稿，立即放行")
            self.drafts.pop()
            try:
                message = await self._confirm_draft(draft)
            except Exception as e:
                self.drafts.put(draft)
                await self._notify(
                    plain_receipt(
                        "遗留草稿执行失败",
                        [kv("原因", e), kv("说明", "草稿已保留")],
                        icon=ICON_FAIL,
                    )
                )
                return
            await self._notify(
                plain_receipt(
                    "重启后处理了超时草稿", [kv("结果", message)], icon=ICON_WARN
                )
            )
            return

        remaining = max(int(minutes - age_minutes), 1)
        self._draft_timer = asyncio.create_task(
            self._draft_timeout_watch(draft, remaining)
        )
        logger.info(f"遗留草稿还剩约 {remaining} 分钟自动放行")

    async def terminate(self) -> None:
        """插件卸载时停止定时任务并释放连接。"""
        self._cancel_draft_timer()
        self.publish_task.stop()
        self.interact_task.stop()
        self.reply_task.stop()
        self.greet_morning_task.stop()
        self.greet_night_task.stop()
        self.holiday_task.stop()
        self._stop_chat_tasks()
        await self.api.close()

    # ------------------------------------------------------------------
    # 主动闲聊：任务重建与执行
    # ------------------------------------------------------------------

    def _stop_chat_tasks(self) -> None:
        """停止并清空主动闲聊任务。"""
        for task in self.chat_open_tasks:
            task.stop()
        self.chat_open_tasks = []

    def _rebuild_chat_tasks(self) -> list[str | None]:
        """按当前配置重建主动闲聊任务：每个时间窗口一个任务。

        窗口内随机取时刻的做法：任务固定在窗口起点触发，随机抖动上限设为窗口长度
        （AstrBot 的调度器会在 0~抖动秒之间随机延后），因此实际发送时刻落在窗口内且每天不固定。

        Returns:
            各任务生效的 Cron 表达式列表。
        """
        self._stop_chat_tasks()
        windows = self.greet.chat_windows
        if not windows or not bool(self.cfg.chat_open_enabled):
            return []
        for index, window in enumerate(windows):
            task = CronTask(
                name=f"{_CHAT_TASK}[{index + 1}]",
                timezone=self.cfg.timezone,
                job=self._make_chat_job(index),
                cron=window.start_cron,
                jitter=window.duration_seconds,
                enabled=bool(self.cfg.chat_open_enabled),
            )
            task.start()
            self.chat_open_tasks.append(task)
        return [task.cron for task in self.chat_open_tasks]

    def _make_chat_job(self, index: int):
        """为第 index 个窗口生成任务回调。"""

        async def job() -> None:
            await self._chat_open_tick(index)

        return job

    async def _chat_open_tick(self, index: int, now: datetime | None = None) -> None:
        """某个时间窗口到点：确认仍在窗口内，并且是当天随机选中的窗口。

        Args:
            index: 窗口下标（对应配置里窗口列表的顺序）。
            now: 注入的时刻，缺省取当前时间（自测用）。
        """
        windows = self.greet.chat_windows
        if index >= len(windows):
            logger.info(f"[chat] 窗口配置已变化，忽略第 {index + 1} 个窗口的触发")
            return
        window = windows[index]
        moment = now or datetime.now(self.cfg.timezone)
        if not window.contains(moment, grace_seconds=_CHAT_WINDOW_GRACE_SECONDS):
            logger.info(
                f"[chat] {window.text} 已错过（当前 {moment.strftime('%H:%M')}），本次跳过"
            )
            return
        chosen = self.greet.select_windows(
            windows, self.greet.chat_per_day, moment.date()
        )
        if index not in chosen:
            picked = "、".join(windows[item].text for item in chosen) or "无"
            logger.info(
                f"[chat] 今天随机选中的窗口是 {picked}，{window.text} 本次不发送"
            )
            return
        await self._run_chat_open(window)

    async def _run_chat_open(self, window) -> None:
        """在到点的窗口里发一条主动闲聊（一次只发一个人）。

        Args:
            window: 到点的窗口（仅用于日志与提示）。
        """
        slot_name = "主动闲聊"
        targets = self._feature_targets(CHAT_KEY)
        if not targets:
            logger.info(f"{slot_name}：没有可发送对象（{window.text}），跳过本次")
            if bool(self.cfg.notify_enabled) and bool(
                self.cfg.active_msg_require_optin
            ):
                await self._notify(
                    plain_receipt(
                        f"{slot_name}没有发送",
                        [
                            kv(
                                "原因",
                                self._no_consent_note(slot_name, CHAT_KEY),
                            )
                        ],
                        icon=ICON_WARN,
                    )
                )
            return

        # 主动闲聊是私聊内容、收件人已明确同意，所以不走草稿确认：
        # 多一次人工确认往往会错过「闲聊」的时机（与问候的 draft_for_greet 无关）。
        try:
            result = await self.greet.send_chat_open(targets=targets)
        except Exception as e:
            logger.error(f"{slot_name}发送失败: {e}")
            return

        if result.sent or result.errors:
            await self._report_greet(slot_name, result)

    # ------------------------------------------------------------------
    # 平台客户端
    # ------------------------------------------------------------------

    def _find_platform(self) -> Any | None:
        """找到 aiocqhttp(OneBot) 平台适配器实例。"""
        try:
            manager = self.context.platform_manager
            getter = getattr(manager, "get_insts", None)
            platforms = getter() if callable(getter) else manager.platform_insts
            for platform in platforms:
                meta = platform.meta()
                if getattr(meta, "name", "") == "aiocqhttp":
                    return platform
        except Exception as e:
            logger.warning(f"查找 OneBot 平台实例失败: {e}")
        return None

    def _get_onebot_client(self) -> Any | None:
        """获取 OneBot 客户端实例。

        Returns:
            客户端实例；未找到时返回 None。
        """
        if self._client is not None:
            return self._client

        platform = self._find_platform()
        bot = getattr(platform, "bot", None) if platform is not None else None
        if bot is not None:
            self._client = bot
        return self._client

    def _platform_id(self) -> str:
        """取平台实例 id，用于拼装 UMO。"""
        platform = self._find_platform()
        if platform is None:
            return ""
        try:
            return str(platform.meta().id or "")
        except Exception:
            return ""

    def _remember_client(self, event: AstrMessageEvent) -> None:
        """缓存 OneBot 客户端与当前会话标识，避免重复查找。

        会话标识会同步给联网搜索桥，用于读取按会话覆盖的 AstrBot 配置；
        私聊会话还会按 QQ 号记下来，问候优先用它当发送地址。
        """
        bot = getattr(event, "bot", None)
        if bot is not None:
            self._client = bot
        umo = str(getattr(event, "unified_msg_origin", "") or "")
        if not umo:
            return
        self.web.remember_umo(umo)

        parts = umo.split(":")
        if len(parts) == 3 and parts[1] == "FriendMessage" and parts[2].isdigit():
            self._private_umos[parts[2]] = umo

    def _greet_umo(self, qq: str) -> str:
        """问候用：返回最近一次与该 QQ 的真实私聊会话地址；没有则返回空串。"""
        return self._private_umos.get(str(qq).strip(), "")

    # ------------------------------------------------------------------
    # 发布
    # ------------------------------------------------------------------

    async def _publish(
        self,
        text: str,
        images: list[bytes] | None = None,
        *,
        source: str = "manual",
    ) -> PublishRecord:
        """发布说说并写入发布历史。

        Args:
            text: 说说正文。
            images: 图片二进制内容列表。
            source: 来源标识，用于历史记录。

        Returns:
            发布记录。

        Raises:
            RuntimeError: 发布失败时抛出。
        """
        uin = 0
        try:
            uin = await self.session.get_uin()
        except Exception as e:
            logger.debug(f"获取登录 QQ 号失败: {e}")

        record = PublishRecord(
            time=int(time.time()),
            text=text,
            uin=uin,
            source=source,
            images=len(images or []),
        )

        try:
            resp = await self.api.publish(text, images or [])
        except Exception as e:
            record.ok = False
            record.error = str(e)
            self.store.append(record)
            raise

        if not resp.ok:
            record.ok = False
            record.error = str(resp.message or resp.code)
            self.store.append(record)
            raise RuntimeError(record.error)

        record.tid = str(resp.data.get("tid") or "")
        record.time = int(resp.data.get("now") or record.time)
        self.store.append(record)
        logger.info(f"说说发布成功: tid={record.tid}")
        return record

    async def _auto_publish(self) -> None:
        """定时发布任务：生成内容 -> 直接发布或转草稿。"""
        try:
            text, source = await self.content.generate()
        except Exception as e:
            logger.error(f"自动发布内容生成失败: {e}")
            await self._notify(
                plain_receipt(
                    "定时发布失败",
                    [kv("原因", "内容生成异常"), kv("详情", e)],
                    icon=ICON_FAIL,
                )
            )
            return
        await self._dispatch_post(text, source=source, prefix="定时发布")

    async def _auto_interact(self) -> None:
        """定时互动任务：读 / 赞 / 评好友说说。

        回复自己说说下的评论是**独立任务**（``qzone_reply``，按
        ``interact_reply_cron`` 高频轮询），不再搭在这次巡检上。
        """
        lines: list[str] = []

        if self.interact.targets:
            result = await self.interact.run_once()
            lines.append(
                plain_receipt(
                    "说说互动完成", result.summary().splitlines(), icon=ICON_OK
                )
            )
        else:
            logger.info("未配置 interact_uins，跳过本轮好友说说巡检")

        if lines and bool(self.cfg.interact_notify):
            await self._notify(f"\n{DIVIDER}\n".join(lines))

        pending = self.drafts.pending
        if pending is not None and pending.kind == "comment":
            await self._send_draft(pending)
            await self._arm_draft_timer(pending)

    async def _auto_reply(self) -> None:
        """定时任务：按 ``interact_reply_cron`` 轮询自己说说下的新评论并回复。

        QQ空间没有评论推送通道（OneBot 只推送 QQ 消息事件），因此评论只能靠轮询发现，
        所以这个任务默认每 5 分钟跑一轮；每轮无论有没有新评论都会写一行日志。
        """
        if not bool(self.cfg.interact_reply_enabled):
            return
        try:
            result = await self.interact.run_replies_once()
        except Exception as e:
            logger.error(f"[reply] 本轮评论巡检异常: {e}")
            return
        if (result.replied or result.drafted) and bool(self.cfg.interact_notify):
            await self._notify(
                plain_receipt(
                    "评论回复完成", result.summary().splitlines(), icon=ICON_OK
                )
            )

    async def _greet_morning(self) -> None:
        """定时任务：群发早安问候。"""
        await self._run_greet("morning")

    async def _greet_night(self) -> None:
        """定时任务：群发晚安问候。"""
        await self._run_greet("night")

    async def _greet_holiday(self) -> None:
        """定时任务：节日当天群发节日祝福。"""
        await self._run_holiday()

    def _greet_prefs_text(self, qq: str) -> str:
        """逐人生成时给 AI 的「这个对象的偏好」说明（取不到返回空串）。"""
        user = self.prefs.get(qq)
        if user is None:
            return ""
        state = UserPrefStore.state_text(user)
        return f"主动消息设置：{state}｜各项开关：{UserPrefStore.features_text(user)}"

    def _greet_interaction_text(self, qq: str) -> str:
        """逐人生成时给 AI 的「与该对象的最近往来」说明（取不到返回空串）。

        只从插件已有的记录推断（今天的问候去重记录），不额外发请求。
        """
        labels = {
            "morning": "早安",
            "night": "晚安",
            "chat": "日常闲聊",
            "holiday": "节日祝福",
        }
        records = self.greet._sent.get(self.greet._today(), [])
        hits: list[str] = []
        for item in records:
            if not str(item).endswith(f":{qq}"):
                continue
            slot = str(item).split(":", 1)[0]
            label = labels.get(slot) or (
                "节日祝福" if slot.startswith("holiday") else slot
            )
            if label not in hits:
                hits.append(label)
        if not hits:
            return ""
        return f"今天已经给他发过：{'、'.join(hits)}"

    async def _greet_feeds(self, qq: str, count: int) -> list[str]:
        """读取该对象最近说说的正文，供逐人生成参考。

        只取正文（忽略图片），只用于当次生成、不落盘；对方设了权限或不是好友时
        接口会返回空或失败，此时返回空列表（问候照常发送）。

        Args:
            qq: 目标 QQ 号。
            count: 取最近几条。

        Returns:
            正文列表；读不到时为空列表。
        """
        try:
            resp = await self.api.get_feeds(qq, pos=0, num=max(int(count or 1), 1))
        except Exception as e:
            logger.debug(f"读取 {qq} 的最近说说失败，本次不参考: {e}")
            return []
        if not resp.ok:
            logger.debug(
                f"读取 {qq} 的最近说说失败（{resp.message or resp.code}），本次不参考"
            )
            return []
        posts = QzoneParser.parse_feeds(resp.data)
        return [post.text for post in posts if post.text]

    def _active_msg_allowed(self, qq: str, feature: str) -> bool:
        """判断该 QQ 是否接受这个功能的主动消息。

        Args:
            qq: 目标 QQ 号。
            feature: 功能标识（morning / night / holiday）。

        Returns:
            允许时返回 True；``active_msg_require_optin`` 关闭时一律允许。
        """
        if not bool(self.cfg.active_msg_require_optin):
            return True
        return self.prefs.allowed(qq, feature)

    def _opted_in_allowed(self, qq: str) -> bool:
        """判断该 QQ 是否「接受过主动消息」（并集口径里的一半）。

        与特权名单取并集的那一半：名单里的人由 ``InteractService.allowed_uin``
        直接放行，不走这里。全局 ``active_msg_require_optin`` 关闭时，
        同意机制本身不生效（没人被问过），此时一律允许。

        Args:
            qq: 目标 QQ 号。

        Returns:
            允许互动时返回 True。
        """
        if not bool(self.cfg.active_msg_require_optin):
            return True
        return self.prefs.allowed(qq, "reply")

    def _opted_in_count(self) -> int:
        """已接受主动消息的人数（用于状态里的「∪ 已同意 M 人」）。"""
        return int(self.prefs.stats().get("accepted", 0))

    def _feature_targets(self, feature: str) -> list[str]:
        """取某个主动消息功能的收件人（早安 / 晚安 / 节日祝福共用一份口径）。

        ``active_msg_require_optin`` 开启时：收件人 = 已接受、且没有单独关掉该功能的用户
        （不再读 ``greet_users``）；关闭时：退回按 ``greet_users`` 发送，不检查偏好。

        Args:
            feature: 功能标识（morning / night / holiday）。

        Returns:
            收件人 QQ 列表；没有可发送对象时返回空列表。
        """
        if not bool(self.cfg.active_msg_require_optin):
            return list(self.greet.targets)
        return self.prefs.allowed_users(feature)

    def _no_consent_note(self, slot_name: str, feature: str) -> str:
        """开关为开但无人同意时的提示文案。

        Args:
            slot_name: 展示名（早安 / 晚安 / 节日祝福）。
            feature: 功能标识。

        Returns:
            可直接展示与通知的提示文本。
        """
        label = FEATURE_LABELS.get(feature, slot_name)
        return f"目前没有已同意接收{label}的用户，可在私聊里回复 /私聊开 接受"

    async def _draft_greet(
        self,
        *,
        slot_key: str,
        slot_name: str,
        feature: str,
        text: str,
        targets: list[str],
    ) -> None:
        """把问候或节日祝福转成待确认草稿。

        Args:
            slot_key: 记录标识。
            slot_name: 展示名（早安 / 晚安 / 节日祝福）。
            feature: 用于用户偏好检查的功能标识。
            text: 已生成好的内容。
            targets: 收件人列表（已按口径算好）。
        """
        if not targets:
            logger.info(f"{slot_name}：没有可发送对象")
            if bool(self.cfg.notify_enabled) and bool(
                self.cfg.active_msg_require_optin
            ):
                await self._notify(
                    plain_receipt(
                        f"{slot_name}没有发送",
                        [
                            kv(
                                "原因",
                                self._no_consent_note(slot_name, feature),
                            )
                        ],
                        icon=ICON_WARN,
                    )
                )
            return

        draft = self.drafts.put(
            Draft(
                kind="greet",
                text=text,
                source=f"greet:{slot_key}",
                targets=list(targets),
                note=(
                    "下面是预览；确认后会按每个人分别生成内容再发出"
                    f"（共 {len(targets)} 人）"
                ),
            )
        )
        await self._send_draft(draft)
        await self._arm_draft_timer(draft)

    async def _report_greet(self, slot_name: str, result) -> None:
        """问候类任务的统一通知。"""
        if not bool(self.cfg.notify_enabled):
            return
        if result.sent == 0 and result.errors:
            # 别再用「已发送」这种说法掩盖失败：一条都没发出去时明确报警
            await self._notify(
                plain_receipt(
                    f"{slot_name}没有发出去",
                    result.summary().splitlines(),
                    icon=ICON_WARN,
                )
                + self._usage_note()
            )
            return
        await self._notify(
            plain_receipt(
                f"{slot_name}已发送",
                [*result.summary().splitlines(), kv("内容", result.text)],
                icon=ICON_OK,
            )
            + self._usage_note()
        )

    def greet_holiday_status(self, upcoming: tuple[str, date, int] | None) -> str:
        """拼节日祝福的状态说明。

        Args:
            upcoming: ``days_until()`` 的结果（节日名 / 日期 / 相隔天数）。

        Returns:
            形如「下一个节日 中秋（2026-09-25，还有 2 天）｜今日已发 0 人」的文本。
        """
        today = datetime.now(self.cfg.timezone).date()
        parts: list[str] = []
        if upcoming is None:
            parts.append(f"节日表只覆盖 {table_range_text()}，需要更新插件")
        else:
            name, day, left = upcoming
            when = "就是今天" if left == 0 else f"还有 {left} 天"
            parts.append(f"下一个节日 {name}（{day.isoformat()}，{when}）")
        parts.append(
            f"今日已发 {self.greet.sent_today(f'holiday:{today.isoformat()}')} 人"
        )
        return "｜".join(parts)

    def next_reply_run_text(self, now: datetime | None = None) -> str:
        """下一轮评论巡检的时刻（人话）。

        Args:
            now: 注入的当前时刻，缺省取当前时间。

        Returns:
            形如 ``20:00``；跨到第二天时形如 ``次日 12:00``；无法计算时给出说明。
        """
        moment = now or datetime.now(self.cfg.timezone)
        # 用 Cron 计算基础触发时刻（不含随机抖动），这样显示的是整点/半点，
        # 例如「下一轮 20:00」；算不出来时才退回调度器的排期时间。
        stamp = next_cron_moment(str(self.reply_task.cron or ""), moment)
        if stamp is None:
            stamp = self.reply_task.next_run_datetime
        if stamp is None:
            return "未排期"
        text = stamp.strftime("%H:%M")
        if stamp.date() != moment.date():
            return f"次日 {text}"
        return text

    def interact_reply_interval_text(self, now: datetime | None = None) -> str:
        """评论巡检间隔的人话说明（含时段、下一轮与最坏延迟）。

        Args:
            now: 注入的当前时刻，缺省取当前时间。

        Returns:
            形如「每 30 分钟一轮｜时段：每天 12:00-14:00、20:00-23:00｜下一轮 20:00
            ｜最坏延迟：时段内 30 分钟 + 抖动 120 秒，跨时段则等到下一个时段」。
        """
        cron = str(self.reply_task.cron or "").strip()
        if not cron:
            return "未设置（不会自动巡检）"

        every_minutes = 0
        minutes = _parse_field(cron.split()[0]) if len(cron.split()) == 5 else None
        if minutes and len(minutes) > 1:
            ordered = sorted(minutes)
            step = ordered[1] - ordered[0]
            if step > 0 and all(
                later - earlier == step for earlier, later in pairwise(ordered)
            ):
                every_minutes = step

        jitter = max(int(self.reply_task.jitter or 0), 0)
        window = describe_cron_windows(cron)
        if every_minutes <= 0:
            return (
                f"按 {cron}｜下一轮 {self.next_reply_run_text(now)}"
                f"｜最坏延迟取决于巡检间隔 + 抖动 {jitter} 秒"
            )
        if every_minutes >= 24 * 60:
            return (
                f"每天一次（{cron}）｜下一轮 {self.next_reply_run_text(now)}"
                f"｜最坏延迟：约 24 小时 + 抖动 {jitter} 秒"
            )

        parts = [f"每 {every_minutes} 分钟一轮"]
        if window:
            parts.append(f"时段：{window}")
        else:
            parts.append(f"按 {cron}")
        parts.append(f"下一轮 {self.next_reply_run_text(now)}")
        parts.append(
            f"最坏延迟：时段内 {every_minutes} 分钟 + 抖动 {jitter} 秒，"
            "跨时段则等到下一个时段"
        )
        return "｜".join(parts)

    def next_chat_window_text(self) -> str:
        """下一个主动闲聊窗口的时刻文本。

        Returns:
            形如 ``09-24 12:07``（窗口起点 + 随机抖动后的实际调度时刻）；
            没有已排期的窗口时给出原因说明。
        """
        stamps = [
            item
            for item in (task.next_run_datetime for task in self.chat_open_tasks)
            if item is not None
        ]
        if stamps:
            return min(stamps).strftime("%m-%d %H:%M")
        if not self.greet.chat_windows:
            return "未配置时间窗口"
        if not bool(self.cfg.chat_open_enabled):
            return "已关闭"
        return "未调度"

    def chat_open_status(self) -> str:
        """主动闲聊的状态说明：开关 / 窗口 / 每天上限 / 今日已发 / 下一个窗口。"""
        windows = self.greet.chat_windows
        today = datetime.now(self.cfg.timezone).date()
        sent = self.greet.sent_today(self.greet.chat_slot_key(today))
        return (
            f"{'开启' if bool(self.cfg.chat_open_enabled) else '关闭'}"
            f"｜窗口 {describe_windows(windows)}"
            f"｜每天最多 {self.greet.chat_per_day} 条（每次只发 1 人，同一人每天最多 1 条）"
            f"｜今日已发 {sent} 人"
            f"｜下一个窗口 {self.next_chat_window_text()}"
        )

    def _guidance_text(self) -> str:
        """首次私聊引导的文案（不超过 6 行）。"""
        morning = describe_cron(self.cfg.greet_morning_cron)
        night = describe_cron(self.cfg.greet_night_cron)
        holiday = describe_cron(self.cfg.holiday_cron)
        chat_windows = describe_windows(self.greet.chat_windows)
        section = Section(icon=ICON_INFO, label="主动消息说明")
        section.add(
            kv(
                "可能的时间段",
                f"早安 {morning}、晚安 {night}、节日祝福 {holiday}、"
                f"日常闲聊 {chat_windows}",
            ),
            kv("实际时间", "会随机延后；闲聊在窗口内随机，不会固定在同一秒"),
            kv(
                "可能打扰到的功能",
                "早安 / 晚安问候、传统节日祝福、日常闲聊"
                "（白天与晚上可能收到一两句招呼），以及评论回复"
                "（评论回复会在说说评论区提醒被回复的人，不是私聊）",
            ),
            kv(
                "如何设置",
                f"{quote_command('私聊开')}接收，{quote_command('私聊关')}不接收",
            ),
            kv(
                "说明",
                "不回应视为不接受，不会收到任何主动消息；"
                f"随时可用{quote_command('私聊开')}改回来",
            ),
        )
        return section.text()

    @staticmethod
    def _looks_like_command(text: str, wake_prefixes: list[str]) -> bool:
        """判断这条私聊消息是不是指令，避免引导与指令互相打扰。

        Args:
            text: 已去掉首尾空白的消息正文。
            wake_prefixes: AstrBot 配置里的唤醒前缀列表。

        Returns:
            看起来像指令时返回 True。
        """
        if not text:
            return True
        if text.startswith("/"):
            return True
        for prefix in wake_prefixes:
            if prefix and text.startswith(prefix):
                return True
        # 唤醒前缀会被 AstrBot 剥掉，所以还要识别指令本身的名字
        if text.startswith("空间"):
            return True
        return text.split(maxsplit=1)[0].lower() in {"space", "qz"}

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_message(self, event: AstrMessageEvent):
        """私聊首次引导：说明可能收到的主动消息并引导设置偏好。

        只在私聊、且该用户还没被问过时发送一次；指令、唤醒开头与群聊都会跳过。
        """
        if not bool(self.cfg.active_msg_require_optin):
            return

        # 过滤器已限定私聊，这里再挡一次：直接调用本方法（例如测试）时也不越界
        private_checker = getattr(event, "is_private_chat", None)
        if callable(private_checker):
            try:
                if not private_checker():
                    return
            except Exception:
                return

        text = str(getattr(event, "message_str", "") or "").strip()
        try:
            wake_prefixes = [
                str(item)
                for item in (self.context.get_config().get("wake_prefix") or [])
            ]
        except Exception:
            wake_prefixes = []
        if self._looks_like_command(text, wake_prefixes):
            return

        qq = str(event.get_sender_id() or "").strip()
        if not qq.isdigit():
            return
        if not self.prefs.needs_guidance(qq):
            return

        self.prefs.mark_seen(qq)
        self.prefs.mark_asked(qq)
        logger.info(f"已向 {qq} 发出首次主动消息引导")
        yield event.plain_result(self._guidance_text())

    async def _run_greet(self, slot_key: str) -> None:
        """执行一次问候：草稿确认开启时先转草稿，否则直接发送。

        收件人口径与节日祝福一致：同意机制开启时只发给已接受且没关掉该项的用户。

        Args:
            slot_key: 时段标识（morning / night）。
        """
        slot = self.greet.slot_of(slot_key)
        slot_name = slot.name if slot else slot_key
        targets = self._feature_targets(slot_key)

        if not targets:
            logger.info(f"{slot_name}：没有可发送对象，跳过本次问候")
            if bool(self.cfg.notify_enabled) and bool(
                self.cfg.active_msg_require_optin
            ):
                await self._notify(
                    plain_receipt(
                        f"{slot_name}没有发送",
                        [
                            kv(
                                "原因",
                                self._no_consent_note(slot_name, slot_key),
                            )
                        ],
                        icon=ICON_WARN,
                    )
                )
            return

        if bool(self.cfg.draft_for_greet):
            try:
                text = await self.greet.build_text(slot)
            except Exception as e:
                logger.error(f"问候内容生成失败: {e}")
                await self._notify(
                    plain_receipt(
                        f"{slot_name}失败",
                        [kv("原因", "内容生成异常"), kv("详情", e)],
                        icon=ICON_FAIL,
                    )
                )
                return
            await self._draft_greet(
                slot_key=slot_key,
                slot_name=slot_name,
                feature=slot_key,
                text=text,
                targets=targets,
            )
            return

        try:
            result = await self.greet.send(slot_key, targets=targets, feature=slot_key)
        except Exception as e:
            logger.error(f"问候发送失败: {e}")
            return

        await self._report_greet(slot_name, result)

    async def _run_holiday(self) -> None:
        """执行一次节日祝福：收件人是已同意接收节日祝福的用户，当天不是节日就不发送。"""
        slot_name = "节日祝福"
        targets = self._feature_targets(HOLIDAY_KEY)

        if not targets:
            logger.info("没有已同意接收节日祝福的用户，跳过本次节日祝福")
            if bool(self.cfg.notify_enabled) and bool(
                self.cfg.active_msg_require_optin
            ):
                await self._notify(
                    plain_receipt(
                        f"{slot_name}没有发送",
                        [
                            kv(
                                "原因",
                                self._no_consent_note(slot_name, HOLIDAY_KEY),
                            )
                        ],
                        icon=ICON_WARN,
                    )
                )
            return

        if bool(self.cfg.draft_for_greet):
            try:
                preview = await self.greet.build_holiday_preview()
            except Exception as e:
                logger.error(f"节日祝福内容生成失败: {e}")
                await self._notify(
                    plain_receipt(
                        f"{slot_name}失败",
                        [kv("原因", "内容生成异常"), kv("详情", e)],
                        icon=ICON_FAIL,
                    )
                )
                return
            if preview is None:
                return
            slot_key, text = preview
            await self._draft_greet(
                slot_key=slot_key,
                slot_name=slot_name,
                feature=HOLIDAY_KEY,
                text=text,
                targets=targets,
            )
            return

        try:
            result = await self.greet.send_holiday(targets=targets)
        except Exception as e:
            logger.error(f"节日祝福发送失败: {e}")
            return

        await self._report_greet(slot_name, result)

    async def _dispatch_post(self, text: str, *, source: str, prefix: str) -> None:
        """统一的自动发布出口：草稿模式先转人工确认。

        Args:
            text: 待发布正文。
            source: 内容来源标识。
            prefix: 通知文案前缀。
        """
        if bool(self.cfg.draft_enabled):
            draft = self.drafts.put(Draft(kind="post", text=text, source=source))
            sent = await self._send_draft(draft)
            await self._arm_draft_timer(draft)
            if sent == 0:
                logger.warning("草稿模式已开启，但没有可用的通知会话，说说未发布")
            else:
                logger.info(f"已生成说说草稿并发出确认请求（{sent} 个会话）")
            return

        try:
            record = await self._publish(text, source=source)
        except Exception as e:
            await self._notify(
                plain_receipt("发布失败", [kv("原因", e)], icon=ICON_FAIL)
                + self._usage_note()
            )
            return
        await self._notify(
            self._format_record(record, prefix=f"{prefix}成功")
            + self.content.warning_note()
            + self._usage_note()
        )

    async def _confirm_draft(self, draft: Draft) -> str:
        """执行草稿：说说走发布接口，评论与回复走评论接口，问候走私聊。

        Args:
            draft: 待执行的草稿。

        Returns:
            给管理员看的结果文本。

        Raises:
            RuntimeError: 执行失败时抛出。
        """
        if draft.kind == "comment":
            if not draft.target_tid:
                raise RuntimeError("草稿缺少目标说说 ID，无法评论")
            resp = await self.api.comment(
                draft.target_uin, draft.target_tid, draft.text
            )
            if not resp.ok:
                raise RuntimeError(str(resp.message or resp.code))
            return plain_receipt(
                "评论已发布",
                [kv("草稿", draft.title()), kv("内容", draft.text)],
                icon=ICON_OK,
            )

        if draft.kind == "greet":
            # 确认后逐人重新生成（预览只是给人看的一份），因此每个人收到的话不一样
            source = str(draft.source or "")
            slot_key = source.split(":", 1)[1] if ":" in source else ""
            targets = draft.targets or None
            if slot_key in ("morning", "night"):
                result = await self.greet.send(
                    slot_key, targets=targets, force=True, feature=slot_key
                )
            elif slot_key.startswith("holiday"):
                result = await self.greet.send_holiday(targets=targets, force=True)
            else:
                # 旧版本遗留的问候草稿：没有时段信息，只能按原文发出
                result = await self.greet.send_text(draft.text, targets)
            if result.sent == 0:
                raise RuntimeError(f"问候没有发出去：{result.summary()}")
            lines = list(result.summary().splitlines())
            if self.greet.last_texts:
                lines.append(
                    kv(
                        "已生成",
                        f"{len(self.greet.last_texts)} 人的不同内容（每人单独生成）",
                    )
                )
            else:
                lines.append(kv("内容", result.text))
            return plain_receipt("问候已发送", lines, icon=ICON_OK)

        record = await self._publish(draft.text, source=draft.source or "draft")
        return self._format_record(record, prefix="草稿已发布")

    @staticmethod
    def _format_record(record: PublishRecord, prefix: str = "发布成功") -> str:
        """把发布记录格式化为统一的区块回执。"""
        failed = "失败" in prefix
        lines: list[str] = []
        if record.tid:
            lines.append(kv("tid", record.tid))
            if record.uin:
                lines.append(
                    kv(
                        "链接",
                        f"https://user.qzone.qq.com/{record.uin}/mood/{record.tid}",
                    )
                )
        if prefix and prefix not in ("发布成功", "发布失败"):
            lines.append(kv("方式", prefix))
        if record.images:
            lines.append(kv("图片", f"{record.images} 张"))
        if record.text:
            lines.append(kv("内容", record.text))
        if record.error:
            lines.append(kv("原因", record.error))
        if failed and not lines:
            lines.append(kv("状态", "没有可显示的发布信息"))
        label = "发布失败" if failed else "发布成功"
        icon = ICON_FAIL if failed else ICON_OK
        return plain_receipt(label, lines, icon=icon)

    @staticmethod
    def _format_time(timestamp: int) -> str:
        """时间戳转可读时间。"""
        try:
            return datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "-"

    async def _extract_images(self, event: AstrMessageEvent) -> list[bytes]:
        """提取消息中附带的图片。

        Args:
            event: 消息事件。

        Returns:
            图片二进制内容列表，最多 max_images 张。
        """
        limit = max(int(self.cfg.max_images or 9), 1)
        images: list[bytes] = []

        for component in getattr(event.message_obj, "message", None) or []:
            if not isinstance(component, Image):
                continue
            try:
                encoded = await component.convert_to_base64()
                images.append(base64.b64decode(encoded))
            except Exception as e:
                logger.warning(f"读取消息图片失败，已跳过: {e}")
                continue
            if len(images) >= limit:
                break

        return images

    # ------------------------------------------------------------------
    # 通知与草稿投递
    # ------------------------------------------------------------------

    def _admin_qqs(self) -> tuple[list[str], str]:
        """解析管理员 QQ 号列表。

        优先用插件配置里的 ``admin_uins``；留空时回退到 AstrBot 配置里的
        ``admins_id``（也就是决定指令权限的那份名单）。

        Returns:
            二元组 (QQ 号列表, 来源说明)。
        """
        own = [
            str(item).strip()
            for item in (self.cfg.admin_uins or [])
            if str(item).strip()
        ]
        if own:
            return own, "插件配置 admin_uins"

        try:
            admins = self.context.get_config().get("admins_id", []) or []
        except Exception as e:
            logger.warning(f"读取 AstrBot 的 admins_id 失败: {e}")
            return [], "读取失败"

        qqs = [str(item).strip() for item in admins if str(item).strip().isdigit()]
        return qqs, "AstrBot 配置 admins_id"

    def _admin_umos(self) -> list[str]:
        """拼出管理员私聊 UMO（草稿确认、通知用）。"""
        if not bool(self.cfg.draft_admin):
            return []
        platform_id = self._platform_id()
        if not platform_id:
            return []
        qqs, _ = self._admin_qqs()
        return [f"{platform_id}:FriendMessage:{qq}" for qq in qqs]

    @staticmethod
    def _build_chain(message: str, image: str | None = None) -> MessageChain:
        """构造消息链：文本 +（可选）回执图。

        Args:
            message: 文本内容。
            image: 图片本地路径或 URL；为空则只发文本。

        Returns:
            可直接发送的消息链。图片构造失败时自动只发文本。
        """
        components: list[Any] = [Plain(message)]
        if image:
            try:
                if image.startswith(("http://", "https://")):
                    components.append(Image.fromURL(image))
                else:
                    components.append(Image.fromFileSystem(image))
            except Exception as e:
                logger.warning(f"回执图构造失败，改为只发文本: {e}")
        return MessageChain(components)

    async def _send_to(
        self, umos: list[str], message: str, *, image: str | None = None
    ) -> int:
        """向指定会话列表发送同一条消息。

        Args:
            umos: 目标会话列表。
            message: 消息内容。
            image: 可选的回执图（本地路径或 URL）。

        Returns:
            成功发送的会话数。
        """
        sent = 0
        for target in umos:
            try:
                await StarTools.send_message(target, self._build_chain(message, image))
                sent += 1
            except Exception as e:
                logger.warning(f"消息发送到 {target} 失败: {e}")
        return sent

    async def _notify(
        self,
        message: str,
        extra_umos: list[str] | None = None,
        *,
        render: bool = True,
        markup: str = "",
    ) -> int:
        """向配置的会话发送通知。

        Args:
            message: 通知内容（纯文本版，能直接在 QQ 里显示）。
            extra_umos: 额外接收者。
            render: 是否尝试把通知渲染成回执图。默认尝试；
                渲染器自身会在「未开启渲染」或「文本过短」时直接跳过，
                渲染失败也会自动降级为纯文本。
            markup: 回执图使用的 Markdown 版内容；留空时退回 ``message``。

        Returns:
            成功发送的会话数。
        """
        umos: list[str] = []
        if bool(self.cfg.notify_enabled):
            umo = str(self.cfg.notify_umo or "").strip()
            if umo:
                umos.append(umo)
        for item in extra_umos or []:
            if item and item not in umos:
                umos.append(item)

        image = None
        if render:
            # 图片路径用 Markdown 版：标签成为标题、键值名称为真加粗
            image = await self.render.render(str(markup or message))
        return await self._send_to(umos, message, image=image)

    async def _send_draft(self, draft: Draft) -> int:
        """把草稿发给管理员私聊与 draft_umo 指定会话。

        草稿只发给显式配置的确认对象，不会顺带发到 notify_umo，
        免得确认请求出现在群里。

        Args:
            draft: 待确认草稿。

        Returns:
            成功发送的会话数。
        """
        umos = self._admin_umos()
        draft_umo = str(self.cfg.draft_umo or "").strip()
        if draft_umo and draft_umo not in umos:
            umos.append(draft_umo)

        text, markup = draft.describe_pair()
        note_text, note_markup = self._usage_note_pair()
        text += note_text
        markup += note_markup
        image = await self.render.render(markup)
        return await self._send_to(umos, text, image=image)

    # ------------------------------------------------------------------
    # Token 用量提示
    # ------------------------------------------------------------------

    def _usage_note_pair(self) -> tuple[str, str]:
        """用量提示的 (纯文本版, Markdown 版)。"""
        item = self.ai.last_call
        if not item:
            return "", ""
        total = item.get("total", 0)
        detail = (
            f"输入 {item.get('prompt', 0)} + 输出 {item.get('completion', 0)}，"
            f"功能：{item.get('feature', '未知')}，估算值"
        )
        section = Section(icon=ICON_INFO, label="用量估算")
        section.add(f"{kv('本次生成约用', f'{total} tokens')}")
        section.add(f"{kv('构成', detail)}")
        text, markup = pair([section], max_lines=LIMIT_RECEIPT)
        return f"\n\n{text}", f"\n\n{markup}"

    def _usage_note(self) -> str:
        """附在草稿/通知后面的 token 用量提示（估算，纯文本版）。"""
        return self._usage_note_pair()[0]

    # ------------------------------------------------------------------
    # 草稿超时自动放行
    # ------------------------------------------------------------------

    def _cancel_draft_timer(self) -> None:
        """取消当前的草稿超时计时。"""
        task = getattr(self, "_draft_timer", None)
        if task is not None and not task.done():
            task.cancel()
        self._draft_timer = None

    async def _arm_draft_timer(self, draft: Draft) -> None:
        """按配置给草稿安排超时自动放行。

        Args:
            draft: 刚生成的草稿。
        """
        self._cancel_draft_timer()
        minutes = int(self.cfg.draft_timeout_minutes or 0)
        if minutes <= 0:
            return
        self._draft_timer = asyncio.create_task(
            self._draft_timeout_watch(draft, minutes)
        )
        logger.info(f"草稿将在 {minutes} 分钟无人处理后自动放行")

    async def _draft_timeout_watch(self, draft: Draft, minutes: int) -> None:
        """等待超时后，若草稿仍未处理则自动执行。"""
        try:
            await asyncio.sleep(minutes * 60)
        except asyncio.CancelledError:
            return

        current = self.drafts.pending
        if current is None or current.created_time != draft.created_time:
            return  # 已被人工处理，或已经换成新草稿

        logger.info(f"草稿超过 {minutes} 分钟未处理，自动放行")
        self.drafts.pop()
        try:
            message = await self._confirm_draft(draft)
        except Exception as e:
            self.drafts.put(draft)
            await self._notify(
                plain_receipt(
                    "超时草稿执行失败",
                    [kv("原因", e), kv("说明", "草稿已保留")],
                    icon=ICON_FAIL,
                )
            )
            return
        await self._notify(
            plain_receipt(
                "草稿超时已自动执行",
                [kv("等待", f"{minutes} 分钟无人处理"), kv("结果", message)],
                icon=ICON_WARN,
            )
        )

    # ------------------------------------------------------------------
    # system prompt 注入
    # ------------------------------------------------------------------

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        """把当日生活状态注入 system prompt（默认关闭）。"""
        if not bool(self.cfg.life_inject_enabled):
            return
        try:
            text = await self.life.injection_text()
        except Exception as e:
            logger.warning(f"注入生活状态失败: {e}")
            return
        if text:
            req.system_prompt += text

    # ------------------------------------------------------------------
    # 指令：发布
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间发布", alias={"space post", "qz post"})
    async def cmd_publish(self, event: AstrMessageEvent, text: GreedyStr):
        """立即发布一条说说，可附带图片"""
        self._remember_client(event)
        self.content.remember_umo(event.unified_msg_origin)
        self.ai.remember_umo(event.unified_msg_origin)

        content = str(text).strip()
        images = await self._extract_images(event)

        if not content and not images:
            yield event.plain_result(
                plain_receipt(
                    "参数无效",
                    [
                        kv("用法", f"{quote_command('空间发布 说说内容')}立即发布"),
                        kv("说明", "可以同时附带图片，最多 9 张"),
                    ],
                    icon=ICON_WARN,
                )
            )
            return

        yield event.plain_result(
            plain_receipt(
                "正在发布",
                [kv("目标", "QQ空间，请稍候")],
                icon=ICON_INFO,
            )
        )
        try:
            record = await self._publish(content, images, source="manual")
        except Exception as e:
            yield event.plain_result(
                plain_receipt("发布失败", [kv("原因", e)], icon=ICON_FAIL)
            )
            return

        yield event.plain_result(self._format_record(record))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间自动发", alias={"space auto", "qz auto", "空间生成"})
    async def cmd_auto_publish(self, event: AstrMessageEvent):
        """立刻用 AI 生成并发布一条说说（与定时发布同一条链路）"""
        self._remember_client(event)
        self.content.remember_umo(event.unified_msg_origin)
        self.ai.remember_umo(event.unified_msg_origin)

        yield event.plain_result(
            plain_receipt(
                "正在生成",
                [kv("方式", "AI 按人设生成，请稍候")],
                icon=ICON_INFO,
            )
        )
        try:
            text, source = await self.content.generate()
        except Exception as e:
            yield event.plain_result(
                plain_receipt("生成失败", [kv("原因", e)], icon=ICON_FAIL)
            )
            return

        # 与定时发布保持一致：开了草稿确认就先转草稿，不直接发出去
        if bool(self.cfg.draft_enabled):
            draft = self.drafts.put(Draft(kind="post", text=text, source=source))
            sent = await self._send_draft(draft)
            await self._arm_draft_timer(draft)
            note = f"（草稿已发给 {sent} 个会话）" if sent else "（没有可用的通知会话）"
            yield event.plain_result(
                f"已生成说说草稿{note}{self._usage_note()}\n\n{draft.describe()}"
            )
            return

        try:
            record = await self._publish(text, source=source)
        except Exception as e:
            await self._notify(
                plain_receipt("发布失败", [kv("原因", e)], icon=ICON_FAIL)
                + self._usage_note()
            )
            yield event.plain_result(
                plain_receipt("发布失败", [kv("原因", e)], icon=ICON_FAIL)
                + self._usage_note()
            )
            return

        receipt = (
            self._format_record(record, prefix="手动自动发成功")
            + self.content.warning_note()
            + self._usage_note()
        )
        await self._notify(receipt)
        yield event.plain_result(receipt)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间状态", alias={"space status", "qz status", "空间登录"})
    async def cmd_status(self, event: AstrMessageEvent):
        """查看登录态、AI 接入、日程与各定时任务状态（仅管理员）"""
        self._remember_client(event)

        # 区块 1：登录与接入
        login_lines: list[str] = []
        login_icon = ICON_OK
        try:
            nickname = await self.session.get_nickname()
            uin = await self.session.get_uin()
            login_lines.append(
                kv(
                    "登录态",
                    f"正常（{nickname} / {uin}，Cookie 来源："
                    f"{self.session.source or '未知'}）",
                )
            )
        except Exception as e:
            login_icon = ICON_WARN
            login_lines.append(kv("登录态", f"异常（{e}）"))

        admin_qqs, admin_source = self._admin_qqs()
        login_lines.append(
            kv(
                "管理员",
                f"{'、'.join(admin_qqs) if admin_qqs else '未识别到'}"
                f"（来源：{admin_source}）",
            )
        )
        if not admin_qqs:
            login_lines.append(
                kv(
                    "提示",
                    "没有管理员名单，草稿确认发不出去；可在插件配置 admin_uins 填写，"
                    f"或用 {quote_command('空间管理员 add <QQ号>')}",
                )
            )
        overrides = self.ai.overrides_text()
        login_lines.append(
            kv(
                "AI 接入",
                f"{self.ai.describe()}"
                + (f"｜单独指定：{overrides}" if overrides else ""),
            )
        )

        cached = self.life.cached(datetime.now().date())
        if cached and cached.status == "ok":
            life_text = f"已就绪｜{cached.to_line()[:60]}"
        else:
            life_text = "今日尚未生成"
        login_lines.append(
            kv(
                "生活日程",
                f"{life_text}｜注入提示词："
                f"{'开' if self.cfg.life_inject_enabled else '关'}",
            )
        )

        # 区块 2：生成与回执
        gen_lines = [
            kv("联网素材", self.web.status_text()),
            kv("回执图渲染", self.render.status_text()),
        ]
        basis = self.content.last_generation
        if basis:
            gen_lines.append(
                kv(
                    "上次生成依据",
                    f"人设={basis.get('persona') or '未取到'}"
                    f"｜日程={'已引用' if basis.get('life') else '未引用'}"
                    f"｜联网素材={'有' if basis.get('web') else '无'}"
                    f"｜聊天记录={'有' if basis.get('chat') else '无'}"
                    f"｜角度={basis.get('angle') or '未启用'}"
                    f"｜时段={basis.get('slot') or '未知'}",
                )
            )
            if basis.get("repeat_checked"):
                gen_lines.append(
                    kv(
                        "避免重复",
                        f"参考最近 {basis.get('recent') or 0} 条"
                        f"｜与最近内容相似度 {basis.get('repeat') or 0}%"
                        f"（阈值 {int(self.cfg.publish_repeat_threshold or 0)}%）"
                        + ("｜已自动重写一次" if basis.get("repeat_rewritten") else ""),
                    )
                )
            # 提醒合并成一行：区块行数有限，避免把「用量」等信息挤出区块
            warnings = [str(item) for item in (basis.get("warnings") or [])]
            if warnings:
                gen_lines.append(kv("提醒", "；".join(warnings[:3])))
        usage_first_line = self.ai.usage.format_summary(1).splitlines()[0]
        gen_lines.append(kv("Token 用量（估算）", usage_first_line))
        if self.ai.last_call:
            gen_lines.append(kv("最近一次", self.ai.last_call_text()))

        # 区块 3：定时任务
        task_lines = [
            kv(
                "定时发布",
                f"{'开启' if self.publish_task.running else '关闭'}"
                f"（{self.publish_task.describe()}，"
                f"抖动 {self.publish_task.jitter} 秒）",
            ),
            kv("内容来源", self.cfg.content_source),
            kv("下次执行", self.publish_task.next_run_time),
        ]
        if self.publish_task.error:
            task_lines.append(kv("提醒", self.publish_task.error))
        task_lines.append(
            kv(
                "说说互动",
                f"{self.interact.mode_text()}"
                f"（{self.interact_task.cron or '未设置'}，"
                f"关注 {len(self.interact.targets)} 个 QQ）",
            )
        )
        task_lines.append(kv("下次巡检", self.interact_task.next_run_time))
        if not self.interact.targets:
            task_lines.append(kv("提醒", "还没配置 interact_uins，巡检不会做任何事"))
        task_lines.append(
            kv(
                "评论回复",
                f"{self.interact.reply_mode_text()}"
                f"｜巡检 {self.reply_task.cron or '未设置'}"
                f"｜窗口 {self.interact.reply_days} 天",
            )
        )
        task_lines.append(kv("回复对象", self.interact.reply_scope_text()))
        if self.interact.pending_attempts:
            task_lines.append(kv("未确认回复", self.interact.attempts_text()))

        # 区块 4：草稿确认
        draft_lines = [
            kv(
                "草稿确认",
                f"{'开启' if self.cfg.draft_enabled else '关闭'}"
                f"｜评论也确认：{'开' if self.cfg.draft_for_comment else '关'}",
            )
        ]
        pending = self.drafts.pending
        if pending is not None:
            draft_lines.append(
                kv(
                    "待确认",
                    f"{pending.title()}（{self._format_time(pending.created_time)}）",
                )
            )

        # 区块 5：定时问候与节日祝福
        greet_lines = [
            kv(
                "定时问候",
                f"{'开启' if bool(self.cfg.greet_enabled) else '关闭'}"
                f"｜收件人 "
                f"{'已同意的用户' if bool(self.cfg.active_msg_require_optin) else 'greet_users'}"
                f"｜内容 {'AI 生成' if bool(self.cfg.greet_use_ai) else '文案池'}",
            )
        ]
        morning = self.greet.slot_of("morning")
        night = self.greet.slot_of("night")
        for slot, task in (
            (morning, self.greet_morning_task),
            (night, self.greet_night_task),
        ):
            if slot is None:
                continue
            slot_targets = self._feature_targets(slot.key)
            greet_lines.append(
                kv(
                    f"{slot.name}问候",
                    f"{task.cron or '未设置'}（下次 {task.next_run_time}）"
                    f"｜今日已发 {self.greet.sent_today(slot.key)} 人"
                    f"｜本次将发给 {len(slot_targets)} 人（已同意）",
                )
            )
            if bool(self.cfg.greet_enabled) and not slot_targets:
                greet_lines.append(
                    kv("提醒", self._no_consent_note(slot.name, slot.key))
                )
        sample_targets = self._feature_targets("morning") or self.greet.targets
        if sample_targets:
            greet_lines.append(kv("发送地址", self.greet.umo_for(sample_targets[0])))

        # 区块 6：节日祝福与主动闲聊
        upcoming = days_until(datetime.now(self.cfg.timezone).date())
        holiday_targets = self._feature_targets(HOLIDAY_KEY)
        holiday_lines = [
            kv(
                "节日祝福",
                f"{'开启' if bool(self.cfg.holiday_enabled) else '关闭'}"
                f"｜{self.greet_holiday_status(upcoming)}"
                f"｜本次将发给 {len(holiday_targets)} 人（已同意）",
            )
        ]
        if bool(self.cfg.holiday_enabled) and not holiday_targets:
            holiday_lines.append(
                kv("提醒", self._no_consent_note("节日祝福", HOLIDAY_KEY))
            )
        holiday_lines.append(kv("主动闲聊", self.chat_open_status()))

        # 区块 5：主动消息与最近发布
        stats = self.prefs.stats()
        tail_lines = [
            kv(
                "主动消息同意",
                f"已接受 {stats['accepted']} 人"
                f"｜已拒绝 {stats['declined']} 人"
                f"｜未回答 {stats['unanswered']} 人"
                f"（需要同意："
                f"{'开' if bool(self.cfg.active_msg_require_optin) else '关'}）",
            )
        ]
        last = self.store.last_success()
        if last:
            tail_lines.append(
                kv("上次发布", f"{self._format_time(last.time)}（tid {last.tid}）")
            )
        else:
            tail_lines.append(kv("上次发布", "暂无记录"))

        sections = [
            Section(icon=login_icon, label="登录与接入", lines=login_lines),
            Section(icon=ICON_INFO, label="生成与回执", lines=gen_lines),
            Section(icon=ICON_INFO, label="定时任务", lines=task_lines),
            Section(icon=ICON_INFO, label="草稿确认", lines=draft_lines),
            Section(icon=ICON_INFO, label="定时问候", lines=greet_lines),
            Section(icon=ICON_INFO, label="节日祝福与闲聊", lines=holiday_lines),
            Section(icon=ICON_INFO, label="主动消息与最近发布", lines=tail_lines),
        ]
        # 状态是多区块输出：区块之间用分隔线，每个区块不超过 8 行
        text, _markup = pair(sections, divider=True, block_limit=LIMIT_BLOCK_LINES)
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间重登", alias={"space relogin", "qz relogin"})
    async def cmd_relogin(self, event: AstrMessageEvent):
        """强制重新获取 QQ空间登录态"""
        self._remember_client(event)
        try:
            ctx = await self.session.refresh()
        except Exception as e:
            yield event.plain_result(
                plain_receipt("重登失败", [kv("原因", e)], icon=ICON_FAIL)
            )
            return
        yield event.plain_result(
            plain_receipt(
                "重登成功",
                [
                    kv("uin", ctx.uin),
                    kv("Cookie 来源", self.session.source),
                ],
                icon=ICON_OK,
            )
        )

    # ------------------------------------------------------------------
    # 指令：定时与开关
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间定时", alias={"space cron", "qz cron"})
    async def cmd_schedule(self, event: AstrMessageEvent, spec: GreedyStr = ""):
        """查看或设置自动发布时间：支持多个时间点、HH:MM、5 段 Cron 与 off"""
        self._remember_client(event)
        text = str(spec).strip()

        if not text:
            yield event.plain_result(self._schedule_text())
            return

        if text.lower() in _OFF_FLAGS:
            self.cfg.set("publish_times", [])
            self.cfg.set("publish_per_day", 0)
            self.cfg.set("publish_cron", "")
            self.publish_task.reconfigure(times=[], per_day=0, cron="")
            yield event.plain_result(
                plain_receipt(
                    "发布时间已清空",
                    [kv("状态", "定时自动发布已关闭")],
                    icon=ICON_INFO,
                )
            )
            return

        # 可选前缀「每天 N」：只改每天发布条数
        per_day: int | None = None
        head, _, rest = text.partition(" ")
        if head in {"每天", "每天发", "daily"} and rest.strip():
            number, _, tail = rest.strip().partition(" ")
            if number.isdigit():
                per_day = min(max(int(number), 0), 10)
                text = tail.strip()
        if not text:
            yield event.plain_result(
                plain_receipt(
                    "设置失败", [kv("原因", "没有可用的时间点")], icon=ICON_FAIL
                )
            )
            return

        valid: list[str] = []
        invalid: list[str] = []
        # 单个 5 段 Cron 里带空格，不能按空格拆开，先整体识别
        fields = text.split()
        candidates = (
            [text]
            if len(fields) == 5 and not any(sep in text for sep in (",", "，", "、"))
            else split_times(text)
        )
        for item in candidates:
            try:
                normalize_cron(item)
            except ValueError:
                invalid.append(item)
                continue
            valid.append(item)

        if not valid:
            yield event.plain_result(
                plain_receipt(
                    "设置失败",
                    [
                        kv("原因", f"这些时间点无法识别（{'、'.join(invalid)}）"),
                        kv("用法", "请使用 HH:MM 或 5 段 Cron"),
                    ],
                    icon=ICON_FAIL,
                )
            )
            return

        # 只给了一个 5 段 Cron 时，按兼容项 publish_cron 处理
        single_cron = len(valid) == 1 and len(valid[0].split()) == 5
        days = per_day if per_day is not None else (1 if single_cron else len(valid))
        times = [] if single_cron else valid

        self.cfg.set("publish_times", times)
        self.cfg.set("publish_cron", normalize_cron(valid[0]) or "")
        self.cfg.set("publish_per_day", days)
        if not bool(self.cfg.auto_publish_enabled):
            self.cfg.set("auto_publish_enabled", True)

        self.publish_task.reconfigure(
            times=times,
            per_day=days,
            cron=normalize_cron(valid[0]) or "",
            enabled=True,
        )

        section = Section(icon=ICON_OK, label="发布时间已设置")
        section.add(kv("发布计划", self.publish_task.describe()))
        if self.publish_task.incomplete:
            # 条数多于时间点：配置不完整，明确提示而不是少发几条
            section.add(
                kv("状态", "配置不完整，当前不会自动发布"),
                kv("详情", self.publish_task.error),
                kv(
                    "用法",
                    "补齐时间点，或用"
                    f"{quote_command('空间定时 每天 2 08:30,12:30')}把条数调小",
                ),
            )
        else:
            section.add(kv("下次执行", self.publish_task.next_run_time))
        if invalid:
            section.add(kv("已忽略", "、".join(invalid)))
        yield event.plain_result(section.text())

    def _schedule_text(self) -> str:
        """自动发布时间的展示文本。"""
        section = Section(icon=ICON_INFO, label="发布时间")
        section.add(
            kv(
                "自动发布",
                f"{'开启' if self.publish_task.running else '关闭'}"
                f"｜{self.publish_task.describe()}",
            ),
            kv("抖动", f"{self.publish_task.jitter} 秒"),
            kv("下次执行", self.publish_task.next_run_time),
        )
        if self.publish_task.error:
            section.add(kv("提醒", self.publish_task.error))
        section.add(
            kv(
                "用法",
                f"{quote_command('空间定时 08:30,12:30,21:00')}多个时间点｜"
                f"{quote_command('空间定时 每天 2 08:30,12:30')}只发前 2 个",
            ),
            kv(
                "用法",
                f"{quote_command('空间定时 30 8 * * *')}单个 Cron｜"
                f"{quote_command('空间定时 off')}关闭",
            ),
        )
        return section.text()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间开关", alias={"space toggle", "qz toggle"})
    async def cmd_toggle(self, event: AstrMessageEvent, state: GreedyStr = ""):
        """开关定时自动发布"""
        self._remember_client(event)
        flag = str(state).strip().lower()

        if not flag:
            yield event.plain_result(self._schedule_text())
            return

        if flag in _ON_FLAGS:
            self.cfg.set("auto_publish_enabled", True)
            crons = self.publish_task.reconfigure(enabled=True)
            if not crons:
                yield event.plain_result(
                    plain_receipt(
                        "定时发布已开启",
                        [
                            kv("提醒", "没有可用的发布时间点"),
                            kv("用法", f"用{quote_command('空间定时 08:30')}设置"),
                        ],
                        icon=ICON_WARN,
                    )
                )
                return
            yield event.plain_result(
                plain_receipt(
                    "定时发布已开启",
                    [
                        kv("发布计划", self.publish_task.describe()),
                        kv("下次执行", self.publish_task.next_run_time),
                    ],
                    icon=ICON_OK,
                )
            )
            return

        if flag in _OFF_FLAGS:
            self.cfg.set("auto_publish_enabled", False)
            self.publish_task.reconfigure(enabled=False)
            yield event.plain_result(plain_receipt("定时发布已关闭", icon=ICON_WARN))
            return

        yield event.plain_result(
            plain_receipt(
                "参数无效",
                [
                    kv(
                        "用法",
                        f"{quote_command('空间开关 on')}或"
                        f"{quote_command('空间开关 off')}",
                    )
                ],
                icon=ICON_WARN,
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间互动", alias={"space interact", "qz interact"})
    async def cmd_interact(self, event: AstrMessageEvent, state: GreedyStr = ""):
        """查看或开关「定时读说说」任务（点赞/评论在插件配置里单独开）"""
        self._remember_client(event)
        flag = str(state).strip().lower()

        if not flag:
            section = Section(icon=ICON_INFO, label="说说互动")
            section.add(
                kv("当前状态", self.interact.mode_text()),
                kv("巡检时间", self.interact_task.cron or "未设置"),
                kv("下次巡检", self.interact_task.next_run_time),
                kv(
                    "关注对象",
                    "、".join(self.interact.targets) or "（未配置 interact_uins）",
                ),
                kv(
                    "用法",
                    f"{quote_command('空间互动 on')}或"
                    f"{quote_command('空间互动 off')}；立即跑一轮用"
                    f"{quote_command('空间读说说')}",
                ),
            )
            yield event.plain_result(section.text())
            return

        if flag in _ON_FLAGS:
            self.cfg.set("interact_enabled", True)
            cron = self.interact_task.reconfigure(enabled=True)
            if cron is None:
                yield event.plain_result(
                    plain_receipt(
                        "说说互动已开启",
                        [
                            kv("提醒", "没有可用的时间配置"),
                            kv("用法", "请先在面板里设置巡检时间"),
                        ],
                        icon=ICON_WARN,
                    )
                )
                return
            yield event.plain_result(
                plain_receipt(
                    "说说互动已开启",
                    [
                        kv("巡检时间", cron),
                        kv("下次巡检", self.interact_task.next_run_time),
                    ],
                    icon=ICON_OK,
                )
            )
            return

        if flag in _OFF_FLAGS:
            self.cfg.set("interact_enabled", False)
            self.interact_task.reconfigure(enabled=False)
            yield event.plain_result(plain_receipt("说说互动已关闭", icon=ICON_WARN))
            return

        yield event.plain_result(
            plain_receipt(
                "参数无效",
                [
                    kv(
                        "用法",
                        f"{quote_command('空间互动 on')}或"
                        f"{quote_command('空间互动 off')}",
                    )
                ],
                icon=ICON_WARN,
            )
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间回复", alias={"space reply", "qz reply"})
    async def cmd_reply(self, event: AstrMessageEvent, action: GreedyStr = ""):
        """查看、开关或立即执行「回复自己说说下的评论」"""
        self._remember_client(event)
        flag = str(action).strip().lower()

        if not flag:
            section = Section(icon=ICON_INFO, label="回复评论")
            section.add(
                kv("开关", "开启" if bool(self.cfg.interact_reply_enabled) else "关闭"),
                kv("巡检间隔", self.interact_reply_interval_text()),
                kv(
                    "时间窗口",
                    f"{self.interact.reply_days} 天内自己发的说说"
                    "（旧说说下的新评论同样会被发现）",
                ),
                kv(
                    "每轮上限",
                    f"{self.interact.reply_limit} 条"
                    "；同一条说说每轮最多回 1 条，多余的在下一轮继续",
                ),
                kv("今日已回", f"{self.interact.replied_today} 条"),
                kv("回复对象", self.interact.reply_scope_text()),
                kv("未确认", self.interact.attempts_text()),
                kv("下次巡检", self.reply_task.next_run_time),
                kv(
                    "说明",
                    "QQ空间没有评论推送，评论只能靠定时轮询发现",
                ),
                kv(
                    "用法",
                    f"{quote_command('空间回复 on')}或"
                    f"{quote_command('空间回复 off')}开关；立即跑一轮用"
                    f"{quote_command('空间回复 now')}",
                ),
            )
            yield event.plain_result(section.text())
            return

        if flag in _ON_FLAGS:
            self.cfg.set("interact_reply_enabled", True)
            cron = self.reply_task.reconfigure(enabled=True)
            yield event.plain_result(
                plain_receipt(
                    "回复评论已开启",
                    [
                        kv("巡检间隔", self.interact_reply_interval_text()),
                        kv("下次巡检", self.reply_task.next_run_time),
                        kv(
                            "说明",
                            "QQ空间没有评论推送，评论只能靠定时轮询发现"
                            + ("" if cron else "；当前时间配置不可用，请检查巡检间隔"),
                        ),
                    ],
                    icon=ICON_OK,
                )
            )
            return

        if flag in _OFF_FLAGS:
            self.cfg.set("interact_reply_enabled", False)
            self.reply_task.reconfigure(enabled=False)
            yield event.plain_result(
                plain_receipt(
                    "回复评论已关闭",
                    [kv("说明", "评论巡检任务已停止，不再回复新评论")],
                    icon=ICON_WARN,
                )
            )
            return

        if flag in _NOW_FLAGS:
            yield event.plain_result(
                plain_receipt(
                    "正在巡检",
                    [kv("范围", "自己说说下的评论，请稍候")],
                    icon=ICON_INFO,
                )
            )
            result = await self.interact.run_replies_once(force=True)
            yield event.plain_result(
                plain_receipt(
                    "评论回复完成",
                    result.summary().splitlines(),
                    icon=ICON_INFO,
                )
            )
            return

        yield event.plain_result(
            plain_receipt(
                "参数无效",
                [
                    kv(
                        "用法",
                        f"{quote_command('空间回复 on')}、"
                        f"{quote_command('空间回复 off')}或"
                        f"{quote_command('空间回复 now')}",
                    )
                ],
                icon=ICON_WARN,
            )
        )

    # ------------------------------------------------------------------
    # 指令：联网搜索（接入 AstrBot 自带能力）/ 日程 / 读说说
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间搜索", alias={"space search", "qz search"})
    async def cmd_search(self, event: AstrMessageEvent, query: GreedyStr = ""):
        """用 AstrBot 自带的联网搜索测一条；不带参数只看状态"""
        self._remember_client(event)
        text = str(query).strip()

        if not text:
            _, reason = self.web.readiness()
            yield event.plain_result(
                plain_receipt(
                    "联网素材",
                    [
                        kv("开关", "开" if self.cfg.web_search_enabled else "关"),
                        kv("AstrBot 联网搜索", reason),
                        kv(
                            "用法",
                            f"{quote_command('空间搜索 关键词')}"
                            "跑一条搜索，用来确认接入是否正常（不会发说说）",
                        ),
                    ],
                    icon=ICON_INFO,
                )
            )
            return

        yield event.plain_result(
            plain_receipt("正在联网搜索", [kv("关键词", text)], icon=ICON_INFO)
        )
        outcome = await self.web.search(
            text,
            count=int(self.cfg.web_search_count or 5),
            umo=event.unified_msg_origin,
        )
        if not outcome:
            yield event.plain_result(
                plain_receipt(
                    "搜索失败",
                    [
                        kv("原因", outcome.error),
                        kv(
                            "提示",
                            "服务商与密钥都在 AstrBot 面板的「联网搜索」里配置，"
                            "插件只负责调用",
                        ),
                    ],
                    icon=ICON_FAIL,
                )
            )
            return
        section = Section(icon=ICON_OK, label="搜索完成")
        section.add(kv("条数", f"{len(outcome.hits)} 条"))
        for hit in self.web.format_hits(outcome.hits).splitlines():
            section.add(f"  {hit}")
        yield event.plain_result(section.text())

    @filter.command("空间日程", alias={"space life", "qz life"})
    async def cmd_life(self, event: AstrMessageEvent, action: GreedyStr = ""):
        """查看今日生活日程；带 renew 参数可重新生成"""
        self._remember_client(event)
        force = str(action).strip().lower() in _RENEW_FLAGS

        if force and not bool(self.cfg.life_inject_enabled):
            logger.info("重新生成生活日程（注入提示词为关闭状态）")

        try:
            state = await self.life.get_state(force=force)
        except Exception as e:
            yield event.plain_result(
                plain_receipt("读取日程失败", [kv("原因", e)], icon=ICON_FAIL)
            )
            return

        if state is None:
            yield event.plain_result(
                plain_receipt(
                    "没有拿到日程",
                    [kv("说明", "请先检查 AstrBot 里是否配置了可用的 LLM 提供商")],
                    icon=ICON_WARN,
                )
            )
            return

        if state.status != "ok":
            yield event.plain_result(
                plain_receipt(
                    "日程生成失败",
                    [kv("原因", "通常是 AI 不可用"), kv("详情", "见 AstrBot 日志")],
                    icon=ICON_WARN,
                )
            )
            return

        section = Section(
            icon=ICON_INFO, label="今日生活状态", title_extra=f"（{state.date}）"
        )
        section.add(
            kv("当前时段", time_desc()),
            kv("穿搭", state.outfit),
            kv("日程", state.schedule),
        )
        yield event.plain_result(section.text())

    @filter.command("私聊开", alias={"space pm on", "qz pm on"})
    async def cmd_pm_on(self, event: AstrMessageEvent, action: GreedyStr = ""):
        """开启主动私聊消息：不带参数开启全部，带参数只开该项（所有用户可用）"""
        self._remember_client(event)
        yield event.plain_result(self._apply_pm(event, str(action), enable=True))

    @filter.command("私聊关", alias={"space pm off", "qz pm off"})
    async def cmd_pm_off(self, event: AstrMessageEvent, action: GreedyStr = ""):
        """关闭主动私聊消息：不带参数关闭全部，带参数只关该项（所有用户可用）"""
        self._remember_client(event)
        yield event.plain_result(self._apply_pm(event, str(action), enable=False))

    def _apply_pm(self, event: AstrMessageEvent, spec: str, *, enable: bool) -> str:
        """处理「私聊开 / 私聊关」并给出回执。

        参数无法识别时只提示，不改动任何状态——尤其不会因此变成「已接受」。

        Args:
            event: 消息事件。
            spec: 参数（功能名，可为空）。
            enable: True 表示开启，False 表示关闭。

        Returns:
            回执文本。
        """
        qq = str(event.get_sender_id() or "").strip()
        if not qq.isdigit():
            return "无法识别你的 QQ 号，暂时不能修改设置"

        arg = spec.strip()
        head = arg.split()[0].lower() if arg.split() else ""

        if not arg:
            # 不带参数：开启 = 接受并全部开启；关闭 = 拒绝并全部关闭
            self.prefs.set_opted_in(qq, enable)
            self.prefs.set_all_features(qq, enable)
            note = (
                "已记录：接受主动消息，功能全部开启"
                if enable
                else "已记录：不接受主动消息，功能全部关闭"
            )
            return f"{plain_receipt(note, icon=ICON_OK)}\n{self._pm_text(qq)}"

        feature = self._feature_of(head)
        if feature is None:
            state = UserPrefStore.state_text(self.prefs.get(qq))
            kept = (
                "你的状态保持为未回答（视为不接受）"
                if state == "未回答"
                else f"你的状态保持为{state}，未做任何改动"
            )
            detail = kv("说明", f"未识别该功能名「{arg}」，{kept}")
            hint = kv("可用的功能名", "早安、晚安、节日、闲聊")
            head = plain_receipt("参数无法识别", [detail, hint], icon=ICON_WARN)
            return f"{head}\n{self._pm_text(qq)}"

        self.prefs.set_feature(qq, feature, enable)
        if enable:
            # 明确要求开启某一项，等同于接受（否则开了也收不到）
            self.prefs.set_opted_in(qq, True)
        label = FEATURE_LABELS[feature]
        head = f"已{'开启' if enable else '关闭'}{label}"
        return f"{plain_receipt(head, icon=ICON_OK)}\n{self._pm_text(qq)}"

    @staticmethod
    def _pm_usage(state: str = "") -> str:
        """两个指令的用法行，并说明随时可以改回来。"""
        back = {
            "已接受": "想全部停掉就用 /私聊关（只想停某一项就用 /私聊关 晚安）",
            "已拒绝": "想重新接收就用 /私聊开（只想开某一项就用 /私聊开 晚安）",
        }.get(state, "/私聊开 或 /私聊关")
        return "\n".join(
            [
                kv(
                    "用法",
                    f"{quote_command('私聊开')}开启全部｜"
                    f"{quote_command('私聊关')}关闭全部",
                ),
                kv(
                    "用法",
                    f"{quote_command('私聊开 早安')}只开某一项｜"
                    f"{quote_command('私聊关 晚安')}只关某一项",
                ),
                kv("随时可以改回来", back),
            ]
        )

    @staticmethod
    def _feature_of(token: str) -> str | None:
        """把用户输入的功能名映射成内部标识。"""
        table = {
            "morning": "morning",
            "早安": "morning",
            "早上": "morning",
            "night": "night",
            "晚安": "night",
            "晚上": "night",
            "holiday": "holiday",
            "节日": "holiday",
            "节日祝福": "holiday",
            "chat": "chat",
            "闲聊": "chat",
            "聊天": "chat",
            "日常闲聊": "chat",
        }
        return table.get(str(token).strip().lower())

    def _pm_text(self, qq: str) -> str:
        """拼本人主动消息设置的展示文本。"""
        user = self.prefs.get(qq)
        state = UserPrefStore.state_text(user)
        section = Section(icon=ICON_INFO, label="我的接收设置")
        section.add(
            kv("当前状态", f"{state}（{qq}）"),
            kv("功能开关", UserPrefStore.features_text(user)),
            kv(
                "时间段（由管理员设置，只读）",
                f"早安 {describe_cron(self.cfg.greet_morning_cron)}"
                f"｜晚安 {describe_cron(self.cfg.greet_night_cron)}"
                f"｜节日祝福 {describe_cron(self.cfg.holiday_cron)}"
                f"｜日常闲聊 {describe_windows(self.greet.chat_windows)}",
            ),
        )
        if state == "未回答":
            section.add(kv("说明", "未回答视为不接受，不会收到任何主动消息"))
        section.add(*self._pm_usage(state).splitlines())
        return section.text()

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间读说说", alias={"space read", "qz read"})
    async def cmd_read(self, event: AstrMessageEvent, force: GreedyStr = ""):
        """立即巡检一轮好友说说与自己说说下的评论（force 忽略去重）"""
        self._remember_client(event)
        force_flag = bool(str(force).strip())
        lines: list[str] = []

        if self.interact.targets:
            yield event.plain_result(
                plain_receipt(
                    "正在巡检",
                    [kv("范围", "好友说说，请稍候")],
                    icon=ICON_INFO,
                )
            )
            result = await self.interact.run_once(force=force_flag)
            lines.append(
                plain_receipt("巡检完成", result.summary().splitlines(), icon=ICON_OK)
            )
        else:
            lines.append(
                plain_receipt(
                    "没有巡检对象",
                    [kv("说明", "请在插件配置的 interact_uins 里填 QQ 号")],
                    icon=ICON_WARN,
                )
            )

        if bool(self.cfg.interact_reply_enabled):
            yield event.plain_result(
                plain_receipt(
                    "正在巡检",
                    [kv("范围", "自己说说下的评论，请稍候")],
                    icon=ICON_INFO,
                )
            )
            reply = await self.interact.run_replies_once(force=force_flag)
            lines.append(
                plain_receipt(
                    "评论回复完成", reply.summary().splitlines(), icon=ICON_OK
                )
            )

        yield event.plain_result("\n".join(lines))

        pending = self.drafts.pending
        if pending is not None and pending.kind == "comment":
            yield event.plain_result(pending.describe())

    # ------------------------------------------------------------------
    # 指令：管理员名单
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间管理员", alias={"space admin", "qz admin"})
    async def cmd_admin(self, event: AstrMessageEvent, action: GreedyStr = ""):
        """查看或修改插件内的管理员 QQ 名单"""
        self._remember_client(event)
        args = str(action).split()
        qqs, source = self._admin_qqs()

        if not args:
            section = Section(icon=ICON_INFO, label="管理员名单")
            section.add(
                kv("当前名单", "、".join(qqs) or "（空）"),
                kv("来源", source),
                kv(
                    "用法",
                    f"{quote_command('空间管理员 add 123456')}加入｜"
                    f"{quote_command('空间管理员 remove 123456')}移除",
                ),
                kv(
                    "说明",
                    "这里只影响草稿确认与通知发给谁；指令权限由 AstrBot 配置里的"
                    " admins_id 决定（插件不绕过它）",
                ),
            )
            yield event.plain_result(section.text())
            return

        sub = args[0].lower()
        targets = [item for item in args[1:] if item.strip()]
        if sub not in {"add", "remove", "del", "delete", "加", "删"} or not targets:
            yield event.plain_result(
                plain_receipt(
                    "参数无效",
                    [
                        kv(
                            "用法",
                            f"{quote_command('空间管理员 add 123456')}或"
                            f"{quote_command('空间管理员 remove 123456')}",
                        )
                    ],
                    icon=ICON_WARN,
                )
            )
            return

        current = [
            str(item).strip()
            for item in (self.cfg.admin_uins or [])
            if str(item).strip()
        ]
        bad = [item for item in targets if not item.isdigit()]
        if bad:
            yield event.plain_result(
                plain_receipt(
                    "名单有误",
                    [kv("不是 QQ 号", "、".join(bad))],
                    icon=ICON_FAIL,
                )
            )
            return

        if sub in {"add", "加"}:
            added = [item for item in targets if item not in current]
            current.extend(added)
            message = (
                f"已添加管理员：{'、'.join(added)}" if added else "这些 QQ 号已在名单里"
            )
        else:
            removed = [item for item in targets if item in current]
            current = [item for item in current if item not in targets]
            message = (
                f"已移除管理员：{'、'.join(removed)}"
                if removed
                else "这些 QQ 号不在名单里"
            )

        self.cfg.set("admin_uins", current)
        yield event.plain_result(
            plain_receipt(
                "管理员名单已更新",
                [
                    kv("结果", message),
                    kv(
                        "当前名单",
                        "、".join(current) or "（空，将回退用 AstrBot 的 admins_id）",
                    ),
                ],
                icon=ICON_OK,
            )
        )

    # ------------------------------------------------------------------
    # 指令：定时问候
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间问候", alias={"space greet", "qz greet"})
    async def cmd_greet(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """立即发一次问候用于测试；也支持 /空间问候 on|off 开关定时问候"""
        self._remember_client(event)
        parts = str(args).split()

        if not parts:
            morning = self.greet.slot_of("morning")
            night = self.greet.slot_of("night")
            upcoming = days_until(datetime.now(self.cfg.timezone).date())
            require_optin = bool(self.cfg.active_msg_require_optin)
            section = Section(icon=ICON_INFO, label="定时问候")
            section.add(
                kv(
                    "问候开关",
                    f"{'开' if bool(self.cfg.greet_enabled) else '关'}"
                    f"｜内容来源 "
                    f"{'AI 生成' if bool(self.cfg.greet_use_ai) else '文案池'}"
                    f"｜收件人 "
                    f"{'已同意的用户' if require_optin else 'greet_users'}",
                )
            )
            for slot, task in (
                (morning, self.greet_morning_task),
                (night, self.greet_night_task),
            ):
                if slot is None:
                    continue
                slot_targets = self._feature_targets(slot.key)
                section.add(
                    kv(
                        f"{slot.name}问候",
                        f"{task.cron or '未设置'}（下次 {task.next_run_time}）"
                        f"｜本次将发给 {len(slot_targets)} 人（已同意）",
                    )
                )
                if bool(self.cfg.greet_enabled) and not slot_targets:
                    section.add(kv("提醒", self._no_consent_note(slot.name, slot.key)))
            holiday_targets = self._feature_targets(HOLIDAY_KEY)
            section.add(
                kv(
                    "节日祝福",
                    f"{'开' if bool(self.cfg.holiday_enabled) else '关'}"
                    f"｜{self.greet_holiday_status(upcoming)}"
                    f"｜本次将发给 {len(holiday_targets)} 人（已同意）",
                )
            )
            if bool(self.cfg.holiday_enabled) and not holiday_targets:
                section.add(kv("提醒", self._no_consent_note("节日祝福", HOLIDAY_KEY)))
            section.add(
                kv(
                    "主动消息同意",
                    f"{'需要' if require_optin else '不需要'}"
                    f"（用户可用{quote_command('私聊开')}或"
                    f"{quote_command('私聊关')}自行设置）",
                ),
                kv(
                    "用法",
                    f"{quote_command('空间问候 on')}或"
                    f"{quote_command('空间问候 off')}开关定时问候",
                ),
                kv(
                    "用法",
                    f"{quote_command('空间问候 morning 123456')}立刻发一条给指定 QQ"
                    "（忽略当日去重，不受主动消息偏好限制）",
                ),
                kv(
                    "用法",
                    f"{quote_command('空间问候 holiday')}测试节日祝福",
                ),
            )
            yield event.plain_result(section.text())
            return

        flag = parts[0].lower()
        if flag in _ON_FLAGS or flag in _OFF_FLAGS:
            enabled = flag in _ON_FLAGS
            self.cfg.set("greet_enabled", enabled)
            if enabled:
                morning_cron = self.greet_morning_task.reconfigure(enabled=True)
                night_cron = self.greet_night_task.reconfigure(enabled=True)
                if not morning_cron and not night_cron:
                    yield event.plain_result(
                        plain_receipt(
                            "定时问候已开启",
                            [
                                kv("提醒", "早安与晚安的时间都为空"),
                                kv(
                                    "用法", "请先在面板里设置「早安时间」与「晚安时间」"
                                ),
                            ],
                            icon=ICON_WARN,
                        )
                    )
                    return
                yield event.plain_result(
                    plain_receipt(
                        "定时问候已开启",
                        [
                            kv(
                                "早安问候",
                                f"{morning_cron or '未设置'}"
                                f"（下次 {self.greet_morning_task.next_run_time}）"
                                f"｜本次将发给 "
                                f"{len(self._feature_targets('morning'))} 人（已同意）",
                            ),
                            kv(
                                "晚安问候",
                                f"{night_cron or '未设置'}"
                                f"（下次 {self.greet_night_task.next_run_time}）"
                                f"｜本次将发给 "
                                f"{len(self._feature_targets('night'))} 人（已同意）",
                            ),
                        ],
                        icon=ICON_OK,
                    )
                )
                return

            self.greet_morning_task.reconfigure(enabled=False)
            self.greet_night_task.reconfigure(enabled=False)
            yield event.plain_result(plain_receipt("定时问候已关闭", icon=ICON_WARN))
            return

        is_holiday = flag in {"holiday", "节日", "节日祝福"}
        slot = self.greet.slot_of(flag)
        if slot is None and not is_holiday:
            yield event.plain_result(
                plain_receipt(
                    "参数无效",
                    [
                        kv(
                            "用法",
                            f"{quote_command('空间问候 on')}或"
                            f"{quote_command('空间问候 off')}开关定时问候",
                        ),
                        kv(
                            "用法",
                            f"{quote_command('空间问候 morning 123456')}立刻发一条给指定 QQ｜"
                            f"{quote_command('空间问候 holiday')}测试节日祝福",
                        ),
                    ],
                    icon=ICON_WARN,
                )
            )
            return

        targets = [item for item in parts[1:] if item.isdigit()]
        if not targets and not self.greet.targets:
            yield event.plain_result(
                plain_receipt(
                    "没有发送对象",
                    [
                        kv("原因", "没指定 QQ 号，且 greet_users 也是空的"),
                        kv(
                            "用法",
                            f"{quote_command('空间问候 morning 123456')}指定一个",
                        ),
                    ],
                    icon=ICON_WARN,
                )
            )
            return

        if is_holiday:
            who = "、".join(targets) if targets else "配置里的对象"
            yield event.plain_result(
                plain_receipt(
                    "正在发送节日祝福",
                    [
                        kv("对象", who),
                        kv(
                            "说明",
                            "测试发送：忽略今天是否节日、忽略当日去重，"
                            "不受主动消息偏好限制",
                        ),
                    ],
                    icon=ICON_INFO,
                )
            )
            try:
                result = await self.greet.send_holiday(
                    targets=targets or None, force=True, record=False, check_optin=False
                )
            except Exception as e:
                yield event.plain_result(
                    plain_receipt("发送失败", [kv("原因", e)], icon=ICON_FAIL)
                )
                return
            yield event.plain_result(self._greet_result_text("节日祝福", result))
            return

        yield event.plain_result(
            plain_receipt(
                f"正在发送{slot.name}问候",
                [
                    kv("对象", "、".join(targets) if targets else "配置里的对象"),
                    kv("说明", "管理员手动发送，不受主动消息偏好限制"),
                ],
                icon=ICON_INFO,
            )
        )
        try:
            # 手动发送不写「今日已问候」记录：否则会把当天的定时问候名额用掉，
            # 到点时定时任务会认为已经发过而直接跳过（这正是「日志成功但没收到」的成因之一）；
            # 同时跳过偏好检查：这是管理员显式指定对象的动作
            result = await self.greet.send(
                slot.key,
                targets=targets or None,
                force=True,
                record=False,
                check_optin=False,
            )
        except Exception as e:
            yield event.plain_result(
                plain_receipt("发送失败", [kv("原因", e)], icon=ICON_FAIL)
            )
            return

        yield event.plain_result(self._greet_result_text(f"{slot.name}问候", result))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间闲聊", alias={"space chat", "qz chat"})
    async def cmd_chat(self, event: AstrMessageEvent, action: GreedyStr = ""):
        """查看或开关主动闲聊；now 立刻发一条测试（仅管理员）"""
        self._remember_client(event)
        parts = str(action).split()
        require_optin = bool(self.cfg.active_msg_require_optin)

        if not parts:
            windows = self.greet.chat_windows
            today = datetime.now(self.cfg.timezone).date()
            targets = self._feature_targets(CHAT_KEY)
            section = Section(icon=ICON_INFO, label="主动闲聊")
            section.add(
                kv(
                    "开关",
                    "开启" if bool(self.cfg.chat_open_enabled) else "关闭",
                ),
                kv(
                    "时间窗口",
                    f"{describe_windows(windows)}（在每个窗口内随机取一个时刻发送）",
                ),
                kv(
                    "每天上限",
                    f"{self.greet.chat_per_day} 条｜每次只发 1 人"
                    "｜同一个人每天最多 1 条",
                ),
                kv(
                    "收件人",
                    f"{'已同意的用户' if require_optin else 'greet_users'}"
                    f"｜本次将发给 {len(targets)} 人（已同意）",
                ),
                kv(
                    "今日已发",
                    f"{self.greet.sent_today(self.greet.chat_slot_key(today))} 人"
                    f"｜下一个窗口 {self.next_chat_window_text()}",
                ),
                kv("说明", "主动闲聊是私聊内容且已获对方同意，不经过草稿确认"),
                kv(
                    "用法",
                    f"{quote_command('空间闲聊 on')}或"
                    f"{quote_command('空间闲聊 off')}开关主动闲聊；"
                    f"{quote_command('空间闲聊 now')}立刻发一条测试",
                ),
            )
            if bool(self.cfg.chat_open_enabled) and not windows:
                section.add(
                    kv("提醒", "没有可用的时间窗口，请在配置里按 HH:MM-HH:MM 填写")
                )
            yield event.plain_result(section.text())
            return

        flag = parts[0].lower()
        if flag in _ON_FLAGS or flag in _OFF_FLAGS:
            enabled = flag in _ON_FLAGS
            self.cfg.set("chat_open_enabled", enabled)
            crons = self._rebuild_chat_tasks()
            windows = self.greet.chat_windows
            if not enabled:
                yield event.plain_result(
                    plain_receipt("主动闲聊已关闭", icon=ICON_WARN)
                )
                return
            if not windows:
                yield event.plain_result(
                    plain_receipt(
                        "主动闲聊已开启",
                        [
                            kv("提醒", "没有可用的时间窗口"),
                            kv(
                                "用法",
                                "请在「主动闲聊的时间窗口」里按 HH:MM-HH:MM 填写",
                            ),
                        ],
                        icon=ICON_WARN,
                    )
                )
                return
            yield event.plain_result(
                plain_receipt(
                    "主动闲聊已开启",
                    [
                        kv(
                            "时间窗口",
                            f"{describe_windows(windows)}｜每天最多 "
                            f"{self.greet.chat_per_day} 条",
                        ),
                        kv(
                            "下次执行",
                            f"{self.next_chat_window_text()}（已排期 "
                            f"{len([item for item in crons if item])} 个窗口）",
                        ),
                    ],
                    icon=ICON_OK,
                )
            )
            return

        if flag not in _NOW_FLAGS:
            yield event.plain_result(
                plain_receipt(
                    "参数无效",
                    [
                        kv(
                            "用法",
                            f"{quote_command('空间闲聊 on')}或"
                            f"{quote_command('空间闲聊 off')}开关；"
                            f"{quote_command('空间闲聊 now')}立刻发一条测试",
                        )
                    ],
                    icon=ICON_WARN,
                )
            )
            return

        targets = self._feature_targets(CHAT_KEY)
        if not targets:
            note = (
                self._no_consent_note("日常闲聊", CHAT_KEY)
                if require_optin
                else "没有可发送对象：请在配置里填写对象"
            )
            yield event.plain_result(
                plain_receipt("主动闲聊没有发送", [kv("原因", note)], icon=ICON_WARN)
            )
            return

        yield event.plain_result(
            plain_receipt(
                "正在发送测试搭话",
                [
                    kv("对象", f"{len(targets)} 个已同意对象中的一位"),
                    kv(
                        "说明",
                        "忽略时间窗口与当日去重，仍遵守主动消息同意设置",
                    ),
                ],
                icon=ICON_INFO,
            )
        )
        try:
            result = await self.greet.send_chat_open(
                targets=targets, force=True, record=False
            )
        except Exception as e:
            yield event.plain_result(
                plain_receipt("发送失败", [kv("原因", e)], icon=ICON_FAIL)
            )
            return
        yield event.plain_result(self._greet_result_text("主动闲聊", result))

    @staticmethod
    def _greet_result_text(name: str, result) -> str:
        """拼一条问候类结果的回执文本。

        Args:
            name: 展示名（早安问候 / 节日祝福…）。
            result: ``GreetResult``。

        Returns:
            多行回执文本。
        """
        lines = result.summary().splitlines()
        if result.targets_used:
            lines.append(
                kv(
                    "发送地址",
                    "；".join(
                        f"{qq} → {umo}" for qq, umo in result.targets_used.items()
                    ),
                )
            )
        lines.append(kv("内容", result.text))
        return plain_receipt(name, lines, icon=ICON_OK)

    # ------------------------------------------------------------------
    # 指令：草稿确认
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间用量", alias={"space usage", "qz usage"})
    async def cmd_usage(self, event: AstrMessageEvent, days: GreedyStr = ""):
        """查看 AI token 用量估算（可按天数，如 /空间用量 7）"""
        self._remember_client(event)
        try:
            span = int(str(days).strip() or 1)
        except ValueError:
            yield event.plain_result(
                plain_receipt(
                    "参数无效",
                    [
                        kv(
                            "用法",
                            f"{quote_command('空间用量')}默认看今天，"
                            f"{quote_command('空间用量 7')}看最近 7 天",
                        )
                    ],
                    icon=ICON_WARN,
                )
            )
            return
        span = min(max(span, 1), 60)

        section = Section(icon=ICON_INFO, label="AI 用量估算")
        for line in self.ai.usage.format_summary(span, indent="").splitlines():
            section.add(f"  {line}" if line.strip() else line)
        section.add(
            kv(
                "说明",
                "按文本长度粗估（中文约 0.7 token/字），与实际计费存在 ±20% 左右误差，"
                "仅供心里有数",
            )
        )
        yield event.plain_result(section.text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间确认", alias={"space ok", "qz ok"})
    async def cmd_confirm(self, event: AstrMessageEvent):
        """确认并发布当前草稿"""
        self._remember_client(event)
        self._cancel_draft_timer()
        draft = self.drafts.pop()
        if draft is None:
            yield event.plain_result(
                plain_receipt(
                    "没有待确认的草稿",
                    [kv("说明", "当前草稿箱是空的")],
                    icon=ICON_INFO,
                )
            )
            return

        yield event.plain_result(
            plain_receipt("正在发布", [kv("草稿", draft.title())], icon=ICON_INFO)
        )
        try:
            message = await self._confirm_draft(draft)
        except Exception as e:
            self.drafts.put(draft)
            yield event.plain_result(
                plain_receipt(
                    "发布失败",
                    [kv("原因", e), kv("说明", "草稿已保留，修正后可再确认")],
                    icon=ICON_FAIL,
                )
            )
            return

        yield event.plain_result(message)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间放弃", alias={"space drop", "qz drop"})
    async def cmd_drop(self, event: AstrMessageEvent):
        """丢弃当前草稿"""
        self._cancel_draft_timer()
        if not self.drafts.clear():
            yield event.plain_result(
                plain_receipt(
                    "没有待确认的草稿",
                    [kv("说明", "当前草稿箱是空的")],
                    icon=ICON_INFO,
                )
            )
            return
        yield event.plain_result(plain_receipt("草稿已丢弃", icon=ICON_WARN))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间重写", alias={"space redo", "qz redo"})
    async def cmd_redo(self, event: AstrMessageEvent):
        """让 AI 按同一目标重写一版草稿"""
        self._remember_client(event)
        draft = self.drafts.pending
        if draft is None:
            yield event.plain_result(
                plain_receipt(
                    "没有待确认的草稿",
                    [kv("说明", "当前草稿箱是空的")],
                    icon=ICON_INFO,
                )
            )
            return

        yield event.plain_result(
            plain_receipt(
                "正在重写",
                [kv("说明", "让 AI 再写一版草稿，请稍候")],
                icon=ICON_INFO,
            )
        )
        try:
            if draft.kind == "comment":
                text = await self.interact.rewrite_comment(draft)
                new_draft = Draft(
                    kind="comment",
                    text=text,
                    source="interact",
                    target_uin=draft.target_uin,
                    target_tid=draft.target_tid,
                    target_name=draft.target_name,
                    target_text=draft.target_text,
                )
            elif draft.kind == "reply":
                # 回复不再走草稿：旧版本遗留的回复草稿无法重写，直接丢弃
                self.drafts.clear()
                yield event.plain_result(
                    plain_receipt(
                        "遗留草稿已失效",
                        [
                            kv("原因", "回复评论不再经过草稿确认"),
                            kv(
                                "说明",
                                "这条旧版本的回复草稿已丢弃，回复会由巡检直接发出",
                            ),
                        ],
                        icon=ICON_WARN,
                    )
                )
                return
            else:
                text = await self.content.rewrite(previous=draft.text)
                new_draft = Draft(
                    kind="post", text=text, source="rewrite", images=draft.images
                )
        except Exception as e:
            yield event.plain_result(
                plain_receipt(
                    "重写失败",
                    [kv("原因", e), kv("说明", "原草稿仍保留")],
                    icon=ICON_FAIL,
                )
            )
            return

        self.drafts.put(new_draft)
        yield event.plain_result(new_draft.describe())

    # ------------------------------------------------------------------
    # 指令：历史与删除
    # ------------------------------------------------------------------

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间历史", alias={"space history", "qz history"})
    async def cmd_history(self, event: AstrMessageEvent, count: int = 5):
        """查看最近的发布记录（仅管理员）"""
        size = min(max(int(count or 5), 1), 20)
        records = self.store.recent(size)

        if not records:
            yield event.plain_result(
                plain_receipt(
                    "暂无发布记录",
                    [kv("说明", "还没有成功发布过说说")],
                    icon=ICON_INFO,
                )
            )
            return

        section = Section(
            icon=ICON_INFO, label="发布记录", title_extra=f"（最近 {len(records)} 条）"
        )
        for record in records:
            state = "成功" if record.ok else "失败"
            summary = (record.text or "").replace("\n", " ")[:40]
            section.add(
                kv(
                    self._format_time(record.time),
                    f"{state}｜来源 {record.source}｜tid "
                    f"{record.tid or '-'}｜{summary}",
                )
            )
            if not record.ok and record.error:
                section.add(f"    {kv('失败原因', record.error)}")
        yield event.plain_result(section.text())

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("空间删除", alias={"space delete", "qz delete"})
    async def cmd_delete(self, event: AstrMessageEvent, tid: GreedyStr):
        """删除指定说说，tid 可通过 /空间历史 查看"""
        self._remember_client(event)
        target = str(tid).strip()
        if not target:
            yield event.plain_result(
                plain_receipt(
                    "参数无效",
                    [
                        kv("用法", f"{quote_command('空间删除 <tid>')}删除指定说说"),
                        kv("查 tid", f"可用{quote_command('空间历史')}查看"),
                    ],
                    icon=ICON_WARN,
                )
            )
            return

        try:
            resp = await self.api.delete(target)
        except Exception as e:
            yield event.plain_result(
                plain_receipt("删除失败", [kv("原因", e)], icon=ICON_FAIL)
            )
            return

        if resp.ok:
            yield event.plain_result(
                plain_receipt("删除成功", [kv("tid", target)], icon=ICON_OK)
            )
        else:
            yield event.plain_result(
                plain_receipt(
                    "删除失败", [kv("原因", resp.message or resp.code)], icon=ICON_FAIL
                )
            )
