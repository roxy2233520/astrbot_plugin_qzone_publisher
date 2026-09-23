"""节日祝福用的节日表与查询函数。

**节日清单（共 7 个）**：

- 农历节日 5 个：除夕（腊月最后一天）、春节（正月初一）、元宵（正月十五）、
  七夕（七月初七）、中秋（八月十五）；
- 固定公历节日 2 个：情人节（2 月 14 日）、国庆（10 月 1 日）。

**农历节日数据来源（2026-2030 逐年核对）**：香港天文台《公曆與農曆對照表》
https://my.weather.gov.hk/tc/gts/time/conversion.htm
以及各年度对照表文本 https://www.hko.gov.hk/tc/gts/time/calendar/text/files/T2026c.txt
（2027-2030 年同规律，文件名为 ``T{年}c.txt``）。

**核对方式**：该表按公历逐日列出农历日期，农历每月初一显示为月名（如「正月」）。
本表由一段本地核对脚本逐日解析五份年度对照表得到：取目标农历月日对应的公历日期；
除夕取春节前一天（农历腊月可能只有 29 天，除夕未必是「年三十」）。
核对脚本不随插件发布，解析逻辑见本文件开头的来源说明。

**超出覆盖范围时不会静默失效**：农历节日只覆盖 2026-2030 年，查询落在表外的年份会写
warning 日志提醒更新本表；固定公历节日不受年份限制。

**要增删节日时**：只能改本文件的 ``LUNAR_FESTIVALS`` / ``FIXED_FESTIVALS`` / ``FESTIVAL_NAMES``，
并同步 ``core/holidays.py`` 中的年份说明、README 与自测断言；农历节日必须重新按上述来源核对
（不要凭记忆推算）。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from astrbot.api import logger

# 支持的节日（顺序即展示顺序）
FESTIVAL_NAMES: tuple[str, ...] = (
    "除夕",
    "春节",
    "元宵",
    "情人节",
    "七夕",
    "中秋",
    "国庆",
)

# 农历节日的公历日期表（2026-2030，来源见模块开头说明）
LUNAR_FESTIVALS: dict[str, str] = {
    # 2026 年
    "2026-02-16": "除夕",
    "2026-02-17": "春节",
    "2026-03-03": "元宵",
    "2026-08-19": "七夕",
    "2026-09-25": "中秋",
    # 2027 年
    "2027-02-05": "除夕",
    "2027-02-06": "春节",
    "2027-02-20": "元宵",
    "2027-08-08": "七夕",
    "2027-09-15": "中秋",
    # 2028 年
    "2028-01-25": "除夕",
    "2028-01-26": "春节",
    "2028-02-09": "元宵",
    "2028-08-26": "七夕",
    "2028-10-03": "中秋",
    # 2029 年
    "2029-02-12": "除夕",
    "2029-02-13": "春节",
    "2029-02-27": "元宵",
    "2029-08-16": "七夕",
    "2029-09-22": "中秋",
    # 2030 年
    "2030-02-02": "除夕",
    "2030-02-03": "春节",
    "2030-02-17": "元宵",
    "2030-08-05": "七夕",
    "2030-09-12": "中秋",
}

# 固定公历节日：键为 "月-日"，任何年份都成立，不进农历表
FIXED_FESTIVALS: dict[str, str] = {
    "02-14": "情人节",
    "10-01": "国庆",
}

TABLE_START = date(2026, 1, 1)
TABLE_END = date(2030, 12, 31)

# 往后找下一个节日时最多扫多少天（固定节日保证一年内必有一个，留出余量）
MAX_SCAN_DAYS = 400

# 每个年份只提醒一次，避免日志被刷屏
_WARNED_YEARS: set[int] = set()


def table_range_text() -> str:
    """返回农历节日表覆盖范围的说明文本。"""
    return f"{TABLE_START.year}-{TABLE_END.year} 年"


def _as_date(value: date | datetime | str | None) -> date:
    """把入参统一成 date；None 表示今天。"""
    if value is None:
        return date.today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value).strip())


def as_date(value: date | datetime | str | None = None) -> date:
    """把入参统一成 date。

    Args:
        value: date / datetime / ISO 日期字符串；None 表示今天。

    Returns:
        对应的 date 对象。
    """
    return _as_date(value)


def _warn_out_of_range(day: date) -> None:
    """农历节日表覆盖不到该年份时写一次 warning。"""
    if TABLE_START <= day <= TABLE_END or day.year in _WARNED_YEARS:
        return
    _WARNED_YEARS.add(day.year)
    logger.warning(
        f"农历节日表只覆盖 {table_range_text()}，{day.year} 年的农历节日不会触发"
        "（情人节、国庆等固定公历节日不受影响）；"
        "请更新 core/holidays.py（数据来源见该文件开头说明）"
    )


def _lunar_festival(day: date) -> str | None:
    """查农历节日；超出表范围时写 warning 并返回 None。"""
    if not (TABLE_START <= day <= TABLE_END):
        _warn_out_of_range(day)
        return None
    return LUNAR_FESTIVALS.get(day.isoformat())


def festival_of(value: date | datetime | str | None = None) -> str | None:
    """查询某一天是不是节日。

    Args:
        value: 目标日期；None 表示今天。

    Returns:
        节日名；不是节日时返回 None。固定公历节日（情人节、国庆）任何年份都有效。
    """
    day = _as_date(value)
    fixed = FIXED_FESTIVALS.get(day.strftime("%m-%d"))
    if fixed:
        return fixed
    return _lunar_festival(day)


def next_festival(
    value: date | datetime | str | None = None,
) -> tuple[str, date] | None:
    """查询从某天（含当天）起的下一个节日。

    Args:
        value: 起始日期；None 表示今天。

    Returns:
        (节日名, 公历日期)；扫描范围内找不到时返回 None。
    """
    start = _as_date(value)
    for offset in range(MAX_SCAN_DAYS + 1):
        day = start + timedelta(days=offset)
        fixed = FIXED_FESTIVALS.get(day.strftime("%m-%d"))
        if fixed:
            return fixed, day
        lunar = _lunar_festival(day)
        if lunar:
            return lunar, day
    return None


def days_until(
    value: date | datetime | str | None = None,
) -> tuple[str, date, int] | None:
    """查询下一个节日以及还有几天。

    Args:
        value: 起始日期；None 表示今天。

    Returns:
        (节日名, 日期, 相隔天数)；找不到时返回 None。
    """
    start = _as_date(value)
    found = next_festival(start)
    if found is None:
        return None
    name, day = found
    return name, day, (day - start).days
