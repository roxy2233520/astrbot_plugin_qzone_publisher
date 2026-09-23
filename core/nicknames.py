"""QQ 昵称获取与清洗：给「逐人个性化问候」提供可用称呼。

来源与优先级：

1. OneBot 的 ``get_friend_list``（一次取回全部好友昵称，结果缓存 6 小时并落盘
   ``<插件数据目录>/nicknames.json``）；
2. 缓存里没有这个 QQ 时，对该 QQ 调一次 ``get_stranger_info``；
3. 都取不到就不用昵称（称呼退化为「你」），整个过程**不阻塞发送**、失败只记日志。

清洗规则（措辞安全）：昵称只允许作为称呼或温和的观察出现，因此下面这些昵称一律不使用——
含广告 / 联系方式 / 敏感词、纯数字、纯符号、乱码比例过高、超长、含控制字符的昵称。
插件的提示词里同时写明：不得基于昵称联想调侃，不得提及外貌、性别、年龄、职业、地域、
健康状况、经济情况与感情状态，也不得拿昵称里的自嘲、梗、数字或符号开玩笑。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger

# 昵称缓存有效期（秒）
DEFAULT_TTL = 6 * 3600

# 昵称长度上限（超过就不当名字用）
MAX_NAME_LENGTH = 16

# 命中即判定不可用的词：广告、联系方式、敏感内容
_BLOCKED_WORDS = (
    "http",
    "www.",
    ".com",
    ".cn",
    "微信",
    "vx",
    "v信",
    "企鹅号",
    "加我",
    "私聊",
    "代购",
    "刷单",
    "兼职",
    "日结",
    "贷款",
    "博彩",
    "赌博",
    "色情",
    "约炮",
    "包养",
    "成人",
    "出售",
    "低价",
    "推广",
    "广告",
    "免费领",
    "扫码",
)

# 连续 6 位以上数字（QQ 号、电话、广告编号）不算名字
_LONG_DIGITS = re.compile(r"\d{6,}")
# 控制字符
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# 只由符号/空白组成的昵称
_ONLY_SYMBOLS = re.compile(r"^[\W_]+$", re.UNICODE)


def nickname_is_usable(name: str) -> bool:
    """判断昵称能否作为称呼使用。

    Args:
        name: 原始昵称。

    Returns:
        可用时返回 True；广告、联系方式、敏感词、纯数字、纯符号、乱码比例过高或
        超长的昵称一律返回 False（调用方会退化为「你」）。
    """
    text = str(name or "").strip()
    if not text or len(text) > MAX_NAME_LENGTH:
        return False
    if _CONTROL_CHARS.search(text):
        return False
    if _LONG_DIGITS.search(text):
        return False
    if _ONLY_SYMBOLS.match(text):
        return False

    lowered = text.lower()
    if any(word in lowered for word in _BLOCKED_WORDS):
        return False

    # 乱码判定：既不是中文/字母/数字，也不是常见标点与符号的比例过高
    unusual = 0
    for char in text:
        code = ord(char)
        if (
            "\u4e00" <= char <= "\u9fff"  # 汉字
            or char.isalnum()
            or char in " _-·.。!！?？~～^&*()（）[]【】<>《》+"
        ):
            continue
        if 0x2600 <= code <= 0x27BF or code >= 0x1F000:  # emoji
            unusual += 1
            continue
        unusual += 1
    return unusual * 2 <= len(text)


class NicknameBook:
    """好友昵称缓存（落盘 + 过期刷新）。"""

    def __init__(self, path: Path | None, ttl: int = DEFAULT_TTL) -> None:
        """初始化。

        Args:
            path: 缓存文件路径；为 None 时只存在内存里（测试或无数据目录时）。
            ttl: 缓存有效期（秒）。
        """
        self.path = Path(path) if path else None
        self.ttl = max(int(ttl), 0)
        self.fetched_at: float = 0.0
        self._names: dict[str, str] = {}
        self.load()

    # ------------------------------------------------------------------
    # 落盘
    # ------------------------------------------------------------------

    def load(self) -> None:
        """从磁盘读取缓存。"""
        self._names = {}
        self.fetched_at = 0.0
        if self.path is None or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"昵称缓存读取失败，已忽略: {e}")
            return
        if not isinstance(raw, dict):
            return
        self.fetched_at = float(raw.get("fetched_at") or 0)
        names = raw.get("names")
        if isinstance(names, dict):
            self._names = {
                str(key): str(value)
                for key, value in names.items()
                if str(key).strip() and str(value).strip()
            }

    def save(self) -> None:
        """原子写入缓存。"""
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"fetched_at": self.fetched_at, "names": self._names}
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except Exception as e:
            logger.error(f"昵称缓存写入失败: {e}")

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def expired(self) -> bool:
        """缓存是否已过期（没取过也算过期）。"""
        if self.fetched_at <= 0:
            return True
        return (time.time() - self.fetched_at) >= self.ttl

    def cached(self, qq: str) -> str:
        """取缓存里的原始昵称（未做可用性判断）。"""
        return self._names.get(str(qq).strip(), "")

    def usable_name(self, qq: str) -> str:
        """取可用于称呼的昵称；不可用时返回空串。"""
        name = self.cached(qq)
        return name if nickname_is_usable(name) else ""

    def remember(self, qq: str, name: str) -> None:
        """把一个昵称写进缓存（不落盘，由调用方决定何时保存）。"""
        key = str(qq).strip()
        text = str(name or "").strip()
        if key and text:
            self._names[key] = text

    # ------------------------------------------------------------------
    # 从 OneBot 获取
    # ------------------------------------------------------------------

    async def refresh(self, client: Any) -> int:
        """调用 ``get_friend_list`` 拉取全部好友昵称。

        Args:
            client: OneBot 客户端。

        Returns:
            本次更新到的昵称条数；失败返回 0（只记日志，不抛异常）。
        """
        if client is None:
            return 0
        try:
            friends = await client.get_friend_list()
        except Exception as e:
            logger.debug(f"获取好友列表失败，本轮不使用昵称: {e}")
            return 0

        if not isinstance(friends, list):
            return 0

        updated = 0
        for item in friends:
            if not isinstance(item, dict):
                continue
            qq = str(item.get("user_id") or item.get("uin") or "").strip()
            name = str(item.get("nickname") or item.get("nick") or "").strip()
            if qq and name:
                self._names[qq] = name
                updated += 1
        self.fetched_at = time.time()
        self.save()
        logger.info(
            f"已更新好友昵称缓存：{updated} 条（有效期 {self.ttl // 3600} 小时）"
        )
        return updated

    async def name_of(self, qq: str, client: Any) -> str:
        """取某个 QQ 可用作称呼的昵称。

        Args:
            qq: 目标 QQ 号。
            client: OneBot 客户端，可为 None。

        Returns:
            可用昵称；取不到或不可用时返回空串（调用方退化为「你」）。
        """
        key = str(qq).strip()
        if not key:
            return ""

        if self.expired and client is not None:
            await self.refresh(client)

        name = self.usable_name(key)
        if name:
            return name

        # 缓存里没有：对这个 QQ 单独查一次
        if client is None:
            return ""
        try:
            info = await client.get_stranger_info(user_id=int(key))
        except Exception as e:
            logger.debug(f"获取 {key} 的昵称失败，本次不使用昵称: {e}")
            return ""
        if isinstance(info, dict):
            self.remember(key, str(info.get("nickname") or ""))
            self.save()
        return self.usable_name(key)
