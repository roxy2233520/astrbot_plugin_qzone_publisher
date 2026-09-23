"""自动读 / 赞 / 评好友说说，并回复自己说说下的评论。

默认策略是**只读**：按配置里关注的 QQ 号定时拉取最近说说，记录已处理过的 tid；
点赞与评论必须显式开启（``interact_like`` / ``interact_comment``）。
开启 ``draft_for_comment`` 后，生成的评论先进入草稿箱等人工确认，不会直接发出。

**时间窗口**：每个好友只看 ``interact_days`` 天内**最新的一条**说说；
如果这位好友最新一条都超出了窗口，就整体跳过（不点赞、不评论），
免得去评论几天前的老说说。

**回复自己说说下的评论**（``interact_reply_enabled``，默认关闭）：
只处理自己 ``interact_reply_days`` 天内发布的说说，别人的评论才回复；
评论与它下面的子回复（接口的 ``list_3``）都会被视为待回复对象，
回复子回复时 ``commentId`` 用子回复自己的 tid；
同一条评论只回复一次，每轮最多 ``interact_reply_max_per_run`` 条，
且同一条说说每轮最多回一条；回复一律直接发出，不经过草稿确认。

**回复是否成功以回查为准**：回复接口实测「成功时也返回 HTML 框架页」，
因此发出请求后会回查说说详情，只有在该评论的子回复里找到自己的回复才算成功；
评论下已经有自己的回复时直接跳过，避免重复叠加。

去重依据分别是 ``uin_tid``（好友互动）与 ``说说tid_评论tid``（回复），
存在 ``<插件数据目录>/interacted_tids.json`` 与 ``replied_comments.json``。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from astrbot.api import logger

from .config import PluginConfig
from .draft import Draft, DraftBox
from .llm import AIClient
from .qzone import FeedComment, FeedPost, QzoneAPI, QzoneParser
from .ui import kv

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
        """生成可读汇总（统一排版：首行为统计，其余为键值行）。"""
        parts = [
            f"检查 {self.checked} 条，跳过 {self.skipped} 条，"
            f"点赞 {self.liked} 条，评论 {self.commented} 条"
        ]
        if self.drafted:
            parts.append(kv("转草稿", f"{self.drafted} 条"))
        if self.stale:
            parts.append(kv("时间窗口", f"{self.stale} 人最近一条超出时间窗口"))
        text = "\n".join(parts)
        if self.errors:
            text += "\n" + "\n".join(kv("失败", item) for item in self.errors[:5])
        return text


@dataclass(slots=True)
class ReplyResult:
    """一次「回复自己说说下评论」巡检的汇总。

    Attributes:
        checked: 检查过的评论条数（含子回复）。
        replied: 已回复条数（均经回查确认）。
        drafted: 转入草稿箱的条数（回复不走草稿，恒为 0）。
        skipped: 跳过条数（自己的评论 / 空内容 / 已回复过 / 已有自己的回复）。
        errors: 出错信息。
    """

    checked: int = 0
    replied: int = 0
    drafted: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """生成可读汇总（统一排版：首行为统计，其余为键值行）。"""
        parts = [
            f"检查评论 {self.checked} 条，回复 {self.replied} 条，"
            f"跳过 {self.skipped} 条"
        ]
        if self.drafted:
            parts.append(kv("转草稿", f"{self.drafted} 条"))
        text = "\n".join(parts)
        if self.errors:
            text += "\n" + "\n".join(kv("失败", item) for item in self.errors[:5])
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
        self.reply_file = Path(config.data_dir) / "replied_comments.json"
        self._replied: list[str] = []
        self.load_replied()
        # 每日回复条数（用于「今日已回几条」），与去重记录分开存
        self.reply_count_file = Path(config.data_dir) / "reply_counts.json"
        self._reply_counts: dict[str, int] = {}
        self.load_reply_counts()
        # 回复巡检轮次：仅用于日志，让用户能看出它在跑
        self.reply_round = 0

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
        if bool(self.cfg.interact_reply_enabled):
            parts.append("回复")
        return " + ".join(parts)

    @property
    def window_days(self) -> int:
        """好友互动的时间窗口天数（至少 1 天）。"""
        return max(int(self.cfg.interact_days or 0), 1)

    @property
    def reply_days(self) -> int:
        """回复评论的时间窗口天数（至少 1 天）。

        回复有自己的窗口 ``interact_reply_days``（默认 7 天），比好友互动的窗口更长：
        旧说说下面新来的评论同样要被发现，不能被 ``interact_days`` 挡住。
        """
        return max(int(self.cfg.interact_reply_days or 0), 1)

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

    # ------------------------------------------------------------------
    # 回复自己说说下的评论
    # ------------------------------------------------------------------

    @staticmethod
    def comment_id_problem(post_tid: str, comment_tid: str) -> str:
        """检查评论 id 是否可用。

        实测：空间给评论的 id 就是小整数（例如 ``1``），**不是**长数字，
        因此只挡真正不可用的两种：完全没有 id，以及与说说 id 相同
        （后者说明解析错位，拿它去请求只会拿到错误响应）。

        Args:
            post_tid: 所在说说的 tid。
            comment_tid: 从接口解析出来的评论 id。

        Returns:
            有问题时返回原因说明；正常时返回空串。
        """
        value = str(comment_tid or "").strip()
        if not value:
            return "评论缺少 id，已跳过"
        if value == str(post_tid or "").strip():
            return f"评论 id（{value}）与说说 id 相同，疑似解析错位，已跳过"
        return ""

    @staticmethod
    def reply_candidates(comment: FeedComment) -> list[FeedComment]:
        """一条评论下所有可回复的对象：评论本身，以及它下面的子回复。

        子回复来自接口的 ``list_3``；别人的子回复同样是新的待回复对象，
        回复它时 ``commentId`` 用该子回复自己的 tid。

        Args:
            comment: 顶层评论。

        Returns:
            候选列表，父评论在前、子回复按接口顺序在后。
        """
        return [comment, *comment.replies]

    @staticmethod
    def has_own_reply(
        thread: FeedComment, candidate: FeedComment, self_uin: int
    ) -> bool:
        """该评论（或它的子回复）下面是否已经有我发出的回复。

        只要这条评论的 ``list_3`` 里出现我自己的回复，就认为这条线程已经处理过：
        评论本身不再回复，它下面别人的子回复也一并跳过——宁可少回一次，
        也不要在已有重复回复的基础上继续叠加。``parent_tid`` 指向别的子回复时
        不算（那是另一条分支）。

        Args:
            thread: 顶层评论。
            candidate: 本次准备回复的对象。
            self_uin: 自己的 QQ 号。

        Returns:
            已经有我的回复时返回 True。
        """
        if not self_uin:
            return False
        for sub in thread.replies:
            if sub.uin != self_uin:
                continue
            parent = str(sub.parent_tid).strip()
            # 子回复多半不带 parent_tid，解析时会填成父评论的 tid；
            # 两种都算「这条线程已经有我的回复」。
            if not parent or parent in (candidate.tid, thread.tid):
                return True
        return False

    @staticmethod
    def _reply_key(post_tid: str, comment_tid: str) -> str:
        """回复去重键：说说 tid + 评论 tid。"""
        return f"{post_tid}_{comment_tid}"

    def load_replied(self) -> None:
        """加载已回复评论记录。"""
        self._replied = []
        if not self.reply_file.exists():
            return
        try:
            raw = json.loads(self.reply_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"回复去重记录读取失败，已忽略: {e}")
            return
        if isinstance(raw, list):
            self._replied = [str(item) for item in raw]

    def save_replied(self) -> None:
        """原子写入已回复评论记录。"""
        try:
            self.reply_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.reply_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._replied[-_SEEN_LIMIT:], ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(self.reply_file)
        except Exception as e:
            logger.error(f"回复去重记录写入失败: {e}")

    def replied(self, post_tid: str, comment_tid: str) -> bool:
        """该条评论是否已经回复过。"""
        return self._reply_key(post_tid, comment_tid) in self._replied

    def mark_replied(self, post_tid: str, comment_tid: str) -> None:
        """把评论标记为已回复并落盘（回复真正发出后调用）。"""
        key = self._reply_key(post_tid, comment_tid)
        if key not in self._replied:
            self._replied.append(key)
        self.save_replied()

    @property
    def replied_count(self) -> int:
        """累计已回复的评论条数。"""
        return len(self._replied)

    # ------------------------------------------------------------------
    # 每日回复条数（今日已回几条）
    # ------------------------------------------------------------------

    @staticmethod
    def _today() -> str:
        """今天的日期串。"""
        return datetime.now().strftime("%Y-%m-%d")

    def load_reply_counts(self) -> None:
        """加载每日回复条数记录。"""
        self._reply_counts = {}
        if not self.reply_count_file.exists():
            return
        try:
            raw = json.loads(self.reply_count_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"每日回复条数记录读取失败，已忽略: {e}")
            return
        if isinstance(raw, dict):
            for key, value in raw.items():
                try:
                    self._reply_counts[str(key)] = int(value)
                except (TypeError, ValueError):
                    continue

    def save_reply_counts(self) -> None:
        """原子写入每日回复条数记录，只保留最近 7 天。"""
        try:
            self.reply_count_file.parent.mkdir(parents=True, exist_ok=True)
            keys = sorted(self._reply_counts)[-7:]
            payload = {key: self._reply_counts[key] for key in keys}
            tmp = self.reply_count_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.reply_count_file)
        except Exception as e:
            logger.error(f"每日回复条数记录写入失败: {e}")

    def count_reply(self) -> int:
        """把「今天回复了一条」记进每日计数。"""
        today = self._today()
        self._reply_counts[today] = self._reply_counts.get(today, 0) + 1
        self.save_reply_counts()
        return self._reply_counts[today]

    @property
    def replied_today(self) -> int:
        """今天已经回复了几条。"""
        return int(self._reply_counts.get(self._today(), 0))

    @property
    def reply_limit(self) -> int:
        """每轮最多回复几条（至少 1）。"""
        return max(int(self.cfg.interact_reply_max_per_run or 0), 1)

    def reply_mode_text(self) -> str:
        """给状态与指令用的回复模式描述。"""
        if not bool(self.cfg.interact_reply_enabled):
            return "关闭"
        return (
            f"开启（每轮最多 {self.reply_limit} 条，巡检到即直接回复并回查确认，"
            f"已回复 {self.replied_count} 条）"
        )

    async def run_replies_once(self, *, force: bool = False) -> ReplyResult:
        """巡检一轮：回复自己说说下别人留下的新评论。

        只处理 ``interact_reply_days`` 天内自己发布的说说（默认 7 天，比好友互动的
        窗口更长，避免旧说说下的新评论永远发现不了）；每轮最多回复
        ``interact_reply_max_per_run`` 条，且同一条说说每轮最多回一条；
        自己的评论、空内容评论、已回复过的评论，以及**评论下已经有自己回复**的，
        都会被跳过；评论下的子回复同样会被当作待回复对象。

        回复是否成功以「回查评论详情能否找到自己的回复」为准，
        因此回执里的「回复 N 条」都是确认过的条数。

        无论本轮有没有新评论，都会写一行 info 日志（第几轮 / 检查 / 回复 / 跳过），
        方便确认定时任务确实在跑。

        Args:
            force: 保留参数以兼容旧调用；回复早已不走草稿，当前它不再改变行为。

        Returns:
            本次巡检汇总。
        """
        result = ReplyResult()
        self.reply_round += 1
        try:
            self_uin = await self.api.session.get_uin()
        except Exception as e:
            result.errors.append(f"无法确认自己的 QQ 号: {e}")
            self._log_round(result, reason="无法确认自己的 QQ 号")
            return result
        if not self_uin:
            result.errors.append("无法确认自己的 QQ 号，本轮不回复评论")
            self._log_round(result, reason="无法确认自己的 QQ 号")
            return result

        days = self.reply_days
        cutoff = int(time.time()) - days * 86400
        limit = self.reply_limit
        count = max(int(self.cfg.interact_count or 0), 1)

        try:
            resp = await self.api.get_feeds(self_uin, pos=0, num=count)
        except Exception as e:
            result.errors.append(f"读取自己的说说失败: {e}")
            self._log_round(result, reason=f"读取自己的说说失败：{e}")
            return result
        if not resp.ok:
            result.errors.append(f"读取自己的说说失败: {resp.message or resp.code}")
            self._log_round(result, reason="读取自己的说说失败")
            return result

        posts = QzoneParser.parse_feeds(resp.data)
        fresh = [
            post
            for post in posts
            if post.created_time <= 0 or post.created_time >= cutoff
        ]
        reason = ""
        if not posts:
            reason = "自己的说说列表为空"
        elif not fresh:
            reason = (
                f"自己的说说都在 {days} 天窗口之外（窗口由 interact_reply_days 决定）"
            )

        for post in sorted(fresh, key=lambda item: item.created_time, reverse=True):
            if result.replied >= limit:
                break

            comments = await self._comments_of(post, result)
            note = await self._reply_in_post(post, comments, self_uin, result, limit)
            if note:
                reason = note

        if result.replied >= limit:
            reason = (
                f"本轮达到每轮上限 {limit} 条，"
                "剩余新评论会在下一轮继续处理（可调大「每轮最多回复几条」）"
            )

        self.save_replied()
        self._log_round(result, reason=reason)
        return result

    async def _reply_in_post(
        self,
        post: FeedPost,
        comments: list[FeedComment],
        self_uin: int,
        result: ReplyResult,
        limit: int,
    ) -> str:
        """处理一条说说下的评论与子回复，最多回复一条。

        Args:
            post: 目标说说。
            comments: 该说说下的顶层评论。
            self_uin: 自己的 QQ 号。
            result: 本轮汇总。
            limit: 每轮最多回复几条。

        Returns:
            给本轮日志用的说明；没有值得说明的事情时返回空串。
        """
        note = ""
        for thread in comments:
            for candidate in self.reply_candidates(thread):
                if result.replied >= limit:
                    return note

                result.checked += 1
                if candidate.uin == self_uin:
                    result.skipped += 1
                    continue
                if not candidate.content.strip():
                    result.skipped += 1
                    continue
                # 评论 id 不可用时先挡下来：拿错 id 去请求只会白打一次请求
                problem = self.comment_id_problem(post.tid, candidate.tid)
                if problem:
                    result.skipped += 1
                    note = problem
                    logger.warning(f"[reply] {post.tid} 下的{problem}")
                    continue
                if self.replied(post.tid, candidate.tid):
                    result.skipped += 1
                    continue
                # 这条评论下已经有我的回复：跳过，避免继续叠加重复回复
                if self.has_own_reply(thread, candidate, self_uin):
                    result.skipped += 1
                    note = f"{post.tid} 下评论 {candidate.tid} 已经有我的回复，本轮跳过"
                    logger.info(f"[reply] {note}")
                    continue

                try:
                    await self._reply_to_comment(post, candidate, result)
                except Exception as e:
                    result.errors.append(f"{post.tid}/{candidate.tid}: {e}")
                # 同一条说说每轮最多回一条
                return f"{post.tid} 下已回复一条（同一条说说每轮最多回一条）"
        return note

    def _log_round(self, result: ReplyResult, *, reason: str = "") -> None:
        """写一行本轮巡检日志（无论有没有新评论都写）。

        Args:
            result: 本轮汇总。
            reason: 未回复或跳过时的原因说明。
        """
        line = (
            f"[reply] 第 {self.reply_round} 轮评论巡检：检查 {result.checked} 条，"
            f"回复 {result.replied} 条，转草稿 {result.drafted} 条，"
            f"跳过 {result.skipped} 条"
        )
        if reason:
            line += f"｜说明：{reason}"
        if result.errors:
            line += f"｜错误 {len(result.errors)} 条：{result.errors[0]}"
        logger.info(line)

    async def _comments_of(
        self, post: FeedPost, result: ReplyResult
    ) -> list[FeedComment]:
        """取一条说说的评论明细，列表没带全时回退到详情接口。

        Args:
            post: 目标说说。
            result: 本轮汇总，用于记录取详情的错误。

        Returns:
            评论明细列表；取不到时返回空列表。
        """
        if post.comments:
            return post.comments
        if post.comment_count <= 0:
            return []

        try:
            resp = await self.api.get_detail(post.tid)
        except Exception as e:
            result.errors.append(f"{post.tid}: 取评论详情异常 {e}")
            return []
        if not resp.ok:
            result.errors.append(
                f"{post.tid}: 取评论详情失败 {resp.message or resp.code}"
            )
            return []
        return QzoneParser.parse_comments(resp.data)

    async def _reply_to_comment(
        self, post: FeedPost, comment: FeedComment, result: ReplyResult
    ) -> None:
        """生成一条回复并直接发出。

        ``api.reply()`` 成功即代表**已回查确认**自己的回复出现在该评论下，
        因此只有这时才写去重记录与今日计数。

        Args:
            post: 评论所在的说说。
            comment: 被回复的评论（也可能是别人写的子回复）。
            result: 本轮汇总，用于累计结果与错误。
        """
        content = await self._generate_reply(post, comment)

        # 回复一律直接发出：不生成草稿、也不需要用户确认
        resp = await self.api.reply(
            post.uin, post.tid, comment.tid, comment.uin, content
        )
        if not resp.ok:
            result.errors.append(
                f"回复评论 {comment.tid} 失败: {resp.message or resp.code}"
            )
            return

        result.replied += 1
        self.mark_replied(post.tid, comment.tid)
        self.count_reply()
        logger.info(
            f"已回复 {comment.display_name()} 在 {post.tid} 下的评论（回查已确认）"
        )

    async def _generate_reply(self, post: FeedPost, comment: FeedComment) -> str:
        """用 AI 生成一条评论回复。

        Args:
            post: 评论所在的说说。
            comment: 被回复的评论。

        Returns:
            回复正文。

        Raises:
            RuntimeError: AI 返回内容为空时抛出。
        """
        task = str(self.cfg.interact_reply_prompt or "").strip() or (
            "针对对方的评论写一句得体的回复，直接回应对方提到的内容，"
            "不要解释、不做自我描述、不分选项。"
        )
        limit = max(int(self.cfg.interact_reply_max_chars or 0), 1)

        text = await self.ai.chat(
            system_prompt=(
                f"{task}\n\n# 输出要求\n"
                f"只输出回复正文本身，不要引号、不要解释，不超过 {limit} 字。"
            ),
            prompt=(
                f"我的说说：{post.text or '（无正文）'}\n"
                f"对方（{comment.display_name()}）的评论：{comment.content}"
            ),
            provider_id=str(self.cfg.llm_reply_provider_id or ""),
            feature="回复",
        )

        cleaned = text.strip().strip("\"'“”")
        cleaned = "".join(cleaned.split())
        if not cleaned:
            raise RuntimeError("AI 生成的回复内容为空")
        return cleaned[:limit]
