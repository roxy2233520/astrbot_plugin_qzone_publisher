"""QQ空间响应解析工具。

QQ空间的接口返回并非常规 JSON：可能是 JSONP 包裹、包含 undefined 字面量、
单引号字符串或无引号键，失败时还可能直接回一整页 HTML（登录页 / 风控页）。
因此这里按「逐级放宽」的顺序尝试解析，并在彻底失败时把响应片段写进日志，
同时区分「登录态失效 / 被风控」与「格式无法识别」两种原因，便于回执给出可操作的建议。
"""

import json
import re
from typing import Any

from astrbot.api import logger

from .constants import (
    QZONE_CODE_LOGIN_REQUIRED,
    QZONE_CODE_UNKNOWN,
    QZONE_MSG_EMPTY_RESPONSE,
    QZONE_MSG_LOGIN_REQUIRED,
    QZONE_MSG_NON_OBJECT_RESPONSE,
    QZONE_MSG_UNKNOWN_FORMAT,
)
from .model import FeedComment, FeedPost

# 形如 _preloadCallback({...}); 的 JSONP 包裹（frameElement.callback(…) 同样命中）
_JSONP_PATTERN = re.compile(r"^[^(){]{0,64}\(\s*(\{.*\})\s*\)\s*;?\s*$", re.DOTALL)
# 无引号的键：{ key: 或 , key :
_UNQUOTED_KEY_PATTERN = re.compile(r"([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)")
# 尾逗号：,] 或 ,}
_TRAILING_COMMA_PATTERN = re.compile(r",(\s*[}\]])")
# 判定「这不是数据，而是登录页 / 风控页」的特征
_LOGIN_PAGE_HINTS = (
    "<html",
    "<!doctype",
    "<head",
    "<body",
    "ptlogin",
    "请先登录",
    "请登录",
    "登录",
    "验证",
    "安全",
    "风控",
    "forbidden",
    "blocked",
)


