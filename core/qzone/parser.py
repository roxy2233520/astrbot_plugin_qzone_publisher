"""QQ空间响应解析工具。

QQ空间的接口返回并非常规 JSON：可能是 JSONP 包裹、包含 undefined 字面量、
单引号字符串或无引号键，失败时还可能直接回一整页 HTML（登录页 / 风控页）。
**回复接口成功时回的也是 HTML 框架页（不是 JSON）**，因此回复是否成功不能看
响应体，只能靠回查评论详情确认；这里提供的 ``find_own_reply`` 就是做这件事的。

解析按「逐级放宽」的顺序尝试，并在彻底失败时把响应片段写进日志，
同时区分「登录态失效 / 被风控」「验证页面」「返回的是页面」与「格式无法识别」，
便于回执给出可操作的建议。
"""

import json
import re
from typing import Any

from astrbot.api import logger

from .constants import (
    QZONE_CODE_LOGIN_REQUIRED,
    QZONE_CODE_UNEXPECTED_PAGE,
    QZONE_CODE_UNKNOWN,
    QZONE_CODE_VERIFY_PAGE,
    QZONE_MSG_EMPTY_RESPONSE,
    QZONE_MSG_LOGIN_REQUIRED,
    QZONE_MSG_NON_OBJECT_RESPONSE,
    QZONE_MSG_UNEXPECTED_PAGE,
    QZONE_MSG_UNKNOWN_FORMAT,
    QZONE_MSG_VERIFY_PAGE,
)
from .model import FeedComment, FeedPost

