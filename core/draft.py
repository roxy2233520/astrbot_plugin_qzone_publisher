"""草稿确认。

开启 ``draft_enabled`` 后，定时发布不会直接生效；``draft_for_comment`` /
``draft_for_reply`` / ``draft_for_greet`` 决定评论、回复评论与问候是否也走确认。
内容先放进草稿箱并通知管理员，由 ``/空间确认``、``/空间放弃``、``/空间重写`` 处理；
``draft_timeout_minutes`` 大于 0 时，超时无人处理会自动放行。
草稿会持久化到磁盘，AstrBot 重启也不会丢。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from astrbot.api import logger

from .ui import ICON_INFO, Section, command_line, kv, pair


@dataclass(slots=True)
class Draft:
    """一条待确认的草稿。

    Attributes:
        kind: post（说说）/ comment（评论）/ reply（回复评论）/ greet（问候）。
        text: 待发布内容。
        source: 内容来源标识，如 llm / pool / file / interact / greet。
        created_time: 生成时间戳。
        target_uin: 评论目标作者 QQ 号（kind=comment 时为好友，
            kind=reply 时为该说说所在的自己账号）。
        target_tid: 评论目标说说 ID（kind=comment / reply 时有效）。
        target_name: 评论目标昵称，仅用于展示。
        target_text: 评论目标说说正文（kind=comment）/ 被回复的评论正文（kind=reply）。
        target_comment_tid: 被回复评论的 ID（kind=reply 时有效）。
        target_comment_uin: 被回复评论的作者 QQ 号（kind=reply 时有效）。
        target_post_text: 被回复评论所在说说的正文，用于重写时提供上下文。
        targets: 问候目标 QQ 号列表（kind=greet 时有效）。
        images: 附带图片数量（仅说说草稿有意义；图片本身不会持久化）。
    """

    kind: str
    text: str
    source: str = ""
    created_time: int = 0
    target_uin: int = 0
    target_tid: str = ""
    target_name: str = ""
    target_text: str = ""
    target_comment_tid: str = ""
    target_comment_uin: int = 0
    target_post_text: str = ""
    targets: list[str] = field(default_factory=list)
    images: int = 0

    @classmethod
    def from_dict(cls, data: dict) -> Draft:
        """由字典构造，忽略未知字段。"""
        raw_targets = data.get("targets")
        targets = (
            [str(item).strip() for item in raw_targets if str(item).strip()]
            if isinstance(raw_targets, list)
            else []
        )
        return cls(
            kind=str(data.get("kind") or "post"),
            text=str(data.get("text") or ""),
            source=str(data.get("source") or ""),
            created_time=int(data.get("created_time") or 0),
            target_uin=int(data.get("target_uin") or 0),
            target_tid=str(data.get("target_tid") or ""),
            target_name=str(data.get("target_name") or ""),
            target_text=str(data.get("target_text") or ""),
            target_comment_tid=str(data.get("target_comment_tid") or ""),
            target_comment_uin=int(data.get("target_comment_uin") or 0),
            target_post_text=str(data.get("target_post_text") or ""),
            targets=targets,
            images=int(data.get("images") or 0),
        )

    def title(self) -> str:
        """草稿类型的中文标题。"""
        if self.kind == "comment":
            who = self.target_name or (
                str(self.target_uin) if self.target_uin else "好友"
            )
            return f"评论草稿（给 {who}）"
        if self.kind == "reply":
            who = self.target_name or (
                str(self.target_comment_uin) if self.target_comment_uin else "评论者"
            )
            return f"回复草稿（回 {who} 的评论）"
        if self.kind == "greet":
            return f"问候草稿（{len(self.targets)} 人）"
        return "说说草稿"

    def _lines(self) -> list[str]:
        """草稿的键值行（纯文本版与 Markdown 版共用）。"""
        lines = [kv("类型", self.title())]
        if self.source:
            lines.append(kv("来源", self.source))
        if self.images:
            lines.append(kv("图片", f"{self.images} 张"))
        if self.kind == "comment" and self.target_tid:
            lines.append(kv("目标说说", self.target_tid))
        if self.kind == "reply":
            lines.append(
                kv(
                    "被回复的评论",
                    f"{self.target_name or self.target_comment_uin}"
                    f"（{self.target_text or '无正文'}）",
                )
            )
            if self.target_tid:
                lines.append(kv("所在说说", self.target_tid))
        if self.kind == "greet" and self.targets:
            lines.append(kv("发送对象", "、".join(self.targets)))
        return lines

    def describe_pair(self) -> tuple[str, str]:
        """给管理员看的完整描述：二元组 (纯文本版, Markdown 版)。

        草稿内容需要逐字确认，因此这里不套用 12 行上限；纯文本版直接发到 QQ，
        Markdown 版用于回执图（标签成为标题、键值名称为真加粗）。
        """
        section = Section(icon=ICON_INFO, label="草稿待确认")
        for line in self._lines():
            section.add(line)
        section.add(
            command_line("空间确认", "空间放弃"),
            command_line("空间重写"),
            kv("说明", "确认后才会真正发出；重写会再生成一版"),
        )
        text, markup = pair([section])
        body = f"内容：\n{self.text}"
        return f"{text}\n{body}", f"{markup}\n\n{body}"

    def describe(self) -> str:
        """给管理员看的完整描述（纯文本版）。"""
        return self.describe_pair()[0]


class DraftBox:
    """草稿箱：同一时刻只保留一条待确认草稿（新的会覆盖旧的）。"""

    def __init__(self, path: Path) -> None:
        """初始化草稿箱。

        Args:
            path: 持久化文件路径。
        """
        self.path = Path(path)
        self._pending: Draft | None = None
        self.load()

    @property
    def pending(self) -> Draft | None:
        """当前待确认的草稿。"""
        return self._pending

    def load(self) -> None:
        """从磁盘加载草稿。"""
        self._pending = None
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"草稿文件读取失败，已忽略: {e}")
            return
        if isinstance(raw, dict) and raw:
            self._pending = Draft.from_dict(raw)

    def save(self) -> None:
        """原子写入磁盘。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload = asdict(self._pending) if self._pending else {}
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except Exception as e:
            logger.error(f"草稿写入失败: {e}")

    def put(self, draft: Draft) -> Draft:
        """放入草稿（覆盖旧草稿）。

        Args:
            draft: 新草稿。

        Returns:
            实际存下的草稿（补全了创建时间）。
        """
        if not draft.created_time:
            draft.created_time = int(time.time())
        if self._pending is not None:
            logger.warning("已有待确认草稿，将被新草稿覆盖")
        self._pending = draft
        self.save()
        return draft

    def pop(self) -> Draft | None:
        """取出并清空草稿。

        Returns:
            被取出的草稿；没有则返回 None。
        """
        draft = self._pending
        self._pending = None
        self.save()
        return draft

    def clear(self) -> bool:
        """丢弃当前草稿。

        Returns:
            是否真的丢弃了草稿。
        """
        if self._pending is None:
            return False
        self._pending = None
        self.save()
        return True
