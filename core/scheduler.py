"""定时任务调度：Cron 表达式 + 随机抖动 + 热更新。

发布任务与互动巡检任务都是同一个 :class:`CronTask`，只是名字、Cron 与回调不同。
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from astrbot.api import logger

from .config import PluginConfig

_TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{1,2})$")
_UNSET = object()


def normalize_cron(spec: str) -> str | None:
    """把用户输入统一成 5 段 Cron 表达式。

    Args:
        spec: "HH:MM"、5 段 Cron 表达式，或空字符串（表示不启用）。

    Returns:
        规范化的 Cron 表达式；输入为空时返回 None。

    Raises:
        ValueError: 格式无法识别或时间越界时抛出。
    """
    text = str(spec or "").strip()
    if not text:
        return None

    match = _TIME_PATTERN.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("时间需在 00:00 ~ 23:59 之间")
        return f"{minute} {hour} * * *"

    fields = text.split()
    if len(fields) == 5:
        return " ".join(fields)

    raise ValueError("无法识别的时间格式，请使用 HH:MM 或 5 段 Cron 表达式")


class CronTask:
    """配置驱动的 Cron 定时任务。

    Attributes:
        name: 任务名，同时作为 APScheduler 的 job id。
        job: 触发时执行的无参协程。
        cron: 规范化后的 Cron 表达式，None 表示不调度。
        jitter: 触发时间上随机延后的最大秒数。
        enabled: 开关。
        error: 最近一次配置错误信息，供状态指令展示。
    """

    def __init__(
        self,
        *,
        name: str,
        timezone,
        job: Callable[[], Awaitable[None]],
        cron: str | None = None,
        jitter: int = 0,
        enabled: bool = True,
    ) -> None:
        """初始化任务。

        Args:
            name: 任务名。
            timezone: 调度时区。
            job: 触发时执行的无参协程。
            cron: Cron 表达式或 HH:MM。
            jitter: 随机抖动秒数。
            enabled: 是否启用。
        """
        self.name = name
        self.timezone = timezone
        self.job = job
        self.jitter = max(int(jitter or 0), 0)
        self.enabled = bool(enabled)
        self.error = ""
        self.cron: str | None = None
        self._scheduler: AsyncIOScheduler | None = None
        self._job = None
        self._set_cron(cron)

    @classmethod
    def from_config(
        cls,
        config: PluginConfig,
        *,
        name: str,
        job: Callable[[], Awaitable[None]],
        cron_key: str,
        jitter_key: str = "",
        enabled_key: str = "",
    ) -> CronTask:
        """按配置项构造任务。

        Args:
            config: 插件配置。
            name: 任务名。
            job: 触发时执行的无参协程。
            cron_key: 时间配置项名。
            jitter_key: 抖动配置项名，留空表示无抖动。
            enabled_key: 开关配置项名，留空表示恒定启用。

        Returns:
            构造好的任务（配置非法时 error 会被填充，任务保持不调度）。
        """
        return cls(
            name=name,
            timezone=config.timezone,
            job=job,
            cron=getattr(config, cron_key, "") if cron_key else "",
            jitter=getattr(config, jitter_key, 0) if jitter_key else 0,
            enabled=bool(getattr(config, enabled_key, True)) if enabled_key else True,
        )

    def _set_cron(self, cron: str | None) -> None:
        """设置 Cron，非法时记录错误并置空。"""
        self.error = ""
        try:
            self.cron = normalize_cron(cron or "")
        except ValueError as e:
            self.cron = None
            self.error = str(e)

    @property
    def running(self) -> bool:
        """调度器是否正在运行。"""
        return self._scheduler is not None and self._scheduler.running

    @property
    def next_run_time(self) -> str:
        """下次执行时间的可读文本。"""
        if self._job is None:
            return "未调度"
        try:
            return self._job.next_run_time.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "未知"

    def start(self) -> str | None:
        """启动调度。

        Returns:
            生效的 Cron 表达式；未启用、未配置或启动失败时返回 None。
        """
        if self.running:
            return self.cron

        if self.error:
            logger.error(f"[{self.name}] 时间配置有误，未启动: {self.error}")
            return None
        if not self.enabled:
            logger.info(f"[{self.name}] 未启用")
            return None
        if not self.cron:
            logger.info(f"[{self.name}] 未配置时间，保持关闭")
            return None

        try:
            self._scheduler = AsyncIOScheduler(
                timezone=self.timezone,
                job_defaults={
                    "coalesce": True,
                    "max_instances": 1,
                    "misfire_grace_time": 300,
                },
            )
            self._job = self._scheduler.add_job(
                self._run,
                self._build_trigger(),
                id=self.name,
                name=self.name,
                replace_existing=True,
            )
            self._scheduler.start()
        except Exception as e:
            logger.error(f"[{self.name}] 调度器启动失败: {e}")
            self._scheduler = None
            self._job = None
            return None

        logger.info(f"[{self.name}] 已启动: {self.cron}，下次执行 {self.next_run_time}")
        return self.cron

    def stop(self) -> None:
        """停止调度并释放资源。"""
        if self._scheduler is None:
            return
        try:
            self._scheduler.remove_all_jobs()
            if self._scheduler.running:
                self._scheduler.shutdown(wait=False)
        except Exception as e:
            logger.debug(f"[{self.name}] 关闭调度器时忽略异常: {e}")
        self._scheduler = None
        self._job = None
        logger.info(f"[{self.name}] 已停止")

    def reconfigure(
        self,
        *,
        cron: object = _UNSET,
        jitter: int | None = None,
        enabled: bool | None = None,
    ) -> str | None:
        """更新参数并按需重启调度（用于指令改配置后的热更新）。

        Args:
            cron: 新的 Cron 表达式，缺省不改；传 None 或空串表示关闭调度。
            jitter: 新的抖动秒数，缺省不改。
            enabled: 新的开关，缺省不改。

        Returns:
            重启后生效的 Cron 表达式；未调度时返回 None。
        """
        if cron is not _UNSET:
            self._set_cron(cron if isinstance(cron, str) else "")
        if jitter is not None:
            self.jitter = max(int(jitter), 0)
        if enabled is not None:
            self.enabled = bool(enabled)

        self.stop()
        return self.start()

    def _build_trigger(self) -> CronTrigger:
        """由 Cron 表达式构造触发器。"""
        if not self.cron:
            raise ValueError(f"[{self.name}] 没有可用的 Cron 表达式")
        minute, hour, day, month, day_of_week = self.cron.split()
        return CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=day_of_week,
            timezone=self.timezone,
            jitter=self.jitter or None,
        )

    async def _run(self) -> None:
        """调度入口：执行一次任务并兜住所有异常。"""
        logger.info(f"[{self.name}] 开始执行")
        try:
            await self.job()
        except Exception as e:
            logger.exception(f"[{self.name}] 执行失败: {e}")
        finally:
            logger.info(f"[{self.name}] 执行结束")