class QzoneParser:
    """QQ空间接口响应解析器。"""

    @staticmethod
    def _error_payload(message: str, code: int = QZONE_CODE_UNKNOWN) -> dict[str, Any]:
        """构造带有错误信息的统一响应体。

        Args:
            message: 给用户看的失败原因。
            code: 合成返回码；判定为登录 / 风控时用 ``QZONE_CODE_LOGIN_REQUIRED``，
                传输层据此自动重取登录态重试一次。

        Returns:
            统一响应体。
        """
        return {"code": code, "message": message, "data": {}}

    @staticmethod
    def visible_snippet(text: str, limit: int = 300) -> str:
        """把响应片段处理成单行可读文本，便于写进日志。

        Args:
            text: 原始响应。
            limit: 最多保留的字符数。

        Returns:
            换行、回车与制表符可见化后的片段（形如 ``\\n``）。
        """
        snippet = str(text or "")[: max(int(limit), 0)]
        return (
            snippet.replace("\r\n", "\\n")
            .replace("\n", "\\n")
            .replace("\r", "\\n")
            .replace("\t", "\\t")
        )

    @staticmethod
    def _single_quotes_to_double(text: str) -> str:
        """把单引号字符串改写为双引号字符串（双引号串内的内容原样保留）。

        Args:
            text: 待处理的文本。

        Returns:
            改写后的文本；单引号未闭合时按原样收尾，不会抛异常。
        """
        out: list[str] = []
        index = 0
        length = len(text)
        in_double = False
        while index < length:
            char = text[index]
            if in_double:
                out.append(char)
                if char == "\\" and index + 1 < length:
                    out.append(text[index + 1])
                    index += 2
                    continue
                if char == '"':
                    in_double = False
                index += 1
                continue
            if char == '"':
                in_double = True
                out.append(char)
                index += 1
                continue
            if char == "'":
                index += 1
                buffer: list[str] = []
                while index < length:
                    current = text[index]
                    if current == "\\" and index + 1 < length:
                        following = text[index + 1]
                        if following in ("'", '"', "\\"):
                            buffer.append(following)
                        else:
                            buffer.append("\\" + following)
                        index += 2
                        continue
                    if current == "'":
                        index += 1
                        break
                    buffer.append('\\"' if current == '"' else current)
                    index += 1
                out.append('"' + "".join(buffer) + '"')
                continue
            out.append(char)
            index += 1
        return "".join(out)

    @classmethod
    def relax_json(cls, text: str) -> str:
        """把 JS 字面量写法放宽成严格 JSON。

        依次处理：单引号字符串、无引号的键、尾逗号。只在直接解析失败后使用，
        因此不会破坏本来就是合法 JSON 的响应。

        Args:
            text: 待处理的 JSON 片段。

        Returns:
            放宽后的文本。
        """
        relaxed = cls._single_quotes_to_double(text)
        relaxed = _UNQUOTED_KEY_PATTERN.sub(r'\1"\2"\3', relaxed)
        relaxed = _TRAILING_COMMA_PATTERN.sub(r"\1", relaxed)
        return relaxed

    @staticmethod
    def is_login_page(text: str) -> bool:
        """判断响应是否像登录页 / 风控页。

        Args:
            text: 原始响应文本。

        Returns:
            命中特征时返回 True。
        """
        lowered = str(text or "").lower()
        return any(hint in lowered for hint in _LOGIN_PAGE_HINTS)

    @classmethod
    def parse_response(cls, text: str) -> dict[str, Any]:
        """把原始响应文本解析为字典。

        解析顺序（逐级放宽）：

        1. 直接 ``json.loads``；
        2. 剥离 JS 包裹（``_Callback(...)`` / ``frameElement.callback(...)``、多余分号与空白）；
        3. 放宽写法后再解析（单引号字符串、无引号键、尾逗号）；
        4. 仍失败时：像登录页 / 风控页就返回可操作的登录态提示，
           否则返回「响应格式无法识别」；两种情况都会把响应前 300 字符写进日志。

        Args:
            text: 接口返回的原始文本，可能是 JSON 或 JSONP。

        Returns:
            解析后的字典；解析失败时返回带 message 的错误字典。
        """
        if not text or not text.strip():
            return cls._error_payload(QZONE_MSG_EMPTY_RESPONSE)

        stripped = text.strip()
        match = _JSONP_PATTERN.match(stripped)
        body = match.group(1) if match else ""
        if not body:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start == -1 or end == -1 or end < start:
                # 完全没有 JSON 片段：多半是 HTML 页面
                return cls._fail(stripped, missing_fragment=True)
            body = stripped[start : end + 1]

        for candidate in (body, cls.relax_json(body)):
            normalized = candidate.replace("undefined", "null").strip()
            if not normalized:
                continue
            try:
                data = json.loads(normalized)
            except ValueError:
                continue
            if isinstance(data, dict):
                return data
            logger.error(
                "QQ空间响应 JSON 根节点不是对象，响应片段: "
                f"{cls.visible_snippet(stripped)}"
            )
            return cls._error_payload(QZONE_MSG_NON_OBJECT_RESPONSE)

        return cls._fail(stripped)

    @classmethod
    def _fail(cls, raw: str, *, missing_fragment: bool = False) -> dict[str, Any]:
        """解析彻底失败时的统一处理：写日志并给出可操作的原因。

        Args:
            raw: 原始响应文本。
            missing_fragment: 是否连 JSON 片段都没有找到。

        Returns:
            带 message 的错误响应体。
        """
        snippet = cls.visible_snippet(raw)
        if cls.is_login_page(raw):
            logger.error(
                "QQ空间返回的像是登录页 / 风控页（不是数据），响应片段: "
                f"{snippet}｜建议用 /空间重登 重取登录态或稍后重试"
            )
            return cls._error_payload(
                QZONE_MSG_LOGIN_REQUIRED, code=QZONE_CODE_LOGIN_REQUIRED
            )
        reason = "缺少 JSON 片段" if missing_fragment else "JSON 解析失败"
        logger.error(f"QQ空间响应{reason}，格式无法识别，响应片段: {snippet}")
        return cls._error_payload(QZONE_MSG_UNKNOWN_FORMAT)

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
