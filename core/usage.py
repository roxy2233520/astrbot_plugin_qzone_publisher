"""Token 用量估算与统计。

插件不直接读 AstrBot 的计费数据（不同提供商口径不同），而是**按文本估算**并累计，
让用户对「这次生成大概要花多少 token」有直观感受：

- ``estimate_tokens``：粗略估算一段文本的 token 数；
- ``UsageStore``：按天、按功能累计，落盘 ``token_usage.json``，供 ``/空间状态``
  和 ``/空间用量`` 展示。

> 估算规则：CJK 字符约 0.7 token/字，其它字符约 1 token/4 字符，再加固定开销。
> 与真实计费存在 ±20% 左右误差，仅用于「心里有数」，不作为账单依据。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from astrbot.api import logger

# 中日韩文字（含假名、韩文音节）：这类字符每个约占 0.7 token
_CJK = re.compile(
    r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af]"
)
_CJK_PER_TOKEN = 0.7
_OTHER_CHARS_PER_TOKEN = 4.0
_OVERHEAD = 1


def estimate_tokens(text: str | None) -> int:
    """粗略估算文本的 token 数。

    Args:
        text: 任意文本。

    Returns:
        估算的 token 数（空文本返回 0）。
    """
    if not text:
        return 0
    cjk_count = len(_CJK.findall(text))
    other_count = max(len(text) - cjk_count, 0)
    value = cjk_count * _CJK_PER_TOKEN + other_count / _OTHER_CHARS_PER_TOKEN
    return max(round(value) + _OVERHEAD, 1)


def estimate_messages(
    system_prompt: str = "", prompt: str | None = None, contexts: list | None = None
) -> int:
    """估算一次请求的输入 token 数。

    Args:
        system_prompt: 系统提示词。
        prompt: 用户输入。
        contexts: 多轮上下文（取 content 字段拼接）。

    Returns:
        估算的输入 token 数。
    """
    parts = [system_prompt or ""]
    if prompt:
        parts.append(prompt)
    for item in contexts or []:
        if isinstance(item, dict) and item.get("content"):
            parts.append(str(item["content"]))
    return estimate_tokens("\n".join(parts))


def _empty_bucket() -> dict[str, Any]:
    """新建一个空白统计桶。"""
    return {"calls": 0, "prompt": 0, "completion": 0, "features": {}}


class UsageStore:
    """按天累计的 token 用量统计。"""

    def __init__(self, path: Path, keep_days: int = 60) -> None:
        """初始化统计存储。

        Args:
            path: JSON 文件路径。
            keep_days: 保留多少天的数据。
        """
        self.path = Path(path)
        self.keep_days = max(int(keep_days), 1)
        self._data: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        """从磁盘加载。"""
        self._data = {}
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Token 用量记录读取失败，已忽略: {e}")
            return
        if isinstance(raw, dict):
            self._data = {
                str(key): value for key, value in raw.items() if isinstance(value, dict)
            }

    def save(self) -> None:
        """原子写入，只保留最近 keep_days 天。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            keys = sorted(self._data)[-self.keep_days :]
            payload = {key: self._data[key] for key in keys}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except Exception as e:
            logger.error(f"Token 用量记录写入失败: {e}")

    @staticmethod
    def _today() -> str:
        """今天的日期串。"""
        return datetime.now().strftime("%Y-%m-%d")

    def record(self, feature: str, prompt_tokens: int, completion_tokens: int) -> None:
        """记录一次调用。

        Args:
            feature: 功能名（说说 / 日程 / 评论 / 问候 / 搜索词…）。
            prompt_tokens: 输入估算。
            completion_tokens: 输出估算。
        """
        today = self._today()
        bucket = self._data.setdefault(today, _empty_bucket())
        bucket["calls"] = int(bucket.get("calls", 0)) + 1
        bucket["prompt"] = int(bucket.get("prompt", 0)) + max(prompt_tokens, 0)
        bucket["completion"] = int(bucket.get("completion", 0)) + max(
            completion_tokens, 0
        )

        features = bucket.setdefault("features", {})
        item = features.setdefault(feature, {"calls": 0, "prompt": 0, "completion": 0})
        item["calls"] += 1
        item["prompt"] += max(prompt_tokens, 0)
        item["completion"] += max(completion_tokens, 0)

        self.save()

    def summary(self, days: int = 1) -> dict[str, Any]:
        """汇总最近若干天的用量。

        Args:
            days: 统计最近多少天（1 表示只看今天）。

        Returns:
            含 calls / prompt / completion / total / by_feature 的字典。
        """
        span = max(int(days), 1)
        keys = {
            (datetime.now() - timedelta(days=offset)).strftime("%Y-%m-%d")
            for offset in range(span)
        }

        result: dict[str, Any] = {
            "days": span,
            "calls": 0,
            "prompt": 0,
            "completion": 0,
            "total": 0,
            "by_feature": {},
        }
        for key in sorted(keys & set(self._data)):
            bucket = self._data[key]
            result["calls"] += int(bucket.get("calls", 0))
            result["prompt"] += int(bucket.get("prompt", 0))
            result["completion"] += int(bucket.get("completion", 0))
            for feature, item in (bucket.get("features") or {}).items():
                target = result["by_feature"].setdefault(
                    feature, {"calls": 0, "prompt": 0, "completion": 0}
                )
                target["calls"] += int(item.get("calls", 0))
                target["prompt"] += int(item.get("prompt", 0))
                target["completion"] += int(item.get("completion", 0))

        result["total"] = result["prompt"] + result["completion"]
        return result

    def format_summary(self, days: int = 1, *, indent: str = "") -> str:
        """把汇总格式化成可读文本。

        Args:
            days: 统计天数。
            indent: 每行前缀（用于状态里的缩进）。

        Returns:
            多行文本。
        """
        data = self.summary(days)
        title = "今日" if days <= 1 else f"最近 {days} 天"
        if not data["calls"]:
            return f"{title}还没有 AI 调用记录"

        lines = [
            f"{title} {data['calls']} 次调用，约 输入 {data['prompt']} + 输出 "
            f"{data['completion']} = {data['total']} tokens（估算）"
        ]
        for feature, item in sorted(
            data["by_feature"].items(), key=lambda kv: -kv[1]["prompt"]
        ):
            lines.append(
                f"{indent}· {feature}: {item['calls']} 次，约 "
                f"{item['prompt'] + item['completion']} tokens"
            )
        return "\n".join(lines)
