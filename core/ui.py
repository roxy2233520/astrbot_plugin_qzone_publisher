"""用户可见文本的统一排版。

一份内容、两种渲染：

- **纯文本版**（默认，发到 QQ 的消息）：只使用一定能在 QQ 里正常显示的字符做层次——
  【】标签、「」引号、`` · `` 与缩进、``├─ └─`` 树形线、``────────`` 分隔线，
  以及四个状态符号（``✅`` 成功 / ``⚠️`` 提醒 / ``❌`` 失败 / ``📌`` 信息）。
  **不包含任何 Markdown 标记**：QQ 不渲染 Markdown，写成 ``**加粗**`` 只会显示星号。
- **Markdown 版**（回执图 t2i）：同一份内容把【】标签渲染成加粗标题、
  把键值行的名称渲染成真加粗，交给 AstrBot 的渲染器排版。

两版由同一份 :class:`Section` 列表生成，因此内容永远一致，只有标记不同。

排版约定
--------

- 区块标题：``✅【发布成功】``；标签用 2~4 字中文名。
- 键值行：``· 名称：值``，同一区块内统一缩进两个空格；相关的值放在同一行或紧邻两行。
- 指令提示：指令名用「/空间发布」这种直角引号包裹，一行内最多两条指令。
- 分隔线：``────────`` 只用于多区块输出（如状态），区块之间一条。
- 长度：单条回执不超过 :data:`LIMIT_RECEIPT` 行，状态类每个区块不超过
  :data:`LIMIT_BLOCK_LINES` 行，超出时截断并给出提示（完整内容可由回执图承载）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ----------------------------------------------------------------------
# 状态符号与排版常量（只用这四个符号，不要引入其它 emoji）
# ----------------------------------------------------------------------

ICON_OK = "✅"  # 成功
ICON_WARN = "⚠️"  # 需要注意
ICON_FAIL = "❌"  # 失败
ICON_INFO = "📌"  # 信息 / 状态 / 待办

ICONS: tuple[str, ...] = (ICON_OK, ICON_WARN, ICON_FAIL, ICON_INFO)

# 分隔线：8 个半角破折号
DIVIDER = "────────"
# 键值行前缀与缩进
BULLET = "·"
INDENT = "  "
# 超出长度上限时的提示
TRUNCATED_HINT = "  · 内容过长，已省略其余部分"

# 单条回执的行数上限
LIMIT_RECEIPT = 12
# 状态类输出中每个区块的行数上限
LIMIT_BLOCK_LINES = 8

_MD_MARKS = ("**", "__", "`", "#", "~~")


def has_markdown(text: str) -> bool:
    """判断文本里是否含 Markdown 标记。

    用于自检「纯文本版不能被 QQ 原样显示成星号」这类问题。

    Args:
        text: 待检查的文本。

    Returns:
        含 ``**`` / ``__`` / 反引号 / ``#`` / ``~~`` 时返回 True。
    """
    raw = str(text or "")
    if any(mark in raw for mark in _MD_MARKS):
        return True
    # 列表项写法（行首 - / * / 数字.）也算 Markdown
    return bool(re.search(r"(?m)^\s*(?:[-*+]\s|\d+\.\s)", raw))


def quote_command(name: str) -> str:
    """把指令名包成「/空间发布」的形式。

    Args:
        name: 指令名，可带或不带前导斜杠。

    Returns:
        直角引号包裹的指令名。
    """
    text = str(name or "").strip()
    if text and not text.startswith("/"):
        text = "/" + text
    return f"「{text}」"


def command_line(*names: str) -> str:
    """把指令拼成一行提示，一行最多两条。

    Args:
        names: 指令名。

    Returns:
        形如 ``· 指令：「/空间确认」或「/空间放弃」`` 的文本；名称为空时返回空串。
    """
    items = [item for item in (str(name).strip() for name in names) if item]
    if not items:
        return ""
    rendered = [quote_command(item) for item in items]
    if len(rendered) == 1:
        return f"{INDENT}{BULLET} 指令：{rendered[0]}"
    head = "、".join(rendered[:2])
    if len(rendered) > 2:
        head += f"（另有 {len(rendered) - 2} 条见说明）"
    return f"{INDENT}{BULLET} 指令：{head}"


def kv(name: str, value: object, *, indent: int = 1) -> str:
    """构造一行键值（纯文本版）。

    Args:
        name: 名称，2~6 字为宜。
        value: 值；None 与前后的空白会被忽略。
        indent: 缩进层级（每层两个空格）。

    Returns:
        形如 ``  · 发布时间：每天 08:30`` 的文本。
    """
    text = "" if value is None else str(value).strip()
    if not text:
        text = "（空）"
    return f"{INDENT * max(indent, 0)}{BULLET} {name}：{text}"


def kv_markdown(name: str, value: object, *, indent: int = 1) -> str:
    """构造一行键值（Markdown 版，名称为真加粗）。"""
    text = "" if value is None else str(value).strip()
    if not text:
        text = "（空）"
    return f"{INDENT * max(indent, 0)}{BULLET} **{name}**：{text}"


def branch(name: str, value: object, *, last: bool = False) -> str:
    """构造一行树形分支（用于目标列表等并列内容）。

    Args:
        name: 分支名。
        value: 分支值。
        last: 是否是最后一项（决定用 ``└─`` 还是 ``├─``）。

    Returns:
        形如 ``  └─ 123456：已同意`` 的文本。
    """
    joint = "└─" if last else "├─"
    return f"{INDENT}{joint} {name}：{value}"


def truncate_lines(text: str, max_lines: int, *, hint: str = TRUNCATED_HINT) -> str:
    """按行数截断文本，超出时补一行提示。

    Args:
        text: 原始文本。
        max_lines: 允许的最大行数（含提示行）；小于等于 0 表示不限制。
        hint: 截断提示行。

    Returns:
        截断后的文本。
    """
    if max_lines <= 0:
        return text
    lines = str(text or "").splitlines()
    if len(lines) <= max_lines:
        return text
    kept = lines[: max(max_lines - 1, 1)]
    return "\n".join([*kept, hint])


@dataclass(slots=True)
class Section:
    """一个区块：状态符号 +【标签】+ 若干行内容。

    Attributes:
        icon: 状态符号，取值见 :data:`ICONS`。
        label: 标签，2~4 字中文名。
        lines: 正文行（已排版好的字符串）。
        title_extra: 附加在标签后的补充说明，例如 ``（12:00-13:30）``。
    """

    icon: str
    label: str
    lines: list[str] = field(default_factory=list)
    title_extra: str = ""

    @property
    def title(self) -> str:
        """纯文本版标题。"""
        return f"{self.icon}【{self.label}】{self.title_extra}"

    @property
    def title_markdown(self) -> str:
        """Markdown 版标题（真加粗）。"""
        return f"**{self.icon}【{self.label}】**{self.title_extra}"

    def add(self, *lines: str) -> Section:
        """追加若干行，返回自身以便链式调用。"""
        for line in lines:
            text = "" if line is None else str(line)
            if text.strip():
                self.lines.append(text)
        return self

    def add_kv(self, name: str, value: object, *, indent: int = 1) -> Section:
        """追加一行键值。"""
        return self.add(kv(name, value, indent=indent))

    def text(self, *, max_lines: int = 0) -> str:
        """渲染纯文本版。"""
        body = "\n".join(self.lines)
        rendered = f"{self.title}\n{body}" if body else self.title
        return truncate_lines(rendered, max_lines)

    def markdown(self, *, max_lines: int = 0) -> str:
        """渲染 Markdown 版（键值名称加粗）。"""
        body = "\n".join(_to_markdown_line(line) for line in self.lines)
        rendered = f"{self.title_markdown}\n{body}" if body else self.title_markdown
        return truncate_lines(rendered, max_lines)

    @property
    def line_count(self) -> int:
        """区块行数（含标题）。"""
        return 1 + len(self.lines)


def _to_markdown_line(line: str) -> str:
    """把纯文本行转成 Markdown 行（键值行的名称加粗）。"""
    text = str(line)
    match = re.match(r"^(\s*· )([^：]{1,12})(：.*)$", text)
    if match:
        return f"{match.group(1)}**{match.group(2)}**{match.group(3)}"
    return text


def compose(
    sections: list[Section],
    *,
    markdown: bool = False,
    divider: bool = False,
    max_lines: int = 0,
    block_limit: int = 0,
) -> str:
    """把多个区块拼成一份完整回执。

    Args:
        sections: 区块列表。
        markdown: True 时输出 Markdown 版。
        divider: True 时在区块之间插入分隔线（多区块状态类输出使用）。
        max_lines: 整份文本的行数上限；0 表示不限制。
        block_limit: 每个区块的行数上限；0 表示不限制。

    Returns:
        拼好的文本。
    """
    rendered = [
        (
            item.markdown(max_lines=block_limit)
            if markdown
            else item.text(max_lines=block_limit)
        )
        for item in sections
    ]
    joiner = f"\n{DIVIDER}\n" if divider else "\n"
    return truncate_lines(joiner.join(rendered), max_lines)


def pair(
    sections: list[Section],
    *,
    divider: bool = False,
    max_lines: int = 0,
    block_limit: int = 0,
) -> tuple[str, str]:
    """同时产出纯文本版与 Markdown 版。

    Args:
        sections: 区块列表。
        divider: 是否在区块之间插入分隔线。
        max_lines: 整份文本的行数上限。
        block_limit: 每个区块的行数上限。

    Returns:
        二元组 (纯文本, Markdown)。
    """
    return (
        compose(
            sections,
            markdown=False,
            divider=divider,
            max_lines=max_lines,
            block_limit=block_limit,
        ),
        compose(
            sections,
            markdown=True,
            divider=divider,
            max_lines=max_lines,
            block_limit=block_limit,
        ),
    )


def receipt(
    label: str,
    lines: list[str] | None = None,
    *,
    icon: str = ICON_OK,
    title_extra: str = "",
) -> tuple[str, str]:
    """单区块回执的快捷入口。

    Args:
        label: 区块标签。
        lines: 正文行。
        icon: 状态符号。
        title_extra: 标签后的补充说明。

    Returns:
        二元组 (纯文本, Markdown)。
    """
    item = Section(
        icon=icon, label=label, lines=list(lines or []), title_extra=title_extra
    )
    return pair([item], max_lines=LIMIT_RECEIPT)


def plain_receipt(
    label: str,
    lines: list[str] | None = None,
    *,
    icon: str = ICON_OK,
    title_extra: str = "",
) -> str:
    """只要纯文本版的单区块回执（多数指令回执使用）。"""
    return receipt(label, lines, icon=icon, title_extra=title_extra)[0]


def status_block(
    label: str,
    lines: list[str] | None = None,
    *,
    icon: str = ICON_INFO,
) -> Section:
    """状态类输出的一个区块（自带每区块行数上限）。"""
    item = Section(icon=icon, label=label, lines=list(lines or []))
    if item.line_count > LIMIT_BLOCK_LINES:
        item.lines = truncate_lines(
            "\n".join(item.lines), LIMIT_BLOCK_LINES - 1
        ).splitlines()
    return item


def plain(text: str) -> str:
    """把任意文本里的 Markdown 标记降级为纯文本。

    仅用于兜底：外部内容（如 AI 返回的说明）若带了 Markdown 标记，
    发到 QQ 前统一去掉，避免出现裸露的星号与井号。

    Args:
        text: 原始文本。

    Returns:
        去掉 ``**`` / ``__`` / 反引号 / 行首 ``#`` 后的文本。
    """
    result = str(text or "")
    result = result.replace("**", "").replace("__", "").replace("~~", "")
    result = result.replace("`", "")
    result = re.sub(r"(?m)^\s*#{1,6}\s*", "", result)
    return result
