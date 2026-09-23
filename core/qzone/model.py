"""QQ空间的请求上下文与统一响应模型。"""

import re
from dataclasses import dataclass, field
from typing import Any

from .constants import QZONE_CODE_OK, QZONE_CODE_UNKNOWN, QZONE_INTERNAL_META_KEY

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

# QQ空间正文里的内置表情标记，展示前需要剥掉
_EM_TAG = re.compile(r"\[em\].*?\[/em\]")


def strip_em_tags(text: str) -> str:
    """去掉正文里的 ``[em]e123[/em]`` 表情标记。"""
    return _EM_TAG.sub("", text or "").strip()


class QzoneContext:
    """一次 QQ空间登录所需的全部动态参数。

    Attributes:
        uin: 登录 QQ 号。
        skey: 普通 skey。
        p_skey: 空间域 p_skey，用于计算 g_tk。
    """

    def __init__(self, uin: int, skey: str, p_skey: str) -> None:
        self.uin = uin
        self.skey = skey
        self.p_skey = p_skey

    @property
    def gtk(self) -> str:
        """按 QQ 的 djb2 变体算法由 p_skey 计算 g_tk。"""
        hash_val = 5381
        for ch in self.p_skey:
            hash_val += (hash_val << 5) + ord(ch)
        return str(hash_val & 0x7FFFFFFF)

    def cookies(self) -> dict[str, str]:
        """返回请求所需的 Cookie。"""
        return {
            "uin": f"o{self.uin}",
            "skey": self.skey,
            "p_skey": self.p_skey,
        }

    def headers(self) -> dict[str, str]:
        """返回请求所需的默认请求头。"""
        return {
            "User-Agent": USER_AGENT,
            "referer": f"https://user.qzone.qq.com/{self.uin}",
            "origin": "https://user.qzone.qq.com",
            "Host": "user.qzone.qq.com",
            "Connection": "keep-alive",
        }


@dataclass(slots=True)
class ApiResponse:
    """统一的接口响应结果。

    Attributes:
        ok: 业务是否成功。
        code: 业务返回码。
        message: 失败原因，成功时为 None。
        data: 业务数据（成功时为去掉内部元信息的原始响应）。
        raw: 原始响应字典。
    """

    ok: bool
    code: int
    message: str | None
    data: dict[str, Any]
    raw: dict[str, Any]

    @classmethod
    def from_raw(
        cls,
        raw: dict[str, Any],
        *,
        code_key: str = "code",
        msg_key: str | tuple[str, ...] = ("message", "msg"),
        success_code: int = QZONE_CODE_OK,
    ) -> "ApiResponse":
        """由原始响应字典构造统一响应对象。

        Args:
            raw: 解析后的原始响应。
            code_key: 业务返回码所在字段名。
            msg_key: 错误消息字段名，可为候选元组。
            success_code: 视为成功的返回码。

        Returns:
            构造好的 ApiResponse。
        """
        code = raw.get(code_key, QZONE_CODE_UNKNOWN)
        if not isinstance(code, int):
            code = QZONE_CODE_UNKNOWN

        message: Any = None
        if isinstance(msg_key, tuple):
            for key in msg_key:
                if raw.get(key):
                    message = raw.get(key)
                    break
        else:
            message = raw.get(msg_key)

        if code == success_code:
            data = dict(raw)
            data.pop(QZONE_INTERNAL_META_KEY, None)
            return cls(ok=True, code=code, message=None, data=data, raw=raw)

        if not message:
            inner = raw.get("data")
            if isinstance(inner, dict):
                message = inner.get("message") or inner.get("msg")
        return cls(
            ok=False,
            code=code,
            message=str(message) if message else None,
            data={},
            raw=raw,
        )

    def get(self, key: str, default: Any = None) -> Any:
        """安全读取业务数据字段。"""
        if not self.ok or not self.data:
            return default
        return self.data.get(key, default)

    def __bool__(self) -> bool:
        return self.ok


def _as_int(value: Any) -> int:
    """尽最大努力把接口字段转成整数，失败返回 0。"""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


