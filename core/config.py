"""插件配置包装层。

面板 schema（``_conf_schema.json``）现在是「板块」结构：顶层是若干
``{"type": "object", "description": "板块标题", "items": {...}}``，
面板会把每个板块渲染成一张带标题的卡片。

而插件代码始终按**扁平名字**读配置（``cfg.greet_users`` / ``cfg.set("draft_enabled", ...)``），
所以这里维护一份「配置项名 -> 板块内路径」的索引，把两种视图隔开：
改面板排版不需要动任何业务代码。

旧版本是扁平结构，升级时 AstrBot 会删掉 schema 里不存在的键并立刻存盘，
因此旧键在新 schema 里以「带永不成立 condition 的隐藏项」保留下来，
由 :meth:`PluginConfig.migrate_flat_config` 在插件启动时把值搬进板块并打上标记，
用户原有配置不会丢。搬运时还会处理一处语义变化：新版发布时间优先读
``publish_times``，所以旧版 ``publish_cron`` 会被继承成对应的时间点，
避免升级后发布时间被静默改成别的值。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.star.context import Context

_PLUGIN_ROOT = Path(__file__).resolve().parent.parent
_SCHEMA_FILE = _PLUGIN_ROOT / "_conf_schema.json"

_MISSING = object()

# 旧版扁平键的迁移标记（同样是隐藏项，不出现在面板里）
MIGRATION_FLAG = "_flat_keys_migrated"

# 名单类配置搬进「名单与权限」板块的标记（隐藏项）
AUDIENCE_MOVED_FLAG = "_audience_keys_moved"

# 从别的板块搬进「名单与权限」的键 -> 旧的板块路径。
# 旧位置在 schema 里保留同名隐藏副本（面板不显示），否则 AstrBot 会在
# 插件启动前就把用户填好的名单当成多余键删掉。
MOVED_SECTION_KEYS: dict[str, tuple[str, str]] = {
    "interact_uins": ("sec_interact", "interact_uins"),
    "interact_reply_uins": ("sec_interact", "interact_reply_uins"),
    "interact_reply_require_optin": ("sec_interact", "interact_reply_require_optin"),
    "greet_users": ("sec_private", "greet_users"),
    "active_msg_require_optin": ("sec_private", "active_msg_require_optin"),
    "admin_uins": ("sec_basic", "admin_uins"),
}


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


def _load_schema() -> dict[str, Any]:
    """读取面板 schema，失败时返回空字典。"""
    try:
        schema = json.loads(_SCHEMA_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"读取 _conf_schema.json 失败，将使用内置默认值: {e}")
        return {}
    return schema if isinstance(schema, dict) else {}


def _leaf_default(meta: dict[str, Any]) -> Any:
    """取一个配置项的默认值；object 类型用 items 里的默认值组装。"""
    if "default" in meta:
        return meta["default"]
    items = meta.get("items")
    if isinstance(items, dict):
        nested = {
            name: sub["default"]
            for name, sub in items.items()
            if isinstance(sub, dict) and "default" in sub
        }
        if nested:
            return nested
    return _MISSING


def _build_index(
    schema: dict[str, Any],
) -> tuple[dict[str, tuple[str, ...]], dict[str, Any], list[str]]:
    """建立「配置项名 -> 板块路径」「配置项名 -> 默认值」「旧扁平键」三份索引。

    Args:
        schema: 面板 schema。

    Returns:
        (paths, defaults, legacy_keys)。
    """
    paths: dict[str, tuple[str, ...]] = {}
    defaults: dict[str, Any] = {}
    legacy: list[str] = []

    for key, meta in schema.items():
        if not isinstance(meta, dict):
            continue
        # 隐藏项（旧的扁平键与内部标记）只用于迁移，不作为主索引
        if meta.get("condition"):
            if key not in (MIGRATION_FLAG, AUDIENCE_MOVED_FLAG):
                legacy.append(key)
            default = _leaf_default(meta)
            if default is not _MISSING:
                defaults.setdefault(key, default)
            continue

        if meta.get("type") == "object" and isinstance(meta.get("items"), dict):
            for name, sub in meta["items"].items():
                if not isinstance(sub, dict):
                    continue
                # 板块里的隐藏项（例如从别的板块搬走时留下的同名副本）只提供默认值，
                # 不占用配置路径——真正生效的是新板块里的那一份。
                if sub.get("condition"):
                    default = _leaf_default(sub)
                    if default is not _MISSING:
                        defaults.setdefault(name, default)
                    continue
                paths[name] = (key, name)
                default = _leaf_default(sub)
                if default is not _MISSING:
                    defaults[name] = default
            continue

        # 顶层直接放的可见项（兼容以后可能新增的散项）
        paths[key] = (key,)
        default = _leaf_default(meta)
        if default is not _MISSING:
            defaults[key] = default

    return paths, defaults, legacy


SCHEMA = _load_schema()
PATHS, DEFAULTS, LEGACY_KEYS = _build_index(SCHEMA)


def _get_path(config: Any, path: tuple[str, ...]) -> Any:
    """按路径读嵌套配置；任一层缺失时返回 ``_MISSING``。"""
    node: Any = config
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _set_path(config: Any, path: tuple[str, ...], value: Any) -> None:
    """按路径写嵌套配置，中间缺失的层级自动补空字典。

    Args:
        config: 配置字典。
        path: 层级路径。
        value: 要写入的值。
    """
    node = config
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = value


def _delete_path(config: Any, path: tuple[str, ...]) -> None:
    """按路径删掉一个配置项（层级不存在时什么都不做）。

    Args:
        config: 配置字典。
        path: 层级路径。
    """
    node: Any = config
    for part in path[:-1]:
        node = node.get(part) if isinstance(node, dict) else None
        if not isinstance(node, dict):
            return
    if isinstance(node, dict):
        node.pop(path[-1], None)


class PluginConfig:
    """插件配置对象。

    属性访问即读取配置（按板块路径读取，缺失时回退到 schema 默认值），
    通过 set() 写入并立即持久化到 AstrBot 配置。

    Attributes:
        raw: 原始 AstrBotConfig。
        context: AstrBot 插件上下文。
        data_dir: 插件数据目录。
        history_file: 发布历史文件路径。
        timezone: AstrBot 配置的时区，缺省为 Asia/Shanghai。
        migrated_keys: 本次启动从旧扁平结构搬到板块下的配置项名。
        moved_keys: 本次启动从别的板块搬进「名单与权限」的配置项名。
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
        self.migrated_keys: list[str] = self.migrate_flat_config()
        self.moved_keys: list[str] = self.migrate_moved_sections()

        tz = context.get_config().get("timezone")
        try:
            self.timezone = ZoneInfo(tz) if tz else ZoneInfo("Asia/Shanghai")
        except Exception:
            self.timezone = ZoneInfo("Asia/Shanghai")

    # ------------------------------------------------------------------
    # 旧扁平配置 -> 板块结构的迁移
    # ------------------------------------------------------------------

    def migrate_flat_config(self) -> list[str]:
        """把旧版扁平配置的值搬进新板块结构（同一次安装只做一次）。

        除了搬运，还会处理一处**语义变化**：新版自动发布时间优先读
        ``publish_times``（时间点列表），而旧版只有 ``publish_cron``。
        如果只搬运不改写，旧用户的时间会从 ``publish_cron`` 悄悄变成
        ``publish_times`` 的默认值——所以这里把「每天一次」的旧写法继承进来，
        复杂写法则留空 ``publish_times``，交给「为空时回退 ``publish_cron``」的既有逻辑。

        Returns:
            本次搬移的配置项名列表；无需迁移时为空。
        """
        raw = self.raw
        if not isinstance(raw, dict) or not LEGACY_KEYS:
            return []
        if raw.get(MIGRATION_FLAG):
            return []

        moved: list[str] = []
        for key in LEGACY_KEYS:
            if key not in raw:
                continue
            path = PATHS.get(key)
            if path is None:
                continue
            value = raw[key]
            if value is not None:
                _set_path(raw, path, value)
                moved.append(key)
            try:
                del raw[key]
            except Exception as e:  # pragma: no cover - 极端情况下不强求删除
                logger.debug(f"删除旧配置键 {key} 失败: {e}")

        self._inherit_publish_time(raw)

        raw[MIGRATION_FLAG] = True
        try:
            raw.save_config()
        except Exception as e:
            logger.warning(f"迁移后的配置保存失败: {e}")
        if moved:
            logger.info(f"已把 {len(moved)} 项旧配置迁移到新的板块结构（原值保持不变）")
        return moved

    # ------------------------------------------------------------------
    # 名单类配置搬板块（2.10.2）：把散在各板块的名单统一到「名单与权限」
    # ------------------------------------------------------------------

    def migrate_moved_sections(self) -> list[str]:
        """把已经搬到「名单与权限」板块的键，从旧板块位置搬过去（只做一次）。

        为什么需要这一步：AstrBot 加载插件配置时会按 schema 对齐用户配置
        （``check_config_integrity`` 会删掉 schema 里没有的键并立即存盘），
        旧位置若彻底从 schema 里消失，用户已经填好的名单会在插件启动前就被删掉。
        因此旧位置保留了一份同名隐藏副本（面板不显示），由这里把值搬到新板块，
        然后删掉旧位置的值；完成后打标记，避免下次启动把默认值又搬一遍。

        Returns:
            本次搬移的配置项名列表；无需迁移时为空。
        """
        raw = self.raw
        if not isinstance(raw, dict) or not MOVED_SECTION_KEYS:
            return []
        if raw.get(AUDIENCE_MOVED_FLAG):
            return []

        moved: list[str] = []
        for key, old_path in MOVED_SECTION_KEYS.items():
            new_path = PATHS.get(key)
            if new_path is None:
                continue
            value = _get_path(raw, old_path)
            if value is not _MISSING and value is not None:
                _set_path(raw, new_path, value)
                moved.append(key)
            _delete_path(raw, old_path)

        raw[AUDIENCE_MOVED_FLAG] = True
        try:
            raw.save_config()
        except Exception as e:
            logger.warning(f"搬移名单配置后的保存失败: {e}")
        if moved:
            logger.info(
                f"已把 {len(moved)} 项名单配置搬到「名单与权限」板块（原值保持不变）"
            )
        return moved

    @staticmethod
    def _simple_daily_time(spec: object) -> str | None:
        """把「每天一次」的简单时间写法转成 ``HH:MM``；复杂写法返回 None。

        Args:
            spec: 旧版 ``publish_cron`` 的值（``HH:MM`` 或 5 段 Cron）。

        Returns:
            形如 ``"00:30"`` 的规范写法；带星期/月份限定、多值或无法识别时返回 None。
        """
        text = str(spec or "").strip()
        if not text:
            return None

        match = re.fullmatch(r"(\d{1,2}):(\d{1,2})", text)
        if match:
            hour, minute = int(match.group(1)), int(match.group(2))
        else:
            fields = text.split()
            if len(fields) != 5:
                return None
            minute_text, hour_text, day, month, day_of_week = fields
            # 只有「每天、单值的小时与分钟」才敢转；其余（星期/月份限定、多值）
            # 一律不转换，避免改变用户的原意
            if day != "*" or month != "*" or day_of_week != "*":
                return None
            if not (minute_text.isdigit() and hour_text.isdigit()):
                return None
            hour, minute = int(hour_text), int(minute_text)

        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            return None
        return f"{hour:02d}:{minute:02d}"

    def _inherit_publish_time(self, raw: dict) -> str:
        """把旧版发布时间继承到 ``publish_times``（迁移时调用一次）。

        Args:
            raw: 已搬到板块结构上的配置字典。

        Returns:
            继承下来的 ``HH:MM``；未继承（无需或无法转换）时返回空串。
        """
        path_times = PATHS.get("publish_times")
        path_cron = PATHS.get("publish_cron")
        path_per_day = PATHS.get("publish_per_day")
        if not (path_times and path_cron):
            return ""

        existing = _get_path(raw, path_times)
        if isinstance(existing, list) and existing:
            # 用户已经自己填过时间点列表，保持不动
            return ""

        cron = str(_get_path(raw, path_cron) or "").strip()
        if not cron:
            return ""

        converted = self._simple_daily_time(cron)
        if not converted:
            logger.info(
                "旧发布时间形式较复杂（带星期/月份限定或多值），"
                f"保留 publish_cron={cron} 由它兜底，publish_times 保持为空"
            )
            return ""

        hour, minute = converted.split(":")
        _set_path(raw, path_times, [converted])
        if path_per_day:
            _set_path(raw, path_per_day, 1)
        # 顺手把兼容项也写成同一个时间的 Cron，避免两处写法不一致时误以为改错了
        _set_path(raw, path_cron, f"{int(minute)} {int(hour)} * * *")
        logger.info(
            f"旧发布时间已继承为 publish_times={converted}"
            f"（publish_cron 同步为 {int(minute)} {int(hour)} * * *，每天 1 条）"
        )
        return converted

    # ------------------------------------------------------------------
    # 读取与写入
    # ------------------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """按配置项名读取配置值。"""
        if name.startswith("_") or "raw" not in self.__dict__:
            raise AttributeError(name)

        path = PATHS.get(name)
        value = _get_path(self.raw, path) if path else _MISSING
        if value is _MISSING:
            # 兼容尚未迁移的旧扁平配置与测试桩
            value = self.raw.get(name, _MISSING)

        default = DEFAULTS.get(name, _MISSING)
        if value is _MISSING or value is None:
            if default is _MISSING:
                raise AttributeError(f"未定义的配置项: {name}")
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
        path = PATHS.get(key)
        if path:
            _set_path(self.raw, path, value)
        else:
            self.raw[key] = value
        self.raw.save_config()

    def mapping(self) -> dict[str, Any]:
        """返回当前配置的扁平浅拷贝，便于展示或调试。"""
        result: dict[str, Any] = {}
        for key in PATHS:
            try:
                result[key] = getattr(self, key)
            except AttributeError:  # pragma: no cover - 理论上不会发生
                continue
        return result

    @property
    def section_of(self) -> dict[str, str]:
        """配置项名 -> 所属板块标题，便于排查与展示。"""
        titles: dict[str, str] = {}
        for key, path in PATHS.items():
            meta = SCHEMA.get(path[0])
            if isinstance(meta, dict):
                titles[key] = str(meta.get("description") or path[0])
        return titles
