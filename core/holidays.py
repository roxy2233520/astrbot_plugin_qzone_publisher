"""中国传统节日的公历日期表与查询函数。

**数据来源（逐年核对）**：香港天文台「公曆與農曆日期對照表」
https://www.hko.gov.hk/tc/gts/time/calendar/text/files/T2026c.txt
（2027-2030 年同规律，文件名为 ``T{年}c.txt``）。

**核对方式**：该表按公历逐日列出农历日期，农历每月初一显示为月名（如「正月」）。
本表由一段本地核对脚本逐日解析五份年度对照表得到：取目标农历月日对应的公历日期；
除夕取春节前一天（农历腊月可能只有 29 天，除夕未必是「年三十」）。
核对脚本不随插件发布，解析逻辑见本文件开头的来源说明。

**覆盖范围**：除夕、春节、元宵、清明、端午、七夕、中秋、重阳、腊八、小年
（小年按北方习惯取腊月廿三），共 2026-2030 五年。

**超出范围时不会静默失效**：查询落在表外的年份会写 warning 日志，提醒更新本表。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from astrbot.api import logger

# 节日名 -> 该节日在表中的年份数（用于自检与文档）
FESTIVAL_NAMES: tuple[str, ...] = (
    "除夕",
    "春节",
    "元宵",
    "清明",
    "端午",
    "七夕",
    "中秋",
    "重阳",
    "腊八",
    "小年",
)

# 公历日期 -> 节日名；数据来源见模块开头说明
FESTIVALS: dict[str, str] = {
    # 2026 年
    "2026-01-26": "腊八",
    "2026-02-10": "小年",
    "2026-02-16": "除夕",
    "2026-02-17": "春节",
    "2026-03-03": "元宵",
    "2026-04-05": "清明",
    "2026-06-19": "端午",
    "2026-08-19": "七夕",
    "2026-09-25": "中秋",
    "2026-10-18": "重阳",
    # 2027 年
    "2027-01-15": "腊八",
    "2027-01-30": "小年",
    "2027-02-05": "除夕",
    "2027-02-06": "春节",
    "2027-02-20": "元宵",
    "2027-04-05": "清明",
    "2027-06-09": "端午",
    "2027-08-08": "七夕",
    "2027-09-15": "中秋",
    "2027-10-08": "重阳",
    # 2028 年
    "2028-01-04": "腊八",
    "2028-01-19": "小年",
    "2028-01-25": "除夕",
    "2028-01-26": "春节",
    "2028-02-09": "元宵",
    "2028-04-04": "清明",
    "2028-05-28": "端午",
    "2028-08-26": "七夕",
    "2028-10-03": "中秋",
    "2028-10-26": "重阳",
    # 2029 年
    "2029-01-22": "腊八",
    "2029-02-06": "小年",
    "2029-02-12": "除夕",
    "2029-02-13": "春节",
    "2029-02-27": "元宵",
    "2029-04-04": "清明",
    "2029-06-16": "端午",
    "2029-08-16": "七夕",
    "2029-09-22": "中秋",
    "2029-10-16": "重阳",
    # 2030 年
    "2030-01-11": "腊八",
    "2030-01-26": "小年",
    "2030-02-02": "除夕",
    "2030-02-03": "春节",
    "2030-02-17": "元宵",
    "2030-04-05": "清明",
    "2030-06-05": "端午",
    "2030-08-05": "七夕",
    "2030-09-12": "中秋",
    "2030-10-05": "重阳",
}

TABLE_START = date(2026, 1, 1)
TABLE_END = date(2030, 12, 31)

# 每个年份只提醒一次，避免日志被刷屏
_WARNED_YEARS: set[int] = set()


def table_range_text() -> str:
    """返回节日表覆盖范围的说明文本。"""
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


def _warn_out_of_range(day: date) -> None:
    """年份超出节日表范围时写一次 warning。"""
    if TABLE_START <= day <= TABLE_END or day.year in _WARNED_YEARS:
        return
    _WARNED_YEARS.add(day.year)
    logger.warning(
        f"节日表只覆盖 {table_range_text()}，{day.year} 年不在范围内："
        "节日祝福将不会触发，请更新 core/holidays.py（数据来源见该文件开头说明）"
    )


def as_date(value: date | datetime | str | None = None) -> date:
    """把入参统一成 date。

    Args:
        value: date / datetime / ISO 日期字符串；None 表示今天。

    Returns:
        对应的 date 对象。
    """
    return _as_date(value)


def festival_of(value: date | datetime | str | None = None) -> str | None:
    """查询某一天是不是内置的传统节日。

    Args:
        value: 目标日期；None 表示今天。

    Returns:
        节日名；不是节日或超出覆盖范围时返回 None。
    """
    day = _as_date(value)
    if not (TABLE_START <= day <= TABLE_END):
        _warn_out_of_range(day)
        return None
    return FESTIVALS.get(day.isoformat())


def next_festival(
    value: date | datetime | str | None = None,
) -> tuple[str, date] | None:
    """查询从某天（含当天）起的下一个节日。

    Args:
        value: 起始日期；None 表示今天。

    Returns:
        (节日名, 公历日期)；在覆盖范围内找不到时返回 None。
    """
    start = _as_date(value)
    if start > TABLE_END:
        _warn_out_of_range(start)
        return None
    if start < TABLE_START:
        start = TABLE_START
    for offset in range((TABLE_END - start).days + 1):
        day = start + timedelta(days=offset)
        name = FESTIVALS.get(day.isoformat())
        if name:
            return name, day
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
