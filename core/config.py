"""插件配置包装层。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api.star import StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_FILE = _PLUGIN_ROOT / "_conf_schema.json"

_MISSING = object()


def _resolve_plugin_name() -> str:
    """推导插件名，用作数据目录名。

    优先读 metadata.yaml 的 name（AstrBot 安装时也用这个值做目录名），
    读不到时退回当前目录名。这样重命名仓库/插件后不会写错数据目录。

    Returns:
        插件名。
    """
    metadata = _PLUGIN_ROOT / "metadata.yaml"
    try:
        for line in metadata.read_text(encoding="utf-8").splitlines():
            if line.startswith("name:"):
                value = line.split(":", 1)[1].strip().strip("'\"")
                if value:
                    return value
    except Exception:
        pass
    return _PLUGIN_ROOT.name


PLUGIN_NAME = _resolve_plugin_name()


def _load_defaults() -> dict[str, Any]:
    """从 _conf_schema.json 读取默认值。

    以面板配置 schema 作为默认值的唯一来源，避免两处默认值不一致。
    对 object 类型（如 life_pool）没有顶层 default 时，把 items 里各项的
    default 组装成嵌套字典。

    Returns:
        配置项到默认值的映射，读取失败时返回空字典。
    """
    try:
        schema = json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(schema, dict):
        return {}

    defaults: dict[str, Any] = {}
    for key, meta in schema.items():
        if not isinstance(meta, dict):
            continue
        if "default" in meta:
            defaults[key] = meta["default"]
            continue
        items = meta.get("items")
        if isinstance(items, dict):
            nested = {
                name: sub["default"]
                for name, sub in items.items()
                if isinstance(sub, dict) and "default" in sub
            }
            if nested:
                defaults[key] = nested
    return defaults


DEFAULTS = _load_defaults()


class PluginConfig:
    """插件配置对象。

    属性访问即读取配置（缺失时回退到 schema 默认值），
    通过 set() 写入并立即持久化到 AstrBot 配置。

    Attributes:
        raw: 原始 AstrBotConfig。
        context: AstrBot 插件上下文。
        data_dir: 插件数据目录。
        history_file: 发布历史文件路径。
        timezone: AstrBot 配置的时区，缺省为 Asia/Shanghai。
    """

    def __init__(self, raw: AstrBotConfig, context: Context) -> None:
        """初始化配置对象。

        Args:
            raw: AstrBot 注入的配置对象。
            context: AstrBot 插件上下文。
        """
        self.raw = raw
        self.context = context
        self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        self.history_file = self.data_dir / "publish_history.json"
        self.draft_file = self.data_dir / "draft.json"

        tz = context.get_config().get("timezone")
        try:
            self.timezone = ZoneInfo(tz) if tz else ZoneInfo("Asia/Shanghai")
        except Exception:
            self.timezone = ZoneInfo("Asia/Shanghai")

    def __getattr__(self, name: str) -> Any:
        """按配置项名读取配置值。"""
        if name.startswith("_") or "raw" not in self.__dict__:
            raise AttributeError(name)

        default = DEFAULTS.get(name, _MISSING)
        if default is _MISSING:
            raise AttributeError(f"未定义的配置项: {name}")

        value = self.raw.get(name, default)
        if value is None:
            return default
        # 嵌套配置（如 life_pool）：面板里只改了部分子项时，用默认值补齐其余子项
        if isinstance(default, dict) and isinstance(value, dict):
            merged = dict(default)
            merged.update(value)
            return merged
        return value

    def set(self, key: str, value: Any) -> None:
        """写入配置项并立即保存到磁盘。

        Args:
            key: 配置项名。
            value: 新的配置值。
        """
        self.raw[key] = value
        self.raw.save_config()

    def mapping(self) -> dict[str, Any]:
        """返回当前配置的浅拷贝，便于展示或调试。"""
        return {key: self.raw.get(key, default) for key, default in DEFAULTS.items()}
