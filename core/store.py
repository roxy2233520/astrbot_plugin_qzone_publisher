"""发布历史持久化。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from astrbot.api import logger


@dataclass(slots=True)
class PublishRecord:
    """一条发布记录。

    Attributes:
        time: 发布时间戳（秒）。
        text: 发布的正文。
        tid: QQ空间返回的说说 ID。
        uin: 发布者 QQ 号。
        source: 来源，manual=手动指令，pool/file/llm=自动发布的内容来源。
        images: 图片数量。
        ok: 是否发布成功。
        error: 失败原因。
    """

    time: int
    text: str
    tid: str = ""
    uin: int = 0
    source: str = "manual"
    images: int = 0
    ok: bool = True
    error: str = ""

    @classmethod
    def from_dict(cls, data: dict) -> PublishRecord:
        """由字典构造记录，忽略未知字段。"""
        return cls(
            time=int(data.get("time") or 0),
            text=str(data.get("text") or ""),
            tid=str(data.get("tid") or ""),
            uin=int(data.get("uin") or 0),
            source=str(data.get("source") or "manual"),
            images=int(data.get("images") or 0),
            ok=bool(data.get("ok", True)),
            error=str(data.get("error") or ""),
        )


class PublishStore:
    """发布历史存储，使用 JSON 原子写避免文件损坏。"""

    def __init__(self, path: Path, limit: int = 200) -> None:
        """初始化存储。

        Args:
            path: JSON 文件路径。
            limit: 最多保留的记录条数。
        """
        self.path = Path(path)
        self.limit = max(int(limit), 1)
        self._records: list[PublishRecord] = []
        self.load()

    def load(self) -> None:
        """从磁盘加载历史记录，文件缺失或损坏时置空。"""
        self._records = []
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"发布历史读取失败，已忽略: {e}")
            return
        if not isinstance(raw, list):
            return
        for item in raw:
            if isinstance(item, dict):
                self._records.append(PublishRecord.from_dict(item))

    def save(self) -> None:
        """原子写入磁盘。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload = [asdict(record) for record in self._records]
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception as e:
            logger.error(f"发布历史写入失败: {e}")

    def append(self, record: PublishRecord) -> None:
        """追加一条记录并按上限裁剪后落盘。"""
        self._records.append(record)
        if len(self._records) > self.limit:
            self._records = self._records[-self.limit :]
        self.save()

    def recent(self, count: int = 10) -> list[PublishRecord]:
        """返回最近 count 条记录，最新在前。"""
        if count <= 0:
            return []
        return list(reversed(self._records[-count:]))

    def last_success(self) -> PublishRecord | None:
        """返回最近一次成功发布的记录。"""
        for record in reversed(self._records):
            if record.ok:
                return record
        return None