# 形如 _preloadCallback({...}); 的 JSONP 包裹（frameElement.callback(…) 同样命中）
_JSONP_PATTERN = re.compile(r"^[^(){]{0,64}\(\s*(\{.*\})\s*\)\s*;?\s*$", re.DOTALL)
# 无引号的键：{ key: 或 , key :
_UNQUOTED_KEY_PATTERN = re.compile(r"([{,]\s*)([A-Za-z_$][\w$]*)(\s*:)")
# 尾逗号：,] 或 ,}
_TRAILING_COMMA_PATTERN = re.compile(r",(\s*[}\]])")
# 判定「登录态真的失效了」的特征。实测表明：回复接口成功时回的是 HTML 框架页，
# 所以这里只认登录页本身的特征（ptlogin、请先登录…），不再用「登录」「login」这类
# 过于宽泛的字符串，避免把框架页 / 普通页面误判成登录失效而触发无用的重登重试。
_LOGIN_PAGE_HINTS = (
    "ptlogin",
    "请先登录",
    "请登录",
    "重新登录",
    "登录态已失效",
    "登录已失效",
)
# 验证 / 风控页面：也不是登录失效，重取登录态帮不上忙，单独分类
_VERIFY_PAGE_HINTS = (
    "安全验证",
    "验证码",
    "身份验证",
    "风控",
    "操作过于频繁",
    "操作频繁",
    "异常访问",
)
# 判定「返回的是页面而不是数据」的特征：JSONP / h5 框架页。
# 这些特征不会出现在登录页上，因此优先按「页面」处理，不触发重登。
_FRAMEWORK_PAGE_HINTS = (
    "frameelement.callback",
    "document.domain",
    "cb=",
    "<script",
)
# 普通的 HTML 页面（没有登录特征也没有框架特征）
_PAGE_HINTS = (
    "<html",
    "<!doctype",
    "<head",
    "<body",
    "forbidden",
    "blocked",
)
# 比对回复正文前去掉空白与标点：接口回显时可能多出空格或标点
_PUNCT_PATTERN = re.compile(r"[\s\W_]+")
# 前缀匹配允许的最短长度：太短容易把两条不同的回复当成同一条
_MIN_PREFIX = 4


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
    def error_payload(message: str, code: int = QZONE_CODE_UNKNOWN) -> dict[str, Any]:
        """构造带错误信息的统一响应体（供传输层改写失败原因使用）。

        Args:
            message: 给用户看的失败原因。
            code: 合成返回码。

        Returns:
            统一响应体。
        """
        return QzoneParser._error_payload(message, code)

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

    @staticmethod
    def is_framework_page(text: str) -> bool:
        """判断响应是否是 JSONP / h5 框架页（不是数据，也不是登录页）。

        Args:
            text: 原始响应文本。

        Returns:
            命中框架页特征时返回 True。
        """
        lowered = str(text or "").lower()
        return any(hint in lowered for hint in _FRAMEWORK_PAGE_HINTS)

    @staticmethod
    def is_page_response(text: str) -> bool:
        """判断响应是否「是页面而不是数据」（JSONP / h5 框架页 / HTML）。

        这类响应不该触发重新获取登录态：多半是接口地址或参数不对。

        Args:
            text: 原始响应文本。

        Returns:
            命中页面特征时返回 True。
        """
        lowered = str(text or "").lower()
        return any(hint in lowered for hint in _PAGE_HINTS)

    @staticmethod
    def is_verify_page(text: str) -> bool:
        """判断响应是否像验证 / 风控页面。

        这类页面同样不是登录失效：重取登录态解决不了，只会多打一次请求。

        Args:
            text: 原始响应文本。

        Returns:
            命中验证 / 风控特征时返回 True。
        """
        lowered = str(text or "").lower()
        return any(hint in lowered for hint in _VERIFY_PAGE_HINTS)

    @classmethod
    def parse_response(cls, text: str) -> dict[str, Any]:
        """把原始响应文本解析为字典。

        解析顺序（逐级放宽）：

        1. 直接 ``json.loads``；
        2. 剥离 JS 包裹（``_Callback(...)`` / ``frameElement.callback(...)``、多余分号与空白）；
        3. 放宽写法后再解析（单引号字符串、无引号键、尾逗号）；
        4. 仍失败时按原因分流：登录特征 → 登录态失效（可自动重登重试）；
           JSONP / 框架页 / HTML → 返回的是页面（不重登）；
           都不是 → 「响应格式无法识别」。三种都会把响应前 300 字符写进日志。

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

        判定顺序（顺序很重要）：

        1. 命中 JSONP / h5 框架页特征（``frameElement.callback``、``document.domain``…）→
           判定为「返回的是页面而不是数据」，**不**触发重登。
           注意：回复接口成功时回的也是这种框架页，因此这一条绝不能升级成登录失效；
        2. 命中登录页特征（``ptlogin`` / 请先登录…）→ 判定为登录态失效，
           传输层会重新获取 Cookie 并重试一次；
        3. 命中验证 / 风控页特征 → 单独分类，同样不重登；
        4. 其它 HTML 页面 → 同样按「返回的是页面」处理；
        5. 都不是 → 报「响应格式无法识别」。

        Args:
            raw: 原始响应文本。
            missing_fragment: 是否连 JSON 片段都没有找到。

        Returns:
            带 message 的错误响应体。
        """
        snippet = cls.visible_snippet(raw)
        if cls.is_framework_page(raw):
            logger.error(
                "QQ空间接口返回的是 JSONP / h5 框架页而不是数据（未重新获取登录态），"
                f"响应片段: {snippet}｜多为接口地址或参数不对，可先升级插件版本"
            )
            return cls._error_payload(
                QZONE_MSG_UNEXPECTED_PAGE, code=QZONE_CODE_UNEXPECTED_PAGE
            )
        if cls.is_login_page(raw):
            logger.error(
                "QQ空间返回的像是登录页（不是数据），响应片段: "
                f"{snippet}｜建议用 /空间重登 重取登录态或稍后重试"
            )
            return cls._error_payload(
                QZONE_MSG_LOGIN_REQUIRED, code=QZONE_CODE_LOGIN_REQUIRED
            )
        if cls.is_verify_page(raw):
            logger.error(
                "QQ空间返回的像是验证 / 风控页面（不是数据，未重新获取登录态），"
                f"响应片段: {snippet}｜建议稍后重试或降低请求频率"
            )
            return cls._error_payload(
                QZONE_MSG_VERIFY_PAGE, code=QZONE_CODE_VERIFY_PAGE
            )
        if cls.is_page_response(raw):
            logger.error(
                "QQ空间接口返回的是页面而不是数据（未重新获取登录态），响应片段: "
                f"{snippet}｜多为接口地址或参数不对，可先升级插件版本"
            )
            return cls._error_payload(
                QZONE_MSG_UNEXPECTED_PAGE, code=QZONE_CODE_UNEXPECTED_PAGE
            )
        reason = "缺少 JSON 片段" if missing_fragment else "JSON 解析失败"
        logger.error(f"QQ空间响应{reason}，格式无法识别，响应片段: {snippet}")
        return cls._error_payload(QZONE_MSG_UNKNOWN_FORMAT)

    @staticmethod
    def normalize_text(text: str) -> str:
        """去掉空白与标点后的正文，用于比对「这条回复是不是我发的」。

        Args:
            text: 原始正文。

        Returns:
            只保留字母、数字与汉字的文本（统一小写）。
        """
        return _PUNCT_PATTERN.sub("", str(text or "")).lower()

    @classmethod
    def reply_text_matches(cls, found: str, sent: str) -> bool:
        """判断回查到的回复正文是否就是本次发出的那条。

        先用去标点后完全相等判断；接口偶尔会截断正文，因此再放宽为前缀匹配
        （较短一方至少 ``_MIN_PREFIX`` 个字符，避免过短误判）。

        Args:
            found: 回查到的正文。
            sent: 本次发出的正文。

        Returns:
            认为是同一条时返回 True。
        """
        left = cls.normalize_text(found)
        right = cls.normalize_text(sent)
        if not left or not right:
            return False
        if left == right:
            return True
        shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
        return len(shorter) >= _MIN_PREFIX and longer.startswith(shorter)

    @classmethod
    def find_own_reply(
        cls,
        comments: list[FeedComment],
        comment_tid: str,
        own_uin: int,
        content: str,
    ) -> FeedComment | None:
        """在评论明细里查找「我自己刚发出的那条回复」。

        优先在目标评论的 ``list_3``（子回复）里找；如果评论 id 没能对上
        （列表接口与详情接口的评论 id 偶尔不同源），再退化为在所有评论的子回复里找。

        Args:
            comments: 回查拿到的评论明细。
            comment_tid: 被回复评论的 id。
            own_uin: 自己的 QQ 号。
            content: 本次发出的回复正文。

        Returns:
            命中的子回复；没找到时返回 None。
        """
        target = next(
            (item for item in comments if str(item.tid) == str(comment_tid)), None
        )
        scope = [target] if target is not None else comments
        for comment in scope:
            for sub in comment.replies:
                if sub.uin == own_uin and cls.reply_text_matches(sub.content, content):
                    return sub
        return None

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
