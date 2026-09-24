"""QQ空间业务接口封装。

接口地址与表单参数来自 QQ 空间网页端协议，协议细节参考了
`Zhalslar/astrbot_plugin_qzone`（GPL-3.0）等项目，详见仓库根目录的
`THIRD_PARTY_NOTICES.md`。
"""

import base64
import time
from collections.abc import Sequence
from typing import Any

from astrbot.api import logger

from .client import QzoneHttpClient
from .constants import (
    QZONE_CODE_FORBIDDEN,
    QZONE_CODE_IMAGE_EXPIRED,
    QZONE_CODE_REPLY_UNCONFIRMED,
    QZONE_CODE_UNEXPECTED_PAGE,
    QZONE_CODE_UNKNOWN,
    QZONE_INTERNAL_META_KEY,
    QZONE_MSG_FORBIDDEN,
    QZONE_MSG_REPLY_UNCONFIRMED,
)
from .model import USER_AGENT, ApiResponse
from .parser import QzoneParser

# 回查时判定「本次新增」的时间余量（秒）：空间的时间戳精度与服务端延迟都留一点余地
_CONFIRM_SLACK = 120


class QzoneAPI(QzoneHttpClient):
    """QQ空间接口集合。"""

    BASE_URL = "https://user.qzone.qq.com"
    UPLOAD_IMAGE_URL = "https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image"
    EMOTION_URL = (
        "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
        "/cgi-bin/emotion_cgi_publish_v6"
    )
    DELETE_URL = (
        "https://h5.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
        "/cgi-bin/emotion_cgi_delete_v6"
    )
    LIST_URL = (
        "https://user.qzone.qq.com/proxy/domain/taotao.qq.com"
        "/cgi-bin/emotion_cgi_msglist_v6"
    )
    DOLIKE_URL = (
        "https://user.qzone.qq.com/proxy/domain/w.qzone.qq.com"
        "/cgi-bin/likes/internal_dolike_app"
    )
    COMMENT_URL = (
        "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
        "/cgi-bin/emotion_cgi_re_feeds"
    )
    # 回复评论用与评论完全相同的 user 域 CGI（只多了 commentId / commentUin）。
    # 实测：这个接口**成功时回的是 HTML 框架页而不是 JSON**，所以回复是否成功
    # 一律以「回查评论详情能否找到自己的回复」为准，不看这个响应体。
    # 请求头也与 comment() 完全一致（不传 h5 专用请求头，传了反而更容易拿到短框架页）。
    REPLY_URL = (
        "https://user.qzone.qq.com/proxy/domain/taotao.qzone.qq.com"
        "/cgi-bin/emotion_cgi_re_feeds"
    )
    DETAIL_URL = (
        "https://h5.qzone.qq.com/proxy/domain/taotao.qq.com"
        "/cgi-bin/emotion_cgi_msgdetail_v6"
    )

    async def publish(
        self, text: str, images: Sequence[bytes] | None = None
    ) -> ApiResponse:
        """发表一条说说。

        Args:
            text: 说说正文，可为空字符串（仅发表图片）。
            images: 图片二进制内容列表，最多 9 张。

        Returns:
            统一响应对象，成功时 data 含 tid 与 now。

        Raises:
            RuntimeError: 图片上传失败时抛出。
        """
        ctx = await self.session.get_ctx()
        data: dict[str, Any] = {
            "syn_tweet_verson": "1",
            "paramstr": "1",
            "who": "1",
            "con": text,
            "feedversion": "1",
            "ver": "1",
            "ugc_right": "1",
            "to_sign": "0",
            "hostuin": ctx.uin,
            "code_version": "1",
            "format": "json",
            "qzreferrer": f"{self.BASE_URL}/{ctx.uin}",
        }

        if images:
            pic_bos: list[str] = []
            richvals: list[str] = []
            for image in images:
                pic_bo, richval = await self._upload_image(image)
                pic_bos.append(pic_bo)
                richvals.append(richval)
            data.update(
                pic_bo=",".join(pic_bos),
                richtype="1",
                richval="\t".join(richvals),
            )
            logger.info(f"已上传 {len(pic_bos)} 张图片")

        raw = await self.request(
            "POST",
            self.EMOTION_URL,
            params={"g_tk": ctx.gtk, "uin": ctx.uin},
            data=data,
        )
        return self._normalize_publish(raw)

    async def _upload_image(self, image: bytes, retry: int = 0) -> tuple[str, str]:
        """上传单张图片到 QQ空间相册并返回发布所需的标记。

        Args:
            image: 图片二进制内容。
            retry: 内部重试计数，调用方无需传入。

        Returns:
            二元组 (pic_bo, richval)。

        Raises:
            RuntimeError: 上传失败时抛出。
        """
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "POST",
            self.UPLOAD_IMAGE_URL,
            data={
                "filename": "filename",
                "uploadtype": "1",
                "albumtype": "7",
                "skey": ctx.skey,
                "uin": ctx.uin,
                "p_skey": ctx.p_skey,
                "output_type": "json",
                "base64": "1",
                "picfile": base64.b64encode(image).decode(),
            },
            headers={
                "referer": f"{self.BASE_URL}/{ctx.uin}",
                "origin": self.BASE_URL,
            },
            timeout=60,
        )

        resp = ApiResponse.from_raw(raw, code_key="ret", msg_key="msg")
        if not resp.ok:
            # 图片凭证过期时刷新登录态后重试一次
            if resp.code == QZONE_CODE_IMAGE_EXPIRED and retry < 1:
                logger.warning("图片上传凭证过期，刷新登录态后重试")
                await self.session.invalidate()
                return await self._upload_image(image, retry + 1)
            raise RuntimeError(f"上传图片失败: {resp.message or resp.code}")

        return QzoneParser.parse_upload_result(resp.data)

    async def delete(self, tid: str) -> ApiResponse:
        """删除自己空间里的一条说说。

        Args:
            tid: 说说 ID。

        Returns:
            统一响应对象。
        """
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "POST",
            self.DELETE_URL,
            params={"g_tk": ctx.gtk},
            data={
                "uin": ctx.uin,
                "topicId": f"{ctx.uin}_{tid}__1",
                "feedsType": 0,
                "feedsFlag": 0,
                "feedsKey": tid,
                "feedsAppid": 311,
                "feedsTime": int(time.time()),
                "fupdate": 1,
                "ref": "feeds",
                "qzreferrer": (
                    "https://user.qzone.qq.com/proxy/domain/ic2.qzone.qq.com"
                    "/cgi-bin/feeds/feeds_html_module"
                    f"?g_iframeUser=1&i_uin={ctx.uin}&i_login_uin={ctx.uin}"
                    "&mode=4&previewV8=1&style=35&version=8&needDelOpr=true"
                ),
            },
        )
        return ApiResponse.from_raw(raw)

    async def get_feeds(
        self, target_uin: str | int, *, pos: int = 0, num: int = 1
    ) -> ApiResponse:
        """获取指定 QQ 号可见的说说列表。

        Args:
            target_uin: 目标 QQ 号。
            pos: 起始位置。
            num: 获取条数。

        Returns:
            统一响应对象，成功时 data 内含 msglist。
        """
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "GET",
            self.LIST_URL,
            params={
                "g_tk": ctx.gtk,
                "uin": target_uin,
                "ftype": 0,
                "sort": 0,
                "pos": pos,
                "num": num,
                "replynum": 100,
                "callback": "_preloadCallback",
                "code_version": 1,
                "format": "json",
                "need_comment": 1,
                "need_private_comment": 1,
            },
        )
        return ApiResponse.from_raw(raw)

    async def like(self, uin: int | str, tid: str) -> ApiResponse:
        """给指定说说点赞。

        Args:
            uin: 说说作者的 QQ 号。
            tid: 说说 ID。

        Returns:
            统一响应对象。
        """
        ctx = await self.session.get_ctx()
        unikey = f"{self.BASE_URL}/{uin}/mood/{tid}"
        raw = await self.request(
            "POST",
            self.DOLIKE_URL,
            params={"g_tk": ctx.gtk},
            data={
                "qzreferrer": f"{self.BASE_URL}/{ctx.uin}",
                "opuin": ctx.uin,
                "unikey": unikey,
                "curkey": unikey,
                "appid": 311,
                "from": 1,
                "typeid": 0,
                "abstime": int(time.time()),
                "fid": tid,
                "active": 0,
                "format": "json",
                "fupdate": 1,
            },
        )
        return ApiResponse.from_raw(raw)

    async def comment(self, uin: int | str, tid: str, content: str) -> ApiResponse:
        """评论指定说说。

        Args:
            uin: 说说作者的 QQ 号。
            tid: 说说 ID。
            content: 评论内容。

        Returns:
            统一响应对象。
        """
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "POST",
            self.COMMENT_URL,
            params={"g_tk": ctx.gtk},
            data={
                "topicId": f"{uin}_{tid}__1",
                "uin": ctx.uin,
                "hostUin": uin,
                "feedsType": 100,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": 52,
                "format": "fs",
                "ref": "feeds",
                "content": content,
            },
        )
        return ApiResponse.from_raw(raw)

    @staticmethod
    def _h5_headers() -> dict[str, str]:
        """h5.qzone.qq.com 域接口所需的请求头。

        Returns:
            请求头字典；不带 Host，交由 aiohttp 按实际地址填写。
        """
        return {
            "User-Agent": USER_AGENT,
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Referer": "https://user.qzone.qq.com/",
            "Origin": "https://user.qzone.qq.com",
        }

    async def reply(
        self,
        uin: int | str,
        tid: str,
        comment_tid: str,
        comment_uin: int | str,
        content: str,
    ) -> ApiResponse:
        """回复自己说说下的一条评论，并以回查评论详情确认是否真的发出去了。

        实测结论：这个接口**成功时也不返回 JSON，而是一段 HTML 框架页**
        （``document.domain="…"; cb=frameElement.callback;``），因此响应体既不能
        用来判定成功，也不能用来判定登录失效。这里改为「发出请求 + 回查确认」：

        1. POST 回复接口（请求头与 ``comment()`` 完全一致，不传 h5 专用头）；
        2. 回查 ``emotion_cgi_msgdetail_v6``（``need_comment=1``、
           ``need_private_comment=1``），在该候选所在的评论线程里查找
           **本次新增**（``create_time`` 不早于发出请求前 120 秒）的、来自我的回复；
        3. 找到即判定成功，调用方据此写去重记录；找不到即判定失败，
           但**不会**因为「响应是 HTML」就判定为登录失效，也不会重取登录态。

        Args:
            uin: 说说作者（自己）的 QQ 号。
            tid: 说说 ID。
            comment_tid: 被回复评论（或子回复）的 ID。
            comment_uin: 被回复评论的作者 QQ 号。
            content: 回复正文。

        Returns:
            统一响应对象；成功表示「已回查确认自己的回复确实在评论下」。
        """
        ctx = await self.session.get_ctx()
        topic_id = f"{uin}_{tid}__1"
        started = int(time.time())
        raw = await self.request(
            "POST",
            self.REPLY_URL,
            params={"g_tk": ctx.gtk},
            data={
                "topicId": topic_id,
                "uin": ctx.uin,
                "hostUin": uin,
                "feedsType": 100,
                "inCharset": "utf-8",
                "outCharset": "utf-8",
                "plat": "qzone",
                "source": "ic",
                "platformid": 52,
                "format": "fs",
                "ref": "feeds",
                "content": content,
                "commentId": comment_tid,
                "commentUin": comment_uin,
                "richval": "",
                "richtype": "",
                "private": "0",
                "paramstr": 2,
                "qzreferrer": f"{self.BASE_URL}/{ctx.uin}/main",
            },
            # 这个接口成功时也回 HTML 页面，页面响应只写 debug 日志，结论交给回查
            page_is_expected=True,
        )
        resp = ApiResponse.from_raw(raw)
        meta = raw.get(QZONE_INTERNAL_META_KEY)
        snippet = str(meta.get("snippet") or "") if isinstance(meta, dict) else ""
        post_note = self._reply_post_note(resp)

        found, confirm_note = await self._confirm_reply(
            ctx.uin, tid, comment_tid, content, since=started - _CONFIRM_SLACK
        )
        if found:
            logger.info(
                f"回复评论已确认: url={self.REPLY_URL}｜topicId={topic_id}"
                f"｜commentId={comment_tid}｜commentUin={comment_uin}"
                f"｜接口={post_note}｜回查结果={confirm_note}"
            )
            return ApiResponse(
                ok=True,
                code=0,
                message=None,
                data={"confirmed": True, "detail": confirm_note},
                raw=raw,
            )

        # HTTP 403 有单独文案；其余情况（含「回的是 HTML 页面」）都按「回查没确认」回报
        denied = resp.code == QZONE_CODE_FORBIDDEN
        logger.error(
            f"回复评论失败: url={self.REPLY_URL}｜topicId={topic_id}"
            f"｜commentId={comment_tid}｜commentUin={comment_uin}"
            f"｜原因={post_note}｜回查结果={confirm_note}｜响应片段: {snippet}"
        )
        return ApiResponse(
            ok=False,
            code=QZONE_CODE_FORBIDDEN if denied else QZONE_CODE_REPLY_UNCONFIRMED,
            message=QZONE_MSG_FORBIDDEN if denied else QZONE_MSG_REPLY_UNCONFIRMED,
            data={},
            raw=raw,
        )

    @staticmethod
    def _reply_post_note(resp: ApiResponse) -> str:
        """把回复 POST 的返回归纳成一句给日志用的话。

        页面响应（``-3002``）既不算成功也不算失败：实测成功时也是页面。

        Args:
            resp: 回复接口的统一响应对象。

        Returns:
            日志用的短说明。
        """
        if resp.ok:
            return "接口返回成功"
        if resp.code == QZONE_CODE_UNEXPECTED_PAGE:
            return "接口返回页面（按回查结果判定）"
        if resp.code == QZONE_CODE_FORBIDDEN:
            return QZONE_MSG_FORBIDDEN
        return str(resp.message or resp.code)

    async def _confirm_reply(
        self,
        own_uin: int,
        tid: str,
        comment_tid: str,
        content: str,
        *,
        since: int = 0,
    ) -> tuple[bool, str]:
        """回查说说详情，确认自己的回复是否真的出现在该候选下。

        判定放宽到「只要这条候选所在的线程里、**本次新增**了我的回复就算成功」，
        正文是否完全一致只写进日志：文本可能因清洗、截断或空间侧改写而不同，
        以此判失败会造成重复回复。``since`` 是发出请求前的时间戳（再留一点余量），
        用来把「本次新增」和「早就存在的历史回复」区分开——历史回复不能算成功。

        Args:
            own_uin: 自己的 QQ 号。
            tid: 说说 ID。
            comment_tid: 被回复评论（或子回复）的 ID。
            content: 本次发出的回复正文（仅用于日志比对）。
            since: 认为「新」的最早时间戳。

        Returns:
            二元组 (是否确认成功, 给日志看的一句说明)。
        """
        try:
            resp = await self.get_detail(tid)
        except Exception as e:
            return False, f"回查异常（{e}）"
        if not resp.ok:
            return False, f"回查失败（{resp.message or resp.code}）"

        comments = QzoneParser.parse_comments(resp.data)
        matched, note = QzoneParser.find_own_reply(
            comments, comment_tid, own_uin, content, since=since
        )
        if matched is not None:
            return True, f"回查命中：{note}"
        return False, f"回查未命中：{note}"

    async def get_detail(self, tid: str) -> ApiResponse:
        """取一条说说的详情，用于拿列表接口没带全的评论明细。

        Args:
            tid: 说说 ID。

        Returns:
            统一响应对象，成功时 data 内含 commentlist。
        """
        ctx = await self.session.get_ctx()
        raw = await self.request(
            "GET",
            self.DETAIL_URL,
            params={
                "g_tk": ctx.gtk,
                "uin": ctx.uin,
                "tid": tid,
                "format": "json",
                "num": 100,
                "callback": "_preloadCallback",
                "code_version": 1,
                "need_comment": 1,
                "need_private_comment": 1,
            },
            headers=self._h5_headers(),
        )
        return ApiResponse.from_raw(raw)

    @staticmethod
    def _normalize_publish(raw: dict[str, Any]) -> ApiResponse:
        """统一发表说说的返回结构。

        QQ空间不同版本会把 tid/now 放在顶层或 data 内，这里做兼容。

        Args:
            raw: 原始响应。

        Returns:
            归一化后的统一响应对象。
        """
        inner = raw.get("data")
        payload = inner if isinstance(inner, dict) else {}

        tid = str(raw.get("tid") or payload.get("tid") or "").strip()
        now = raw.get("now") or payload.get("now") or int(time.time())

        code = raw.get("code", QZONE_CODE_UNKNOWN)
        if not isinstance(code, int):
            code = QZONE_CODE_UNKNOWN

        if tid:
            return ApiResponse(
                ok=True,
                code=code,
                message=None,
                data={"tid": tid, "now": now},
                raw=raw,
            )

        message = (
            raw.get("message")
            or payload.get("message")
            or payload.get("msg")
            or "接口未返回 tid，发布可能未成功"
        )
        return ApiResponse(
            ok=False,
            code=code,
            message=str(message),
            data={},
            raw=raw,
        )
