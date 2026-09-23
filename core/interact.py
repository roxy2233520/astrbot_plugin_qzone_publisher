"""自动读 / 赞 / 评好友说说。

默认策略是**只读**：按配置里关注的 QQ 号定时拉取最近说说，记录已处理过的 tid；
点赞与评论必须显式开启（``interact_like`` / ``interact_comment``）。
开启 ``draft_for_comment`` 后，生成的评论先进入草稿箱等人工确认，不会直接发出。

**时间窗口**：每个好友只看 ``interact_days`` 天内**最新的一条**说说；
如果这位好友最新一条都超出了窗口，就整体跳过（不点赞、不评论），
免得去评论几天前的老说说。

去重依据是 ``uin_tid``，存在 ``<插件数据目录>/interacted_tids.json``，
所以同一条说说不会被点赞或评论第二次。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from astrbot.api import logger

from .config import PluginConfig
from .draft import Draft, DraftBox
from .llm import AIClient
from .qzone import FeedPost, QzoneAPI, QzoneParser

_SEEN_LIMIT = 1000


@dataclass(slots=True)
class InteractResult:
    """一次互动巡检的汇总。

    Attributes:
        checked: 检查过的说说条数。
        liked: 点赞条数。
        commented: 已评论条数。
        drafted: 转入草稿箱的条数。
        skipped: 跳过条数（自己发的 / 已处理过）。
        stale: 最近一条超出时间窗口、整体跳过的好友数。
        errors: 出错信息。
    """

    checked: int = 0
    liked: int = 0
    commented: int = 0
    drafted: int = 0
    skipped: int = 0
    stale: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """生成可读汇总。"""
        parts = [
            f"检查 {self.checked} 条",
            f"跳过 {self.skipped} 条",
            f"点赞 {self.liked} 条",
            f"评论 {self.commented} 条",
        ]
        if self.drafted:
            parts.append(f"转草稿 {self.drafted} 条")
        if self.stale:
            parts.append(f"{self.stale} 人最近一条超出时间窗口")
        text = "，".join(parts)
        if self.errors:
            text += "\n" + "\n".join(f"⚠️ {item}" for item in self.errors[:5])
        return text


class InteractService:
    """好友说说互动服务。"""

    def __init__(
        self,
        config: PluginConfig,
        ai: AIClient,
        api: QzoneAPI,
        drafts: DraftBox,
    ) -> None:
        """初始化服务。

        Args:
            config: 插件配置。
            ai: AI 客户端。
            api: QQ空间接口。
            drafts: 草稿箱。
        """
        self.cfg = config
        self.ai = ai
        self.api = api
        self.drafts = drafts
        self.file = Path(config.data_dir) / "interacted_tids.json"
        self._seen: list[str] = []
        self.load()

    # ------------------------------------------------------------------
    # 去重记录
    # ------------------------------------------------------------------

    def load(self) -> None:
        """加载已处理 tid 记录。"""
        self._seen = []
        if not self.file.exists():
            return
        try:
            raw = json.loads(self.file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"互动去重记录读取失败，已忽略: {e}")
            return
        if isinstance(raw, list):
            self._seen = [str(item) for item in raw]

    def save(self) -> None:
        """原子写入去重记录。"""
        try:
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._seen[-_SEEN_LIMIT:], ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.file)
        except Exception as e:
            logger.error(f"互动去重记录写入失败: {e}")

    def seen(self, post: FeedPost) -> bool:
        """该说说是否已处理过。"""
        return f"{post.uin}_{post.tid}" in self._seen

    def mark(self, post: FeedPost) -> None:
        """把说说标记为已处理。"""
        key = f"{post.uin}_{post.tid}"
        if key not in self._seen:
            self._seen.append(key)

    # ------------------------------------------------------------------
    # 巡检
    # ------------------------------------------------------------------

    @property
    def targets(self) -> list[str]:
        """配置里要关注的 QQ 号列表。"""
        values = self.cfg.interact_uins or []
        return [str(item).strip() for item in values if str(item).strip()]

    def mode_text(self) -> str:
        """给 /空间状态 用的模式描述。"""
        if not bool(self.cfg.interact_enabled):
            return "关闭"
        parts = ["只读", f"{self.window_days} 天内最新一条"]
        if bool(self.cfg.interact_like):
            parts.append("点赞")
        if bool(self.cfg.interact_comment):
            parts.append("评论(先确认)" if bool(self.cfg.draft_for_comment) else "评论")
        return " + ".join(parts)

    @property
    def window_days(self) -> int:
        """时间窗口天数（至少 1 天）。"""
        return max(int(self.cfg.interact_days or 0), 1)

    @staticmethod
    def _ago(created_time: int) -> str:
        """把发布时间戳转成「多久以前」的人话。"""
        if created_time <= 0:
            return "未知时间"
        seconds = max(int(time.time()) - created_time, 0)
        if seconds < 3600:
            return f"{seconds // 60} 分钟"
        if seconds < 86400:
            return f"{seconds // 3600} 小时"
        return f"{seconds // 86400} 天"

    async def run_once(
        self, targets: list[str] | None = None, *, force: bool = False
    ) -> InteractResult:
        """巡检一轮：每个好友只看时间窗口内最新的一条说说。

        Args:
            targets: 覆盖本次巡检的目标 QQ 号；缺省用配置里的列表。
            force: 为 True 时忽略去重记录（手动指令排查用）。

        Returns:
            本次巡检汇总。
        """
        result = InteractResult()
        watch = targets if targets is not None else self.targets
        if not watch:
            result.errors.append("未配置 interact_uins，不知道该读谁的说说")
            return result

        try:
            self_uin = await self.api.session.get_uin()
        except Exception as e:
            logger.debug(f"获取自身 QQ 号失败: {e}")
            self_uin = 0

        count = max(int(self.cfg.interact_count or 0), 1)
        days = self.window_days
        cutoff = int(time.time()) - days * 86400

        for target in watch:
            try:
                resp = await self.api.get_feeds(target, pos=0, num=count)
            except Exception as e:
                result.errors.append(f"{target}: {e}")
                continue

            if not resp.ok:
                result.errors.append(f"{target}: {resp.message or resp.code}")
                continue

            posts = QzoneParser.parse_feeds(resp.data)
            if not posts:
                logger.info(f"QQ {target} 没有可见的说说")
                continue

            # 时间戳缺失（0）时无法判断新旧，按「在窗口内」处理，避免整批被误跳过
            fresh = [
                post
                for post in posts
                if post.created_time <= 0 or post.created_time >= cutoff
            ]
            if not fresh:
                result.stale += 1
                newest = max(posts, key=lambda item: item.created_time)
                logger.info(
                    f"QQ {target} 最近一条说说在 {self._ago(newest.created_time)}前，"
                    f"超出 {days} 天窗口，本轮跳过"
                )
                continue

            # 每个好友只取窗口内最新的一条：不逐条处理，也不去评论几天前的老说说
            post = max(fresh, key=lambda item: item.created_time)
            try:
                await self._handle_post(post, self_uin, result, force=force)
            except Exception as e:
                result.errors.append(f"{target}/{post.tid}: {e}")

        self.save()
        logger.info(f"说说互动巡检完成：{result.summary()}")
        return result

    async def _handle_post(
        self,
        post: FeedPost,
        self_uin: int,
        result: InteractResult,
        *,
        force: bool,
    ) -> None:
        """处理单条说说：去重 -> 点赞 -> 评论/转草稿。"""
        result.checked += 1

        if bool(self.cfg.interact_skip_self) and self_uin and post.uin == self_uin:
            result.skipped += 1
            return

        if not force and self.seen(post):
            result.skipped += 1
            return

        if bool(self.cfg.interact_like):
            resp = await self.api.like(post.uin, post.tid)
            if resp.ok:
                result.liked += 1
                logger.info(f"已点赞 {post.name or post.uin} 的说说 {post.tid}")
            else:
                result.errors.append(
                    f"点赞 {post.tid} 失败: {resp.message or resp.code}"
                )

        if bool(self.cfg.interact_comment):
            content = await self._generate_comment(post)
            if bool(self.cfg.draft_for_comment):
                self.drafts.put(
                    Draft(
                        kind="comment",
                        text=content,
                        source="interact",
                        target_uin=post.uin,
                        target_tid=post.tid,
                        target_name=post.name,
                        target_text=post.text,
                    )
                )
                result.drafted += 1
            else:
                resp = await self.api.comment(post.uin, post.tid, content)
                if resp.ok:
                    result.commented += 1
                    logger.info(f"已评论 {post.name or post.uin} 的说说 {post.tid}")
                else:
                    result.errors.append(
                        f"评论 {post.tid} 失败: {resp.message or resp.code}"
                    )

        self.mark(post)

    async def _generate_comment(self, post: FeedPost) -> str:
        """用 AI 生成一条评论。

        Args:
            post: 目标说说。

        Returns:
            评论正文。

        Raises:
            RuntimeError: AI 不可用或返回为空时抛出。
        """
        task = str(self.cfg.interact_comment_prompt or "").strip() or (
            "生成一句简短、直接、贴题的评论，不要解释、不做自我描述、不分选项。"
        )
        limit = max(int(self.cfg.interact_comment_max_chars or 0), 1)

        text = await self.ai.chat(
            system_prompt=(
                f"{task}\n\n# 输出要求\n"
                f"只输出评论正文本身，不要引号、不要解释，不超过 {limit} 字。"
            ),
            prompt=f"说说作者：{post.name or post.uin}\n说说内容：{post.text or '（无正文）'}",
            provider_id=str(self.cfg.llm_comment_provider_id or ""),
            feature="评论",
        )

        cleaned = text.strip().strip("\"'“”")
        cleaned = "".join(cleaned.split())
        return cleaned[:limit]

    async def rewrite_comment(self, draft: Draft) -> str:
        """按草稿记录的目标重新生成一版评论。

        Args:
            draft: 评论草稿，需带 target_uin / target_tid / target_text。

        Returns:
            新生成的评论正文。

        Raises:
            RuntimeError: AI 不可用或返回为空时抛出。
        """
        post = FeedPost(
            uin=draft.target_uin,
            tid=draft.target_tid,
            name=draft.target_name,
            text=draft.target_text,
        )
        return await self._generate_comment(post)
