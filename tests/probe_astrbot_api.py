"""探测本插件用到的 AstrBot API 最早出现在哪个版本。

做法：从 GitHub raw 拉取指定 tag 的几个关键文件，检查 API 是否已存在。
用于给 metadata.yaml 的 astrbot_version 一个**有依据**的取值，而不是凭感觉写。

用法：python probe_astrbot_api.py
"""

from __future__ import annotations

import re
import sys
import urllib.error
import urllib.request

TAGS = ["v4.16.0", "v4.18.0", "v4.20.0", "v4.22.0", "v4.26.0", "v4.28.1"]

# 文件路径 -> 需要在该文件里出现的 API 正则
#
# 分两组，因为联网素材依赖的 API 可能比核心功能更晚出现：
#   CORE_PROBES —— 发布 / 定时 / 草稿 / 互动 所需
#   WEB_PROBES  —— 联网素材（接入 AstrBot 自带联网搜索）所需
CORE_PROBES = {
    "astrbot/core/star/star_tools.py": [r"def get_data_dir\(\s*cls,\s*plugin_name"],
    "astrbot/core/message/components.py": [r"async def convert_to_base64"],
    "astrbot/core/star/filter/command.py": [r"class GreedyStr"],
    "astrbot/core/star/context.py": [
        r"def get_provider_by_id",
        r"def get_using_provider",
        r"def register_web_api",
    ],
    "astrbot/core/star/register/star_handler.py": [r"def register_on_llm_request"],
    "astrbot/core/platform/manager.py": [r"def get_insts"],
    "astrbot/core/config/astrbot_config.py": [r"_config_schema_to_default_config"],
}

WEB_PROBES = {
    "astrbot/core/tools/web_search_tools.py": [
        r"def normalize_legacy_web_search_config",
    ],
    "astrbot/core/provider/func_tool_manager.py": [r"def get_builtin_tool"],
    "astrbot/core/star/context.py": [r"def get_llm_tool_manager"],
}

PROBES = {**CORE_PROBES, **WEB_PROBES}

RAW = "https://raw.githubusercontent.com/AstrBotDevs/AstrBot/{tag}/{path}"


def fetch(tag: str, path: str) -> str | None:
    """抓取指定 tag 下的文件内容，404 返回 None。"""
    url = RAW.format(tag=tag, path=path)
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        print(f"    ! {path} HTTP {e.code}")
        return None
    except Exception as e:
        print(f"    ! {path} 抓取失败: {e}")
        return None


def main() -> int:
    """逐个 tag 探测，打印哪些 API 已具备。"""
    print(f"{'tag':<10} {'结果':<8} 缺失的 API")
    print("-" * 78)
    verdicts: dict[str, list[str]] = {}

    for tag in TAGS:
        missing: list[str] = []
        for path, patterns in PROBES.items():
            text = fetch(tag, path)
            if text is None:
                missing.append(f"{path}(文件不存在)")
                continue
            for pattern in patterns:
                if not re.search(pattern, text):
                    missing.append(pattern)
        verdicts[tag] = missing
        state = "OK" if not missing else "缺"
        print(f"{tag:<10} {state:<8} {', '.join(missing)[:60] if missing else '-'}")

    ok_tags = [tag for tag, missing in verdicts.items() if not missing]
    if ok_tags:
        print(f"\n全部 API 都具备的最低测试版本: {ok_tags[0]}（>= 该版本可安全声明）")
        return 0
    print("\n没有任何 tag 全部通过，需要降低 API 依赖")
    return 1


if __name__ == "__main__":
    sys.exit(main())
