"""定时任务调度：Cron 表达式 + 随机抖动 + 热更新。

发布任务与互动巡检任务都是同一个 :class:`CronTask`，只是名字、Cron 与回调不同。

发布支持「一天多条」：:class:`CronTaskGroup` 把多个时间点拆成多个 :class:`CronTask`
（名字形如 ``qzone_auto_publish[1]``），每个时间点独立计算下次触发时间与随机抖动，
改配置时整体重建即可热更新。
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


def split_times(spec: object) -> list[str]:
    """把时间点配置拆成字符串列表。

    支持列表入参，也支持用逗号、顿号或空格分隔的字符串（如 ``08:30,12:30``）。

    Args:
        spec: 配置值（列表或字符串）。

    Returns:
        去空、去重（保持顺序）后的时间点列表。
    """
    if isinstance(spec, (list, tuple)):
        items = [str(item).strip() for item in spec]
    else:
        items = [part.strip() for part in re.split(r"[,，、\s]+", str(spec or ""))]
    result: list[str] = []
    for item in items:
        if item and item not in result:
            result.append(item)
    return result


def describe_cron(spec: object) -> str:
    """把时间配置说成给人看的话。

    Args:
        spec: "HH:MM"、5 段 Cron 或空值。

    Returns:
        形如「每天 08:30」的说明；表达式过于复杂时原样返回。
    """
    try:
        cron = normalize_cron(str(spec or ""))
    except ValueError:
        return f"（无法识别：{spec}）"
    if not cron:
        return "未设置"
    minute, hour, day, month, day_of_week = cron.split()
    if (
        day == "*"
        and month == "*"
        and day_of_week == "*"
        and hour.isdigit()
        and minute.isdigit()
    ):
        return f"每天 {int(hour):02d}:{int(minute):02d}"
    return f"Cron {cron}"


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

    @property
    def next_run_datetime(self):
        """下次执行时间（datetime）；未调度或取不到时返回 None。"""
        if self._job is None:
            return None
        try:
            return self._job.next_run_time
        except Exception:
            return None

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


class CronTaskGroup:
    """同一任务名下的多个时间点（用于「一天发几条」）。

    每个时间点对应一个独立的 :class:`CronTask`，任务名形如 ``{name}[{序号}]``，
    因此各时间点的下次触发时间与随机抖动互不影响；改配置时整体重建即完成热更新。

    Attributes:
        name: 任务组名，同时作为子任务名前缀。
        times: 时间点列表（保留用户填写的原始写法）。
        per_day: 每天发布条数；0 表示不自动发布，超过列表长度时按列表长度算。
        fallback_cron: 时间点列表为空或全部非法时，退回使用的兼容时间配置。
        jitter: 每个时间点各自的随机抖动秒数。
        enabled: 总开关。
        error: 配置问题说明（含有无法识别的时间点时也会写在这里）。
    """

    def __init__(
        self,
        *,
        name: str,
        timezone,
        job: Callable[[], Awaitable[None]],
        times: object = None,
        per_day: int = 1,
        fallback_cron: object = "",
        jitter: int = 0,
        enabled: bool = True,
    ) -> None:
        """初始化任务组。

        Args:
            name: 任务组名。
            timezone: 调度时区。
            job: 触发时执行的无参协程。
            times: 时间点列表（或逗号分隔的字符串）。
            per_day: 每天发布条数。
            fallback_cron: 兼容用的单一时间配置。
            jitter: 随机抖动秒数。
            enabled: 是否启用。
        """
        self.name = name
        self.timezone = timezone
        self.job = job
        self.times = split_times(times)
        self.per_day = max(int(per_day or 0), 0)
        self.fallback_cron = str(fallback_cron or "").strip()
        self.jitter = max(int(jitter or 0), 0)
        self.enabled = bool(enabled)
        self.error = ""
        self._tasks: list[CronTask] = []
        self._raws: list[str] = []
        self._crons: list[str] = []
        self._refresh()

    @classmethod
    def from_config(
        cls,
        config: PluginConfig,
        *,
        name: str,
        job: Callable[[], Awaitable[None]],
        times_key: str,
        per_day_key: str = "",
        cron_key: str = "",
        jitter_key: str = "",
        enabled_key: str = "",
    ) -> CronTaskGroup:
        """按配置项构造任务组。

        Args:
            config: 插件配置。
            name: 任务组名。
            job: 触发时执行的无参协程。
            times_key: 时间点列表配置项名。
            per_day_key: 每天条数配置项名。
            cron_key: 兼容用的单一时间配置项名。
            jitter_key: 抖动配置项名。
            enabled_key: 开关配置项名。

        Returns:
            构造好的任务组。
        """
        return cls(
            name=name,
            timezone=config.timezone,
            job=job,
            times=getattr(config, times_key, []) if times_key else [],
            per_day=int(getattr(config, per_day_key, 1) or 0) if per_day_key else 1,
            fallback_cron=getattr(config, cron_key, "") if cron_key else "",
            jitter=int(getattr(config, jitter_key, 0) or 0) if jitter_key else 0,
            enabled=bool(getattr(config, enabled_key, True)) if enabled_key else True,
        )

    # ------------------------------------------------------------------
    # 时间点解析
    # ------------------------------------------------------------------

    def _resolve(self) -> tuple[list[str], list[str]]:
        """解析生效的时间点。

        Returns:
            (生效的原始写法列表, 规范化后的 Cron 列表)；
            ``per_day`` 为 0 时都为空；列表为空或全部非法时回退 ``fallback_cron``。
        """
        raws: list[str] = []
        crons: list[str] = []
        invalid: list[str] = []

        if self.per_day > 0:
            for item in self.times[: self.per_day]:
                text = str(item).strip()
                if not text:
                    continue
                try:
                    crons.append(normalize_cron(text))
                except ValueError:
                    invalid.append(text)
                    continue
                raws.append(text)

        if not crons and self.per_day > 0 and self.fallback_cron:
            try:
                crons = [normalize_cron(self.fallback_cron)]
                raws = [self.fallback_cron]
            except ValueError:
                invalid.append(self.fallback_cron)

        self.error = f"忽略无法识别的时间点：{'、'.join(invalid)}" if invalid else ""
        return raws, crons

    def _refresh(self) -> None:
        """重新计算生效的时间点（不启动调度）。"""
        self._raws, self._crons = self._resolve()

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------

    @property
    def tasks(self) -> list[CronTask]:
        """当前的子任务列表。"""
        return list(self._tasks)

    @property
    def crons(self) -> list[str]:
        """当前生效的 Cron 表达式列表。"""
        return list(self._crons)

    @property
    def times_used(self) -> list[str]:
        """当前生效的时间点（用户填写的原始写法）。"""
        return list(self._raws)

    @property
    def cron(self) -> str | None:
        """兼容单任务展示：把多个 Cron 逗号连接。"""
        return ", ".join(self._crons) if self._crons else None

    @property
    def running(self) -> bool:
        """是否有子任务正在调度。"""
        return any(task.running for task in self._tasks)

    @property
    def next_run_time(self) -> str:
        """所有时间点里最早的下次执行时间。"""
        stamps = [
            item
            for item in (task.next_run_datetime for task in self._tasks)
            if item is not None
        ]
        if not stamps:
            return "未调度"
        try:
            return min(stamps).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return "未知"

    def describe(self) -> str:
        """人话描述当前设置。"""
        if self.per_day <= 0:
            return "每天 0 条（不自动发布）"
        if not self._crons:
            return "未设置可用的发布时间点"
        return f"每天 {len(self._crons)} 条：{'、'.join(self._raws)}"

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------

    def start(self) -> list[str]:
        """按当前时间点重建并启动所有子任务。

        Returns:
            实际生效的 Cron 表达式列表；未启用或无可用时返回空列表。
        """
        self.stop()
        self._refresh()

        if not self.enabled:
            logger.info(f"[{self.name}] 未启用")
            return []
        if not self._crons:
            logger.info(f"[{self.name}] 没有可用的发布时间点，保持关闭")
            return []

        for index, cron in enumerate(self._crons, start=1):
            task = CronTask(
                name=f"{self.name}[{index}]",
                timezone=self.timezone,
                job=self.job,
                cron=cron,
                jitter=self.jitter,
                enabled=True,
            )
            task.start()
            self._tasks.append(task)

        # 兼容项与时间点列表同时存在且不一致时说清按哪个执行，避免用户以为改错了
        if self.fallback_cron:
            try:
                fallback = normalize_cron(self.fallback_cron)
            except ValueError:
                fallback = None
            if fallback and fallback not in self._crons:
                logger.info(
                    f"[{self.name}] 本次按时间点列表执行（{'、'.join(self._raws)}）；"
                    f"配置里的兼容时间「{self.fallback_cron}」未生效"
                )
        return list(self._crons)

    def stop(self) -> None:
        """停止并清理所有子任务。"""
        for task in self._tasks:
            task.stop()
        self._tasks = []

    def reconfigure(
        self,
        *,
        times: object = _UNSET,
        per_day: int | None = None,
        cron: object = _UNSET,
        jitter: int | None = None,
        enabled: bool | None = None,
    ) -> list[str]:
        """更新参数并重建调度（用于指令改配置后的热更新）。

        Args:
            times: 新的时间点列表，缺省不改；传空表示清空。
            per_day: 新的每天条数，缺省不改。
            cron: 新的兼容时间配置，缺省不改。
            jitter: 新的抖动秒数，缺省不改。
            enabled: 新的开关，缺省不改。

        Returns:
            重建后生效的 Cron 表达式列表。
        """
        if times is not _UNSET:
            self.times = split_times(times)
        if per_day is not None:
            self.per_day = max(int(per_day), 0)
        if cron is not _UNSET:
            self.fallback_cron = "" if cron is None else str(cron).strip()
        if jitter is not None:
            self.jitter = max(int(jitter), 0)
        if enabled is not None:
            self.enabled = bool(enabled)
        return self.start()
