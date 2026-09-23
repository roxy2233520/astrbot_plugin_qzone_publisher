"""QQ空间响应解析工具。

QQ空间的接口返回并非常规 JSON：可能是 JSONP 包裹、包含 undefined 字面量，
因此这里做统一清洗后再解析。
"""

import json
import re
from typing import Any

from astrbot.api import logger

from .constants import (
    QZONE_CODE_UNKNOWN,
    QZONE_MSG_EMPTY_RESPONSE,
    QZONE_MSG_INVALID_RESPONSE,
    QZONE_MSG_JSON_PARSE_ERROR,
    QZONE_MSG_NON_OBJECT_RESPONSE,
)
from .model import FeedComment, FeedPost

# 形如 _preloadCallback({...}); 的 JSONP 包裹
_JSONP_PATTERN = re.compile(r"^[^(){]{0,64}\(\s*(\{.*\})\s*\)\s*;?\s*$", re.DOTALL)


class QzoneParser:
    """QQ空间接口响应解析器。"""

    @staticmethod
    def _error_payload(message: str) -> dict[str, Any]:
        """构造带有错误信息的统一响应体。"""
        return {"code": QZONE_CODE_UNKNOWN, "message": message, "data": {}}

    @staticmethod
    def parse_response(text: str) -> dict[str, Any]:
        """把原始响应文本解析为字典。

        Args:
            text: 接口返回的原始文本，可能是 JSON 或 JSONP。

        Returns:
            解析后的字典；解析失败时返回带 message 的错误字典。
        """
        if not text or not text.strip():
            return QzoneParser._error_payload(QZONE_MSG_EMPTY_RESPONSE)

        stripped = text.strip()
        match = _JSONP_PATTERN.match(stripped)
        if match:
            json_str = match.group(1)
        else:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start == -1 or end == -1 or end < start:
                logger.warning("QQ空间响应缺少 JSON 片段")
                return QzoneParser._error_payload(QZONE_MSG_INVALID_RESPONSE)
            json_str = stripped[start : end + 1]

        json_str = json_str.replace("undefined", "null").strip()

        try:
            data = json.loads(json_str)
        except ValueError as e:
            logger.error(f"QQ空间响应 JSON 解析失败: {e}")
            return QzoneParser._error_payload(QZONE_MSG_JSON_PARSE_ERROR)

        if not isinstance(data, dict):
            logger.error("QQ空间响应 JSON 根节点不是对象")
            return QzoneParser._error_payload(QZONE_MSG_NON_OBJECT_RESPONSE)

        return data

    @staticmethod
    def parse_upload_result(payload: dict[str, Any]) -> tuple[str, str]:
        """从图片上传结果中提取 pic_bo 与 richval。

        Args:
            payload: 上传接口的原始响应。

        Returns:
            二元组 (pic_bo, richval)，用于发表说说时携带图片。

        Raises:
            RuntimeError: 响应缺少必要字段时抛出。
        """
        data = payload.get("data")
        if not isinstance(data, dict):
            raise TypeError("图片上传响应缺少 data 字段")

        url = str(data.get("url") or "")
        # 正常返回形如 http://.../psb?/V1/xxx*abc!/b/AAA&bo=XXXX&rf=viewer_311
        match = re.search(r"[?&]bo=([^&]+)", url)
        if match:
            pic_bo = match.group(1)
        elif "&bo=" in url:
            pic_bo = url.split("&bo=", 1)[1]
        else:
            raise RuntimeError(f"图片上传响应缺少 bo 参数: {url[:120]}")
        richval = ",{},{},{},{},{},{},,{},{}".format(
            data.get("albumid", ""),
            data.get("lloc", ""),
            data.get("sloc", ""),
            data.get("type", ""),
            data.get("height", ""),
            data.get("width", ""),
            data.get("height", ""),
            data.get("width", ""),
        )
        return pic_bo, richval

    @staticmethod
    def parse_feeds(payload: dict[str, Any]) -> list[FeedPost]:
        """解析说说列表响应里的 msglist。

        Args:
            payload: 业务层响应字典（成功时含 msglist）。

        Returns:
            FeedPost 列表；没有可解析内容时返回空列表。
        """
        msglist = payload.get("msglist")
        if not isinstance(msglist, list):
            return []

        posts: list[FeedPost] = []
        for item in msglist:
            if not isinstance(item, dict):
                continue
            try:
                post = FeedPost.from_raw(item)
            except Exception as e:
                logger.warning(f"解析单条说说失败，已跳过: {e}")
                continue
            if post.tid:
                posts.append(post)
        return posts

    @staticmethod
    def parse_comments(payload: dict[str, Any]) -> list[FeedComment]:
        """解析说说详情响应里的 commentlist。

        Args:
            payload: 业务层响应字典（成功时含 commentlist）。

        Returns:
            FeedComment 列表；没有可解析内容时返回空列表。
        """
        return FeedComment.parse_many(payload.get("commentlist"))
