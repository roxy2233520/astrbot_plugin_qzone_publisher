"""校验插件元数据与仓库规范性，并做一次隐私体检。

对齐 AstrBot 安装器的真实校验规则（`star_manager.py` / `updater.py`）：
- 仓库根目录必须有 metadata.yaml / metadata.yml，UTF-8，≤1MB；
- name、desc、version、author 四个字段必须是非空字符串；
- name 会被用作安装目录名与 Python 包名，必须是合法标识符。

额外检查一些「公开仓库容易踩的坑」：
- 插件名是否以 astrbot_plugin_ 开头（官方推荐）；
- version 是否为不带 v 前缀的语义化版本；
- astrbot_version 是否为合法 PEP 440 版本范围；
- support_platforms 取值是否在官方支持列表内；
- logo.png 是否存在、是否 1:1；
- **隐私体检**：配置默认值与代码里是否残留个人 QQ 号、绝对路径等。

用法：
    python tests/check_metadata.py
"""

from __future__ import annotations

import re
import struct
import sys
from pathlib import Path

# Windows 控制台默认 GBK，打印中文与 ✅ 会 UnicodeEncodeError，这里统一切到 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent

REQUIRED_FIELDS = ("name", "desc", "version", "author")
METADATA_MAX_BYTES = 1024 * 1024

# 来自官方文档 docs/zh/dev/star/plugin-new.md 的 ADAPTER_NAME_2_TYPE key
KNOWN_PLATFORMS = {
    "aiocqhttp",
    "qq_official",
    "qq_official_webhook",
    "telegram",
    "wecom",
    "wecom_ai_bot",
    "lark",
    "dingtalk",
    "discord",
    "slack",
    "kook",
    "vocechat",
    "weixin_official_account",
    "weixin_oc",
    "satori",
    "misskey",
    "line",
    "matrix",
    "mattermost",
}

SEMVER = re.compile(r"^\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.\-]+)?$")
VERSION_SPEC = re.compile(r"^[<>=!~]=?\s*\d+(\.\d+)*(\s*,\s*[<>=!~]=?\s*\d+(\.\d+)*)*$")
WIN_PATH = re.compile(r"[A-Za-z]:\\\\?[\w\\\-. ]{2,}")
QQ_LIKE = re.compile(r"\b\d{5,12}\b")

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, message: str) -> None:
    """记录并打印一条检查结果。"""
    RESULTS.append((ok, message))
    print(f"  {'PASS' if ok else 'FAIL'}  {message}")


def load_yaml(path: Path) -> dict:
    """读取 YAML（AstrBot 自带 pyyaml，这里同样依赖它）。"""
    try:
        import yaml
    except ImportError:
        print("  需要 pyyaml：python -m pip install pyyaml")
        raise SystemExit(2) from None
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def check_metadata() -> dict:
    """检查 metadata.yaml 是否满足 AstrBot 安装要求。"""
    print("\n[1] metadata.yaml")
    candidates = [ROOT / "metadata.yaml", ROOT / "metadata.yml"]
    path = next((item for item in candidates if item.is_file()), None)
    if path is None:
        check(
            False,
            "仓库根目录缺少 metadata.yaml / metadata.yml（AstrBot 无法识别为插件）",
        )
        return {}

    check(
        path.stat().st_size <= METADATA_MAX_BYTES,
        f"{path.name} 大小 {path.stat().st_size} 字节 ≤ 1MB",
    )

    metadata = load_yaml(path)
    if not isinstance(metadata, dict):
        check(False, f"{path.name} 不是 YAML 对象")
        return {}

    for field in REQUIRED_FIELDS:
        value = metadata.get(field)
        check(
            isinstance(value, str) and bool(value.strip()),
            f"必需字段 {field} 为非空字符串",
        )

    name = str(metadata.get("name") or "")
    check(
        name.isidentifier(), f"name「{name}」是合法 Python 标识符（会用作目录名与包名）"
    )
    check(
        name.startswith("astrbot_plugin_"), "name 以 astrbot_plugin_ 开头（官方推荐）"
    )
    check(name == name.lower(), "name 全部小写（官方推荐）")

    version = str(metadata.get("version") or "")
    check(bool(SEMVER.match(version)), f"version「{version}」是语义化版本且不带 v 前缀")

    spec = metadata.get("astrbot_version")
    if spec is None:
        print("  skip  未声明 astrbot_version（可选）")
    else:
        check(
            isinstance(spec, str) and bool(VERSION_SPEC.match(spec.strip())),
            f"astrbot_version「{spec}」是合法 PEP 440 范围",
        )

    platforms = metadata.get("support_platforms")
    if platforms is None:
        print("  skip  未声明 support_platforms（可选）")
    elif not isinstance(platforms, list):
        check(False, "support_platforms 必须是列表")
    else:
        unknown = [item for item in platforms if item not in KNOWN_PLATFORMS]
        check(not unknown, f"support_platforms 取值合法（{', '.join(platforms)}）")

    repo = metadata.get("repo")
    if isinstance(repo, str) and repo.strip():
        check(
            repo.startswith(("http://", "https://")),
            f"repo 是 http(s) 地址：{repo}",
        )
        if "your-github-name" in repo:
            check(False, "repo 仍是占位符 your-github-name，上传前请替换为真实仓库地址")
    else:
        check(False, "repo 未填写（AstrBot 的更新检查需要它）")

    author = str(metadata.get("author") or "")
    if "your-github-name" in author:
        check(False, "author 仍是占位符 your-github-name，上传前请替换")

    return metadata


