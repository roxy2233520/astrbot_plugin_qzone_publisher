"""私聊用户偏好：记录谁接受机器人的主动消息、接受哪些功能。

存储 ``<插件数据目录>/user_prefs.json``，原子写入，结构：

.. code-block:: json

    {
      "10001": {
        "first_seen": 1790000000,
        "asked_at": 1790000050,
        "opted_in": true,
        "features": {"morning": true, "night": true, "holiday": true},
        "updated_at": 1790000050
      }
    }

语义约定：

- ``opted_in`` 为 ``True`` 表示明确接受；``False`` 表示明确拒绝；
  ``None`` 表示尚未回答——与 ``False`` 一样按「不接受」处理（保守默认：
  没得到同意就不主动打扰）。
- ``features`` 中缺失的功能项按 ``True`` 处理（仅在已接受的前提下生效）。
- ``asked_at`` 记录「已经问过是否接受」的时间；为空表示还没问过，
  首次引导据此判断，保证同一个人只引导一次。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

from .config import PluginConfig

# 需要用户逐项同意的主动消息功能
FEATURES: tuple[str, ...] = ("morning", "night", "holiday")

# 功能名 -> 展示名
FEATURE_LABELS: dict[str, str] = {
    "morning": "早安",
    "night": "晚安",
    "holiday": "节日祝福",
}


@dataclass(slots=True)
class UserPref:
    """一个私聊用户的偏好。

    Attributes:
        qq: 用户 QQ 号。
        first_seen: 第一次见到该用户的时间戳。
        asked_at: 询问过是否接受主动消息的时间戳；0 表示还没问过。
        opted_in: 是否接受主动消息；None 表示尚未回答。
        features: 各主动消息功能的开关。
        updated_at: 最近一次修改的时间戳。
    """

    qq: str
    first_seen: int = 0
    asked_at: int = 0
    opted_in: bool | None = None
    features: dict[str, bool] = field(default_factory=dict)
    updated_at: int = 0

    def feature_enabled(self, feature: str) -> bool:
        """该功能是否开启（缺项按开启处理）。"""
        return bool(self.features.get(feature, True))

    def to_dict(self) -> dict[str, object]:
        """转成落盘用的字典。"""
        return {
            "first_seen": int(self.first_seen),
            "asked_at": int(self.asked_at),
            "opted_in": self.opted_in,
            "features": {name: self.feature_enabled(name) for name in FEATURES},
            "updated_at": int(self.updated_at),
        }


class UserPrefStore:
    """用户偏好的读写与判定。"""

    def __init__(self, config: PluginConfig, path: Path | None = None) -> None:
        """初始化存储。

        Args:
            config: 插件配置（用于取数据目录）。
            path: 覆盖存储路径，便于测试。
        """
        self.cfg = config
        self.file = path or (Path(config.data_dir) / "user_prefs.json")
        self._users: dict[str, UserPref] = {}
        self.load()

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------

    def load(self) -> None:
        """从磁盘读取偏好记录。"""
        self._users = {}
        if not self.file.exists():
            return
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"用户偏好记录读取失败，已忽略: {e}")
            return
        if not isinstance(raw, dict):
            return
        for qq, item in raw.items():
            if not isinstance(item, dict):
                continue
            features = item.get("features")
            opted_in = item.get("opted_in")
            self._users[str(qq)] = UserPref(
                qq=str(qq),
                first_seen=int(item.get("first_seen") or 0),
                asked_at=int(item.get("asked_at") or 0),
                opted_in=bool(opted_in) if opted_in is not None else None,
                features={
                    str(name): bool(value)
                    for name, value in (features or {}).items()
                    if isinstance(features, dict)
                },
                updated_at=int(item.get("updated_at") or 0),
            )

    def save(self) -> None:
        """原子写入偏好记录。"""
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            payload = {qq: user.to_dict() for qq, user in sorted(self._users.items())}
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.file)
        except Exception as e:
            logger.error(f"用户偏好记录写入失败: {e}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def get(self, qq: str) -> UserPref | None:
        """取某个用户的偏好；没有记录时返回 None。"""
        return self._users.get(str(qq).strip())

    def needs_guidance(self, qq: str) -> bool:
        """该用户是否还需要一次「是否接受主动消息」的引导。"""
        user = self.get(qq)
        return user is None or not user.asked_at

    def allowed(self, qq: str, feature: str) -> bool:
        """该用户是否允许接收某个功能的主动消息。

        Args:
            qq: 用户 QQ 号。
            feature: 功能名（morning / night / holiday）。

        Returns:
            允许时返回 True；未接受或该功能被关闭时返回 False。
        """
        user = self.get(qq)
        if user is None or user.opted_in is not True:
            return False
        return user.feature_enabled(str(feature))

    def stats(self) -> dict[str, int]:
        """统计接受 / 拒绝 / 未回答的人数。"""
        accepted = declined = unanswered = 0
        for user in self._users.values():
            if user.opted_in is True:
                accepted += 1
            elif user.opted_in is False:
                declined += 1
            else:
                unanswered += 1
        return {
            "accepted": accepted,
            "declined": declined,
            "unanswered": unanswered,
            "total": len(self._users),
        }

    # ------------------------------------------------------------------
    # 修改
    # ------------------------------------------------------------------

    def _ensure(self, qq: str) -> UserPref:
        """取出或新建一个用户记录。"""
        key = str(qq).strip()
        user = self._users.get(key)
        if user is None:
            now = int(time.time())
            user = UserPref(
                qq=key,
                first_seen=now,
                features={name: True for name in FEATURES},
            )
            self._users[key] = user
        return user

    def mark_seen(self, qq: str) -> bool:
        """记录「见过这个用户」。

        Args:
            qq: 用户 QQ 号。

        Returns:
            True 表示这是第一次见到该用户。
        """
        key = str(qq).strip()
        first = key not in self._users
        user = self._ensure(key)
        if first:
            user.updated_at = int(time.time())
            self.save()
        return first

    def mark_asked(self, qq: str) -> None:
        """记录「已经问过是否接受主动消息」。"""
        user = self._ensure(qq)
        user.asked_at = int(time.time())
        user.updated_at = user.asked_at
        self.save()

    def set_opted_in(self, qq: str, value: bool) -> UserPref:
        """设置是否接受主动消息。

        Args:
            qq: 用户 QQ 号。
            value: True 表示接受，False 表示拒绝。

        Returns:
            更新后的用户偏好。
        """
        user = self._ensure(qq)
        now = int(time.time())
        user.opted_in = bool(value)
        user.updated_at = now
        if not user.asked_at:
            # 用户主动表过态，等价于已经问过，不再发引导
            user.asked_at = now
        if not user.first_seen:
            user.first_seen = now
        self.save()
        return user

    def set_feature(self, qq: str, feature: str, value: bool) -> UserPref:
        """设置某个功能的开关。

        Args:
            qq: 用户 QQ 号。
            feature: 功能名。
            value: True 开启，False 关闭。

        Returns:
            更新后的用户偏好。
        """
        user = self._ensure(qq)
        user.features[str(feature)] = bool(value)
        user.updated_at = int(time.time())
        if user.opted_in is None:
            # 单独打开某个功能视为对该功能接受，但整体仍按未回答记录
            pass
        self.save()
        return user

    def set_all_features(self, qq: str, value: bool) -> UserPref:
        """把全部功能开关设为同一值。"""
        user = self._ensure(qq)
        for name in FEATURES:
            user.features[name] = bool(value)
        user.updated_at = int(time.time())
        self.save()
        return user

    # ------------------------------------------------------------------
    # 展示
    # ------------------------------------------------------------------

    @staticmethod
    def state_text(user: UserPref | None) -> str:
        """把人话状态写出来：已接受 / 已拒绝 / 未回答。"""
        if user is None or user.opted_in is None:
            return "未回答"
        return "已接受" if user.opted_in else "已拒绝"

    @staticmethod
    def features_text(user: UserPref | None) -> str:
        """把各功能开关写成一行。"""
        return "｜".join(
            f"{FEATURE_LABELS[name]} {'开' if user is None or user.feature_enabled(name) else '关'}"
            for name in FEATURES
        )
