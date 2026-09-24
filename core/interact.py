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
from collections.abc import Callable
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
# 「已尝试未确认」记录的保留条数
_ATTEMPT_LIMIT = 200
# 同一候选在回查未确认后，多久才允许再试一次（秒）
_ATTEMPT_RETRY_SECONDS = 24 * 3600
# 放进提示词的说说正文截断长度
_BRIEF_LIMIT = 80
# 回复提示词的默认任务描述（与面板里的默认值保持一致）
DEFAULT_REPLY_PROMPT = (
    "针对这段评论交流写一句得体的回复：结合整段对话回应对方最新那条，"
    "不要只针对最后一句孤立作答，也不要重复自己已经说过的意思，"
    "不要解释、不做自我描述、不分选项。"
)


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
        gated: 不在特权名单里、也没接受过主动消息，因而跳过互动的人数。
        errors: 出错信息。
    """

    checked: int = 0
    liked: int = 0
    commented: int = 0
    drafted: int = 0
    skipped: int = 0
    stale: int = 0
    gated: int = 0
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
        if self.gated:
            parts.append(
                kv("名单/同意", f"{self.gated} 人不在特权名单里也没接受主动消息")
            )
        text = "\n".join(parts)
        if self.errors:
            text += "\n" + "\n".join(kv("失败", item) for item in self.errors[:5])
        return text


@dataclass(slots=True)
class ReplyTarget:
    """一条待回复的候选（评论或子回复），身份带层级。

    空间给父评论与子回复**各自独立编号**（都从 1 开始），所以只用 tid 数字做身份
    会互相冲突（父评论 tid=1 与子回复 tid=1）。这里用「说说 tid + 层级路径」表示：

    Attributes:
        comment: 对应的评论对象。
        path: 从顶层评论到自己的 tid 路径，例如 ``("1",)`` 或 ``("1", "2")``。
        post_tid: 所属说说的 tid。
        parent_comment: 上一层节点；顶层评论时为 None。
    """

    comment: FeedComment
    path: tuple[str, ...]
    post_tid: str
    parent_comment: FeedComment | None = None

    @property
    def level(self) -> int:
        """层级：0 为顶层评论，1 为一级子回复，依此类推。"""
        return max(len(self.path) - 1, 0)

    @property
    def key(self) -> str:
        """去重 / 尝试记录用的 key，形如 ``P_c1``、``P_c1_r2``、``P_c1_r2_r3``。"""
        parts = [f"{self.post_tid}_c{self.path[0]}"]
        parts.extend(f"_r{tid}" for tid in self.path[1:])
        return "".join(parts)

    @property
    def label(self) -> str:
        """日志里的短标识，形如 ``c1`` 或 ``c1/r2``。"""
        head = f"c{self.path[0]}"
        if self.level == 0:
            return head
        return head + "".join(f"/r{tid}" for tid in self.path[1:])

    @property
    def tid(self) -> str:
        """被回复对象的 tid（发送时作为 commentId）。"""
        return self.comment.tid

    @property
    def uin(self) -> int:
        """被回复对象的作者 QQ 号。"""
        return self.comment.uin

    def display_name(self) -> str:
        """展示用名称。"""
        return self.comment.display_name()


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
        reply_optin_checker: Callable[[str], bool] | None = None,
        opted_in_counter: Callable[[], int] | None = None,
    ) -> None:
        """初始化服务。

        Args:
            config: 插件配置。
            ai: AI 客户端。
            api: QQ空间接口。
            drafts: 草稿箱。
            reply_optin_checker: 可选的 ``(qq) -> bool``，判断对方是否接受过主动消息；
                与特权名单取并集（``allowed_uin``）。
            opted_in_counter: 可选的 ``() -> int``，用于在状态里显示已同意人数。
        """
        self.cfg = config
        self.ai = ai
        self.api = api
        self.drafts = drafts
        self._reply_optin = reply_optin_checker
        self._opted_in_counter = opted_in_counter
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
        # 「已尝试但没确认」的回复：同一条候选在 24 小时内不再重发，避免重复回复
        self.attempt_file = Path(config.data_dir) / "replied_attempts.json"
        self._attempts: dict[str, dict] = {}
        self.load_attempts()
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
        """处理单条说说：去重 -> 名单/同意检查 -> 点赞 -> 评论/转草稿。"""
        result.checked += 1

        if bool(self.cfg.interact_skip_self) and self_uin and post.uin == self_uin:
            result.skipped += 1
            return

        if not force and self.seen(post):
            result.skipped += 1
            return

        # 并集口径同样管住好友互动这条路径：只有特权名单里的人、或接受过主动消息的人，
        # 才会自动点赞 / 评论他的说说；两者都不是就只读不打扰（不写去重记录，
        # 等他以后 /私聊开 或进名单后还能补上）。
        if not self.allowed_uin(post.uin):
            result.gated += 1
            logger.info(
                f"[interact] {post.name or post.uin}（{post.uin}）既不在特权名单里、"
                "也没接受过主动消息，跳过点赞与评论"
            )
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
        而且用这个 id 确实能回复成功。因此这里只挡真正不可用的两种：
        完全没有 id，以及与说说 id 相同（后者说明解析错位）；
        **其余一律放行**，短数字 id 只写一行 debug 日志，绝不跳过。

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
        if value.isdigit() and len(value) < 6:
            logger.debug(f"评论 id（{value}）为短数字，仍按真实评论 id 使用")
        return ""

    @property
    def vip_uins(self) -> list[str]:
        """特权名单（配置项 ``interact_reply_uins``，面板里叫「特权名单」）。

        名单里的人无需任何同意即可被自动评论 / 回复：既包括他的说说，
        也包括他在自己说说下的评论与子回复。
        """
        values = self.cfg.interact_reply_uins or []
        return [str(item).strip() for item in values if str(item).strip()]

    @property
    def require_allowed(self) -> bool:
        """是否启用「必须在特权名单里、或接受过主动消息」这项检查。

        关闭时退回旧行为（谁都可以互动），用于排查或临时放开。
        """
        return bool(self.cfg.interact_reply_require_optin)

    def is_vip(self, uin: int | str) -> bool:
        """该 QQ 是否在特权名单里（字符串精确相等）。"""
        return str(uin).strip() in self.vip_uins

    def allowed_uin(self, uin: int | str) -> bool:
        """**并集**口径：特权名单 ∪ 已接受主动消息。

        - 在特权名单里：无需任何同意，直接放行；
        - 不在名单但已接受主动消息（用过 ``/私聊开``）：放行；
        - 两者都不是：不放行（不评论他的说说，也不回复他的评论）。

        Args:
            uin: 目标 QQ 号。

        Returns:
            允许互动时返回 True。
        """
        if not self.require_allowed:
            return True
        if self.is_vip(uin):
            return True
        if self._reply_optin is None:
            return False
        return bool(self._reply_optin(str(uin)))

    @property
    def opted_in_count(self) -> int:
        """已接受主动消息的人数（用于状态展示）。"""
        if self._opted_in_counter is None:
            return 0
        try:
            return int(self._opted_in_counter())
        except Exception as e:  # pragma: no cover - 统计失败不影响主流程
            logger.debug(f"统计已同意人数失败: {e}")
            return 0

    def reply_scope_text(self) -> str:
        """给状态与指令用的「谁能被互动」说明。"""
        if not self.require_allowed:
            return "未启用名单检查（所有评论者都可回复）"
        return (
            f"特权名单 {len(self.vip_uins)} 人 ∪ 已同意 {self.opted_in_count} 人"
            "（两者都不是则不互动）"
        )

    @staticmethod
    def reply_candidates(thread: FeedComment, post_tid: str) -> list[ReplyTarget]:
        """一条评论下所有可回复的对象（**带层级**），最新的在前。

        空间给父评论与子回复**各自独立编号**（都从 1 开始），所以候选身份不能只看
        tid 数字，必须带上层级路径：

        - 顶层评论：``{说说tid}_c{评论tid}``，日志标识 ``c1``；
        - 一级子回复：``{说说tid}_c{父评论tid}_r{子回复tid}``，日志标识 ``c1/r2``；
        - 更深一层（回复的回复）：继续拼 ``_r{tid}``。

        按 ``create_time`` 倒序排列，保证「对方刚回的那条」优先被处理；
        没有时间（0）的排在最后（``sorted`` 是稳定排序，时间相同的保持原顺序）。

        Args:
            thread: 顶层评论（含各层子回复）。
            post_tid: 说说 tid，拼进 key 与标识。

        Returns:
            ReplyTarget 列表，最新的在前。
        """
        targets = [ReplyTarget(comment=thread, path=(thread.tid,), post_tid=post_tid)]
        for reply in thread.replies:
            targets.extend(InteractService._targets_of(reply, (thread.tid,), post_tid))
        return sorted(
            targets,
            key=lambda item: item.comment.create_time,
            reverse=True,
        )

    @staticmethod
    def _targets_of(
        node: FeedComment, ancestors: tuple[str, ...], post_tid: str
    ) -> list[ReplyTarget]:
        """递归展开一个节点及其更深层的子回复。"""
        path = (*ancestors, node.tid)
        targets = [ReplyTarget(comment=node, path=path, post_tid=post_tid)]
        for child in node.replies:
            targets.extend(InteractService._targets_of(child, path, post_tid))
        return targets

    @staticmethod
    def has_own_reply(thread: FeedComment, target: ReplyTarget, self_uin: int) -> bool:
        """**这条候选**下面是否已经有一条我发出的、针对它的回复。

        规则刻意收紧，禁止跨层级比较（父评论 tid 与子回复 tid 可能同为 ``1``）：

        - 只认「我的回复的 ``parent_tid`` 等于该候选的 tid」这一条结构化证据；
        - 该回复还必须出现在**时间上不早于候选本身**（不可能回复一条还没出现的评论），
          否则同一 tid 的父评论旧回复会把新子回复误杀；
        - 不再使用「这条评论的 ``list_3`` 里有我的回复」这种宽松依据，
          归属判定不了时只靠精确 key（``replied_comments.json``）。

        Args:
            thread: 顶层评论（含各层子回复）。
            target: 本次准备回复的候选。
            self_uin: 自己的 QQ 号。

        Returns:
            已经回过这一条时返回 True。
        """
        if not self_uin:
            return False
        candidate_tid = str(target.comment.tid or "").strip()
        if not candidate_tid:
            return False
        for reply in thread.all_replies():
            if reply.uin != self_uin:
                continue
            if str(reply.parent_tid).strip() != candidate_tid:
                continue
            created = target.comment.create_time
            if created > 0 and reply.create_time > 0 and reply.create_time < created:
                # 我的回复比这条候选还早：它回的是同名 tid 的另一个层级
                continue
            return True
        return False

    @staticmethod
    def reply_key(post_tid: str, target: ReplyTarget) -> str:
        """回复去重键：说说 tid + 层级路径（``P_c1`` / ``P_c1_r2`` / ``P_c1_r2_r3``）。"""
        return target.key

    @staticmethod
    def _legacy_keys(post_tid: str, target: ReplyTarget) -> list[str]:
        """老版本写下的去重键（没有层级信息），只用于兼容读取。

        老键形如 ``{说说tid}_{tid}``，无法区分层级。为避免「父评论与子回复同号」
        这种正是本次要修的问题，**只在顶层评论候选上认同老键**；
        子回复候选一律以新键为准。
        """
        if target.level != 0:
            return []
        return [f"{post_tid}_{target.comment.tid}"]

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

    def replied(self, post_tid: str, target: ReplyTarget) -> bool:
        """该候选是否已经回复过（新键 + 老键兼容，见 ``_legacy_keys``）。"""
        if target.key in self._replied:
            return True
        return any(key in self._replied for key in self._legacy_keys(post_tid, target))

    def mark_replied(self, post_tid: str, target: ReplyTarget) -> None:
        """把该候选标记为已回复并落盘（回查确认后调用，写入的是带层级的新键）。"""
        if target.key not in self._replied:
            self._replied.append(target.key)
        self.save_replied()

    @property
    def replied_count(self) -> int:
        """累计已回复的评论条数。"""
        return len(self._replied)

    # ------------------------------------------------------------------
    # 「已尝试但没确认」的回复（防重复的关键一步）
    # ------------------------------------------------------------------

    def load_attempts(self) -> None:
        """加载「已尝试未确认」记录。"""
        self._attempts = {}
        if not self.attempt_file.exists():
            return
        try:
            raw = json.loads(self.attempt_file.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"回复尝试记录读取失败，已忽略: {e}")
            return
        if isinstance(raw, dict):
            for key, value in raw.items():
                if isinstance(value, dict):
                    self._attempts[str(key)] = value

    def save_attempts(self) -> None:
        """原子写入「已尝试未确认」记录，只保留最近 200 条。"""
        try:
            self.attempt_file.parent.mkdir(parents=True, exist_ok=True)
            items = sorted(
                self._attempts.items(),
                key=lambda pair: int(pair[1].get("time") or 0),
            )[-_ATTEMPT_LIMIT:]
            tmp = self.attempt_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(dict(items), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.attempt_file)
        except Exception as e:
            logger.error(f"回复尝试记录写入失败: {e}")

    def attempt_of(self, post_tid: str, target: ReplyTarget) -> dict:
        """取该候选的「已尝试未确认」记录（没有则返回空字典）。"""
        value = self._attempts.get(target.key)
        return value if isinstance(value, dict) else {}

    def attempt_pending(self, post_tid: str, target: ReplyTarget) -> bool:
        """该候选是否处在「已尝试未确认、24 小时内不再重发」的状态。"""
        record = self.attempt_of(post_tid, target)
        if not record:
            return False
        try:
            when = int(record.get("time") or 0)
        except (TypeError, ValueError):
            when = 0
        return (int(time.time()) - when) < _ATTEMPT_RETRY_SECONDS

    def mark_attempt(self, post_tid: str, target: ReplyTarget, reason: str) -> None:
        """记下「发出去但回查没确认」的一次尝试，24 小时内不再重发。"""
        self._attempts[target.key] = {
            "time": int(time.time()),
            "reason": str(reason or "")[:200],
            "label": target.label,
        }
        self.save_attempts()

    def clear_attempt(self, post_tid: str, target: ReplyTarget) -> None:
        """确认成功后清掉尝试记录（去重记录已经能挡住重复）。"""
        if self._attempts.pop(target.key, None):
            self.save_attempts()

    @property
    def pending_attempts(self) -> int:
        """当前仍在 24 小时观察期内、未确认的回复条数。"""
        now = int(time.time())
        count = 0
        for record in self._attempts.values():
            try:
                when = int(record.get("time") or 0)
            except (TypeError, ValueError):
                when = 0
            if (now - when) < _ATTEMPT_RETRY_SECONDS:
                count += 1
        return count

    def attempts_text(self) -> str:
        """给状态与指令用的「未确认回复」说明。"""
        pending = self.pending_attempts
        if not pending:
            return "无未确认的回复"
        return (
            f"有 {pending} 条回复发出后没回查确认，24 小时内不会重发，"
            "可稍后到空间里人工确认"
        )

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
            for target in self.reply_candidates(thread, post.tid):
                if result.replied >= limit:
                    return note

                result.checked += 1
                if target.uin == self_uin:
                    result.skipped += 1
                    self._log_verdict(post.tid, target, "跳过（自己发的）")
                    continue
                # 并集口径：必须在特权名单里，或接受过主动消息，否则不互动
                if not self.allowed_uin(target.uin):
                    result.skipped += 1
                    note = (
                        f"{target.display_name()}（{target.uin}）既不在特权名单里、"
                        "也没接受过主动消息，本轮不回复（可用 /私聊开 或填入特权名单）"
                    )
                    self._log_verdict(post.tid, target, "跳过（不在特权名单且未同意）")
                    continue
                if not target.comment.content.strip():
                    result.skipped += 1
                    self._log_verdict(post.tid, target, "跳过（评论没有正文）")
                    continue
                # 评论 id 不可用时先挡下来：拿错 id 去请求只会白打一次请求
                problem = self.comment_id_problem(post.tid, target.tid)
                if problem:
                    result.skipped += 1
                    note = problem
                    logger.warning(f"[reply] {post.tid} 下的{problem}")
                    continue
                # 三重保险：带层级的去重记录 -> 结构化归属 -> 已尝试未确认
                if self.replied(post.tid, target):
                    result.skipped += 1
                    self._log_verdict(post.tid, target, "跳过（已回复）")
                    continue
                # 只认「我的回复明确指向这一条」：父评论与子回复同号也不会互相误判
                if self.has_own_reply(thread, target, self_uin):
                    result.skipped += 1
                    note = f"{post.tid} 下的 {target.label} 已经有我的回复，跳过"
                    self._log_verdict(post.tid, target, "跳过（已有我的回复）")
                    continue
                if self.attempt_pending(post.tid, target):
                    result.skipped += 1
                    note = (
                        f"{post.tid} 下的 {target.label} 上一轮发出后没回查确认，"
                        "24 小时内不再重发"
                    )
                    self._log_verdict(
                        post.tid, target, "跳过（已尝试未确认，24 小时内不重发）"
                    )
                    continue

                try:
                    await self._reply_to_comment(post, thread, target, result)
                except Exception as e:
                    result.errors.append(f"{post.tid}/{target.label}: {e}")
                    self._log_verdict(post.tid, target, "失败（保留尝试记录）")
                # 同一条说说每轮最多回一条
                return f"{post.tid} 下已回复一条（同一条说说每轮最多回一条）"
        return note

    @staticmethod
    def _log_verdict(post_tid: str, target: ReplyTarget, verdict: str) -> None:
        """写一行「这条候选本轮怎么处置」，让重复发没发一眼可见。

        候选标识带层级（``c1`` / ``c1/r2``），空间里父评论与子回复各自从 1 编号，
        不带层级会出现两条都叫 ``1`` 的候选分不清。

        Args:
            post_tid: 说说 tid。
            target: 候选（带层级）。
            verdict: 处置结论。
        """
        logger.info(f"[reply] {post_tid}/{target.label}：{verdict}")

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
        self,
        post: FeedPost,
        thread: FeedComment,
        target: ReplyTarget,
        result: ReplyResult,
    ) -> None:
        """生成一条回复并直接发出。

        ``api.reply()`` 成功即代表**已回查确认**自己的回复出现在该候选下，
        因此只有这时才写去重记录与今日计数；如果发出去但回查没确认，
        就写一条「已尝试未确认」记录，24 小时内不再对这条候选重发——
        这是防止「同一条评论被重复回复」的关键一步。

        Args:
            post: 评论所在的说说。
            thread: 这条候选所属的顶层评论（用来把整段交流交给 AI）。
            target: 本次要回复的那一条（带层级：父评论或某层子回复）。
            result: 本轮汇总，用于累计结果与错误。
        """
        content = await self._generate_reply(post, thread, target.comment)

        # 回复一律直接发出：不生成草稿、也不需要用户确认。
        # 空间没有真正的嵌套回复：回复子回复时 commentId 必须填**线程顶层评论的 tid**，
        # 新回复会作为同一条父评论下的新子回复落地；填子回复自己的 tid 会被拒
        # （实测返回一小段页面且线程里没有新回复）。
        # commentUin 用被回复那一条的作者，这样对方能收到提醒；
        # 去重仍按候选层级（target.key，例如 P_c1_r2）记录，不受这里影响。
        resp = await self.api.reply(
            post.uin,
            post.tid,
            target.tid,
            target.uin,
            content,
            root_tid=target.path[0],
        )
        if not resp.ok:
            reason = str(resp.message or resp.code)
            self.mark_attempt(post.tid, target, reason)
            result.errors.append(f"回复评论 {target.label} 失败: {reason}")
            self._log_verdict(post.tid, target, "失败（保留尝试记录）")
            return

        result.replied += 1
        self.mark_replied(post.tid, target)
        self.clear_attempt(post.tid, target)
        self.count_reply()
        self._log_verdict(post.tid, target, "已回复（回查命中）")
        logger.info(
            f"已回复 {target.display_name()} 在 {post.tid} 下的 {target.label}"
            "（回查已确认）"
        )

    @staticmethod
    def _brief(text: str, limit: int = _BRIEF_LIMIT) -> str:
        """把正文压成一行并截断，用于放进提示词。"""
        value = " ".join(str(text or "").split())
        if len(value) > limit:
            return value[:limit] + "…"
        return value

    def thread_digest(
        self, post: FeedPost, thread: FeedComment, target: FeedComment
    ) -> str:
        """把**整段交流过程**整理成给 AI 的上下文。

        内容是：说说正文（截断）→ 父评论 → 该线程下全部子回复（按时间升序、
        标明说话人、我自己的回复标成「我」）→ 我之前已经说过的话 →
        本次要回复的是哪一条。

        Args:
            post: 评论所在的说说（自己的说说，作者即「我」）。
            thread: 顶层评论。
            target: 本次要回复的那一条。

        Returns:
            可直接作为提示词正文的多行文本。
        """
        self_uin = post.uin
        lines = [
            f"我的说说：{self._brief(post.text) or '（无正文）'}",
            "",
            "这条评论下的完整交流（按时间先后，标注「我」的是我自己说过的话）：",
            f"- 评论（{thread.display_name()}）：{thread.content}",
        ]
        said: list[str] = []
        for item in sorted(thread.replies, key=lambda one: one.create_time):
            if self_uin and item.uin == self_uin:
                lines.append(f"- 我：{item.content}")
                said.append(item.content)
            else:
                lines.append(f"- 回复（{item.display_name()}）：{item.content}")
        lines.append("")
        if said:
            lines.append("我之前已经说过的话（不要重复这些意思）：")
            lines.extend(f"- {text}" for text in said)
            lines.append("")
        kind = "评论" if str(target.tid) == str(thread.tid) else "回复"
        lines.append(
            f"本次要回复的是：{target.display_name()} 的这条{kind}「{target.content}」"
        )
        return "\n".join(lines)

    async def _generate_reply(
        self, post: FeedPost, thread: FeedComment, target: FeedComment
    ) -> str:
        """用 AI 生成一条评论回复（结合整段交流，不是只看最后一句）。

        Args:
            post: 评论所在的说说。
            thread: 顶层评论，用来提供整段交流。
            target: 本次要回复的那一条。

        Returns:
            回复正文。

        Raises:
            RuntimeError: AI 返回内容为空时抛出。
        """
        task = str(self.cfg.interact_reply_prompt or "").strip() or DEFAULT_REPLY_PROMPT
        limit = max(int(self.cfg.interact_reply_max_chars or 0), 1)

        text = await self.ai.chat(
            system_prompt=(
                f"{task}\n\n# 输出要求\n"
                f"只输出回复正文本身，不要引号、不要解释，不超过 {limit} 字。\n"
                "结合上面给出的整段交流来回应，不要只针对最后一句孤立作答；"
                "不要重复「我之前已经说过的话」里的意思，要接着往下说"
                "（可以补充、可以反问一句、也可以回应对方的情绪）；"
                "不要提及自己在查看评论记录、也不要暗示一直在关注对方。"
            ),
            prompt=self.thread_digest(post, thread, target),
            provider_id=str(self.cfg.llm_reply_provider_id or ""),
            feature="回复",
        )

        cleaned = text.strip().strip("\"'“”")
        cleaned = "".join(cleaned.split())
        if not cleaned:
            raise RuntimeError("AI 生成的回复内容为空")
        return cleaned[:limit]