def check_files() -> None:
    """检查插件必需文件与 logo。"""
    print("\n[2] 必需文件与 Logo")
    check((ROOT / "main.py").is_file(), "存在 main.py（AstrBot 从这里加载插件类）")
    check(
        (ROOT / "_conf_schema.json").is_file(), "存在 _conf_schema.json（配置面板来源）"
    )
    check((ROOT / "README.md").is_file(), "存在 README.md")
    check((ROOT / "LICENSE").is_file(), "存在 LICENSE")
    check((ROOT / "CHANGELOG.md").is_file(), "存在 CHANGELOG.md")

    logo = ROOT / "logo.png"
    if not logo.is_file():
        print("  skip  未提供 logo.png（可选，但插件市场卡片会更好看）")
        return

    data = logo.read_bytes()
    ok_header = data[:8] == b"\x89PNG\r\n\x1a\n"
    check(ok_header, "logo.png 是合法 PNG 文件头")
    if ok_header:
        width, height = struct.unpack(">II", data[16:24])
        check(width == height, f"logo.png 长宽比 1:1（{width}x{height}）")
        check(
            width <= 512, f"logo.png 尺寸 {width}x{height} 不过大（官方推荐 256x256）"
        )
    check(
        logo.stat().st_size <= 1024 * 1024,
        f"logo.png 大小 {logo.stat().st_size} 字节 ≤ 1MB",
    )


def check_privacy() -> None:
    """隐私体检：默认配置与代码里不应残留个人信息。"""
    print("\n[3] 隐私体检（默认值不应带个人信息）")
    import json

    schema_path = ROOT / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    findings: list[str] = []

    def walk(node: dict, prefix: str = "") -> None:
        for key, meta in node.items():
            if not isinstance(meta, dict):
                continue
            default = meta.get("default")
            if isinstance(default, str) and default and QQ_LIKE.search(default):
                findings.append(f"{prefix}{key} 默认值含长数字串: {default!r}")
            if isinstance(default, str) and WIN_PATH.search(default):
                findings.append(f"{prefix}{key} 默认值含本机绝对路径: {default!r}")
            if isinstance(default, list):
                for item in default:
                    if isinstance(item, str) and (
                        WIN_PATH.search(item) or QQ_LIKE.search(item)
                    ):
                        findings.append(f"{prefix}{key} 默认值含可疑项: {item!r}")
            if meta.get("type") == "object" and isinstance(meta.get("items"), dict):
                walk(meta["items"], f"{prefix}{key}.")

    walk(schema)
    check(not findings, "配置默认值不含 QQ 号 / 本机绝对路径")

    code_hits: list[str] = []
    this_file = Path(__file__).resolve()
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        # 跳过扫描器自身：它的规则里本来就要写出这些模式
        if path.resolve() == this_file:
            continue
        for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if WIN_PATH.search(line):
                code_hits.append(f"{path.relative_to(ROOT)}:{index}")
    check(
        not code_hits,
        f"代码里没有写死的 Windows 绝对路径（{', '.join(code_hits) or '无'}）",
    )

    for detail in findings:
        print(f"        · {detail}")


def main() -> int:
    """依次执行全部检查。"""
    print("=" * 62)
    print(f"检查仓库: {ROOT}")
    print("=" * 62)
    metadata = check_metadata()
    check_files()
    check_privacy()

    failed = [message for ok, message in RESULTS if not ok]
    print("\n" + "=" * 62)
    print(f"通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
    for message in failed:
        print(f"  - {message}")
    if failed:
        print(
            "\n提示：占位符（your-github-name）在上传前必须替换；其余 FAIL 建议修复后再发布。"
        )
        return 1
    print("元数据与仓库规范检查通过 ✅")
    _ = metadata
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