@dataclass(slots=True)
class FeedComment:
    """一条评论（来自 msglist / msgdetail 的 commentlist）。

    Attributes:
        uin: 评论者 QQ 号。
        tid: 评论 ID，回复时作为 commentId 使用。
        nickname: 评论者昵称。
        content: 评论正文（已剥离表情标记）。
        create_time: 评论时间戳。
        parent_tid: 被回复的评论 ID（子评论时存在）。
        replies: 该评论下的子回复（接口的 ``list_3``）。
    """

    uin: int
    tid: str
    nickname: str = ""
    content: str = ""
    create_time: int = 0
    parent_tid: str = ""
    replies: list["FeedComment"] = field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "FeedComment":
        """由接口返回的单条评论构造，字段缺失时留空而不报错。

        Args:
            raw: commentlist 里的一项。

        Returns:
            构造好的 FeedComment（含 ``list_3`` 里的子回复）。
        """
        tid = str(raw.get("tid") or raw.get("commentid") or "").strip()
        comment = cls(
            uin=_as_int(raw.get("uin")),
            tid=tid,
            nickname=str(raw.get("name") or raw.get("nickname") or "").strip(),
            content=strip_em_tags(str(raw.get("content") or "")),
            create_time=_as_int(raw.get("create_time") or raw.get("createTime")),
            parent_tid=str(raw.get("parent_tid") or "").strip(),
        )
        comment.replies = cls._parse_replies(raw.get("list_3"), parent_tid=tid)
        return comment

    @classmethod
    def _parse_replies(
        cls, items: object, *, parent_tid: str = ""
    ) -> list["FeedComment"]:
        """解析一条评论下的子回复（``list_3``）。

        子回复缺 id 时同样保留：判断「这条评论下是否已经有我的回复」只需要
        uin 与正文；真要回复它时再按评论 id 规则挡下。

        Args:
            items: 评论项里的 ``list_3``。
            parent_tid: 父评论的 tid，写进子回复的 ``parent_tid``。

        Returns:
            子回复列表；结构不对时返回空列表。
        """
        if not isinstance(items, list):
            return []
        replies: list[FeedComment] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            replies.append(
                cls(
                    uin=_as_int(item.get("uin")),
                    tid=str(item.get("tid") or item.get("commentid") or "").strip(),
                    nickname=str(
                        item.get("name") or item.get("nickname") or ""
                    ).strip(),
                    content=strip_em_tags(str(item.get("content") or "")),
                    create_time=_as_int(
                        item.get("create_time") or item.get("createTime")
                    ),
                    parent_tid=str(item.get("parent_tid") or "").strip() or parent_tid,
                )
            )
        return replies

    @staticmethod
    def parse_many(items: object) -> "list[FeedComment]":
        """解析一组评论，跳过结构不对或缺少评论 ID 的项。

        Args:
            items: 接口返回的 commentlist。

        Returns:
            FeedComment 列表。
        """
        if not isinstance(items, list):
            return []
        comments: list[FeedComment] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                comment = FeedComment.from_raw(item)
            except Exception:  # pragma: no cover - 字段异常时跳过该条
                continue
            if comment.tid:
                comments.append(comment)
        return comments

    def display_name(self) -> str:
        """展示用名称：昵称缺失时退回 QQ 号。"""
        if self.nickname:
            return self.nickname
        return str(self.uin) if self.uin else "（未知）"


@dataclass(slots=True)
class FeedPost:
    """一条说说（来自 emotion_cgi_msglist_v6 的 msglist）。

    Attributes:
        uin: 发布者 QQ 号。
        tid: 说说 ID。
        name: 发布者昵称。
        text: 正文（已剥离表情标记）。
        images: 图片地址列表。
        created_time: 发布时间戳。
        comment_count: 评论数（接口的 cmtnum，缺失时用评论明细条数）。
        comments: 评论明细；列表接口通常只带一部分，详情接口更完整。
        source_name: 来源设备/应用名。
    """

    uin: int
    tid: str
    name: str = ""
    text: str = ""
    images: list[str] = field(default_factory=list)
    created_time: int = 0
    comment_count: int = 0
    comments: list[FeedComment] = field(default_factory=list)
    source_name: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> "FeedPost":
        """由接口返回的单条 msg 构造。

        Args:
            raw: msglist 里的一项。

        Returns:
            构造好的 FeedPost。
        """
        images: list[str] = []
        for item in raw.get("pic") or []:
            if not isinstance(item, dict):
                continue
            for key in ("url2", "url3", "url1", "smallurl"):
                url = item.get(key)
                if url:
                    images.append(str(url))
                    break

        for video in raw.get("video") or []:
            if isinstance(video, dict):
                cover = video.get("url1") or video.get("pic_url")
                if cover:
                    images.append(str(cover))

        commentlist = raw.get("commentlist")
        comments = FeedComment.parse_many(commentlist)

        try:
            uin = int(raw.get("uin") or 0)
        except (TypeError, ValueError):
            uin = 0

        return cls(
            uin=uin,
            tid=str(raw.get("tid") or "").strip(),
            name=str(raw.get("name") or "").strip(),
            text=strip_em_tags(str(raw.get("content") or "")),
            images=images,
            created_time=int(raw.get("created_time") or 0),
            comment_count=max(_as_int(raw.get("cmtnum")), len(comments)),
            comments=comments,
            source_name=str(raw.get("source_name") or "").strip(),
        )
