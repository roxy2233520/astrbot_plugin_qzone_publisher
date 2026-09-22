"""把插件打包成可直接在 AstrBot 面板「从文件安装」的 zip。

用法：
    python tools/make_release_zip.py                 # 输出到插件目录的上一级
    python tools/make_release_zip.py --out D:/x.zip  # 指定输出路径

包内结构（AstrBot 会自动把单层顶层目录的内容上移，与从仓库安装一致）：

    astrbot_plugin_qzone_publisher/
      metadata.yaml
      main.py
      _conf_schema.json
      core/...
      tests/...
      tools/...

只打包需要发布的内容：自动排除 __pycache__、.git、.ruff_cache、虚拟环境、
编辑器目录、日志、运行期数据目录（data/）与误放的压缩包等，避免把开发垃圾带进安装包。
"""

from __future__ import annotations

import argparse
import hashlib
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_NAME = REPO_ROOT.name

EXCLUDE_DIRS = {
    ".git",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    ".venv",
    "venv",
    ".idea",
    ".vscode",
    ".mypy_cache",
    # 运行期数据目录：插件的数据由 AstrBot 存在 <数据目录>/plugin_data 下，
    # 插件目录里出现的 data/ 只可能是本地调试残留（如文转图模板），不应进包。
    "data",
}
EXCLUDE_FILES = {".DS_Store", "Thumbs.db", "desktop.ini", ".env", ".coverage"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".log", ".tmp", ".zip", ".bak"}

REQUIRED_FILES = ("metadata.yaml", "main.py", "_conf_schema.json", "logo.png")


def read_version() -> str:
    """从 metadata.yaml 读版本号，用于命名压缩包。"""
    try:
        for line in (
            (REPO_ROOT / "metadata.yaml").read_text(encoding="utf-8").splitlines()
        ):
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip().strip("'\"") or "0.0.0"
    except Exception:
        pass
    return "0.0.0"


def collect_files() -> list[Path]:
    """收集需要打包的文件（相对仓库根目录，已排序）。"""
    files: list[Path] = []
    for path in sorted(REPO_ROOT.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(REPO_ROOT)
        if any(part in EXCLUDE_DIRS for part in relative.parts[:-1]):
            continue
        if path.name in EXCLUDE_FILES or path.suffix.lower() in EXCLUDE_SUFFIXES:
            continue
        files.append(relative)
    return files


def build(out_path: Path) -> Path:
    """构建 zip。

    Args:
        out_path: 输出 zip 路径。

    Returns:
        实际写出的 zip 路径。

    Raises:
        SystemExit: 缺少必需文件时抛出。
    """
    missing = [name for name in REQUIRED_FILES if not (REPO_ROOT / name).is_file()]
    if missing:
        raise SystemExit(f"缺少必需文件，无法打包：{', '.join(missing)}")

    files = collect_files()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    with zipfile.ZipFile(
        out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9
    ) as archive:
        for relative in files:
            # 统一用 / 作为分隔符：zip 规范如此，Linux 上的 AstrBot 也能正确解压
            arcname = f"{PLUGIN_NAME}/{relative.as_posix()}"
            archive.write(REPO_ROOT / relative, arcname)

    print(f"打包完成：{out_path}")
    print(f"  插件目录名: {PLUGIN_NAME}/")
    print(f"  文件数:     {len(files)}")
    print(f"  体积:       {out_path.stat().st_size / 1024:.1f} KB")
    sha256 = hashlib.sha256(out_path.read_bytes()).hexdigest()
    print(f"  SHA256:     {sha256}")
    return out_path


def main() -> None:
    """命令行入口。"""
    parser = argparse.ArgumentParser(description="打包 AstrBot 插件 zip")
    parser.add_argument("--out", type=Path, default=None, help="输出 zip 路径")
    args = parser.parse_args()

    default_out = REPO_ROOT.parent / f"{PLUGIN_NAME}-v{read_version()}.zip"
    build(args.out or default_out)


if __name__ == "__main__":
    main()
