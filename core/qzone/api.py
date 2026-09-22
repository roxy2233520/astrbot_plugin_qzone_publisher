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
from .constants import QZONE_CODE_IMAGE_EXPIRED, QZONE_CODE_UNKNOWN
from .model import ApiResponse
from .parser import QzoneParser


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
