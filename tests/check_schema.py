"""用 AstrBot 自己的逻辑校验插件 _conf_schema.json 是否可用。

AstrBot 在加载插件时会读取 _conf_schema.json，并通过
``AstrBotConfig._config_schema_to_default_config`` 把 schema 转成默认配置。
该函数有两条硬约束：

1. 每个节点必须有 ``type``，且取值必须在 ``DEFAULT_VALUE_MAP`` 里，否则抛 TypeError；
2. ``object`` 类型会递归读 ``items``，所以嵌套项也要满足第 1 条。

脚本优先导入 AstrBot 源码里的真实实现（缺第三方依赖时自动打空壳桩），
找不到 AstrBot 源码时退回等价实现。

环境变量：
    ASTRBOT_REPO   本地 AstrBot 源码目录（默认取 <工作区>/_repos/AstrBot）
    PLUGIN_STORE   AstrBot 的插件目录，提供时会顺带对照其中其他插件的 schema

用法：
    python tests/check_schema.py
"""

from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

# Windows 控制台默认 GBK，打印中文与 ✅ / ❌ 会 UnicodeEncodeError，这里统一切到 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO_ROOT
ASTRBOT_REPO = Path(
    os.environ.get("ASTRBOT_REPO") or REPO_ROOT.parent / "_repos" / "AstrBot"
)
PLUGIN_STORE = (
    Path(os.environ["PLUGIN_STORE"]) if os.environ.get("PLUGIN_STORE") else None
)

FALLBACK_VALUE_MAP = {
    "int": 0,
    "float": 0.0,
    "bool": False,
    "string": "",
    "text": "",
    "list": [],
    "file": [],
    "object": {},
    "template_list": [],
    "dict": {},
}


def load_real_impl():
    """尝试加载 AstrBot 的真实 schema 转换函数。

    AstrBot 的部分依赖（如 deprecated）在本机测试环境里没有安装，
    这里为缺失的第三方模块安装空壳桩，从而仍然调用 AstrBot 的真实实现，
    而不是我自己复刻的版本。

    Returns:
        (函数, 类型映射, 描述) 三元组；失败时返回 None。
    """
    sys.path.insert(0, str(ASTRBOT_REPO))

    for _ in range(12):
        try:
            from astrbot.core.config.astrbot_config import AstrBotConfig
            from astrbot.core.config.default import DEFAULT_VALUE_MAP
        except ModuleNotFoundError as e:
            missing = e.name
            if not missing or missing.split(".")[0] in {"astrbot", ""}:
                print(f"[warn] 无法导入 AstrBot 实现：{e}")
                return None
            print(f"[info] 为缺失依赖 {missing} 安装空壳桩后重试")
            _install_stub(missing)
            continue
        except Exception as e:  # pragma: no cover - 取决于本地环境
            print(f"[warn] 无法导入 AstrBot 实现（{type(e).__name__}: {e}）")
            return None

        # 该方法定义为实例方法，但实现里不依赖 self，这里显式传 None 调用
        method = AstrBotConfig._config_schema_to_default_config

        def convert(schema: dict, _method=method) -> dict:
            return _method(None, schema)

        return convert, DEFAULT_VALUE_MAP, "AstrBot 真实实现"

    print("[warn] 依赖补齐次数超限，改用等价实现")
    return None


def _install_stub(module_name: str) -> None:
    """为缺失的第三方模块注入最小空壳，仅用于让 AstrBot 模块可导入。"""

    class _StubModule(types.ModuleType):
        def __getattr__(self, item):
            if item == "deprecated":
                return lambda *args, **kwargs: lambda func: func

            def _factory(*args, **kwargs):
                return lambda func: func

            return _factory

    sys.modules[module_name] = _StubModule(module_name)


def fallback_impl(schema: dict) -> dict:
    """AstrBot 逻辑的等价实现（仅用于兜底对照）。"""
    conf: dict = {}

    def parse(node: dict, target: dict) -> None:
        for key, value in node.items():
            if value["type"] not in FALLBACK_VALUE_MAP:
                raise TypeError(
                    f"不受支持的配置类型 {value['type']}。"
                    f"支持的类型有：{FALLBACK_VALUE_MAP.keys()}"
                )
            default = (
                value["default"]
                if "default" in value
                else FALLBACK_VALUE_MAP[value["type"]]
            )
            if value["type"] == "object":
                target[key] = {}
                parse(value["items"], target[key])
            else:
                target[key] = default

    parse(schema, conf)
    return conf


def audit(path: Path, convert, value_map: dict, label: str) -> bool:
    """校验一个插件的 schema。

    Args:
        path: _conf_schema.json 路径。
        convert: schema -> 默认配置的转换函数。
        value_map: AstrBot 支持的类型映射。
        label: 展示用的名称。

    Returns:
        是否全部通过。
    """
    print(f"\n--- {label}: {path}")
    if not path.is_file():
        print("  [skip] 文件不存在")
        return True

    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [FAIL] JSON 解析失败: {e}")
        return False

    if not isinstance(schema, dict) or not schema:
        print("  [FAIL] schema 不是非空对象")
        return False

    problems: list[str] = []
    missing_default: list[str] = []
    type_counter: dict[str, int] = {}
    specials: list[str] = []

    def walk(node: dict, prefix: str = "") -> None:
        for key, meta in node.items():
            name = f"{prefix}{key}"
            if not isinstance(meta, dict):
                problems.append(f"{name}: 节点不是对象")
                continue
            if "type" not in meta:
                problems.append(f"{name}: 缺少 type（AstrBot 会 KeyError）")
                continue
            node_type = meta["type"]
            type_counter[node_type] = type_counter.get(node_type, 0) + 1
            if node_type not in value_map:
                problems.append(f"{name}: 不支持的类型 {node_type}")
            if "default" not in meta:
                missing_default.append(f"{name}({node_type})")
            if "_special" in meta:
                specials.append(f"{name}={meta['_special']}")
            if node_type == "object":
                items = meta.get("items")
                if not isinstance(items, dict):
                    problems.append(f"{name}: object 类型缺少 items")
                    continue
                walk(items, f"{name}.")

    walk(schema)

    try:
        conf = convert(schema)
    except Exception as e:
        problems.append(f"转换默认配置抛异常: {type(e).__name__}: {e}")
        conf = {}

    print(f"  顶层字段: {len(schema)}｜展开后配置项: {len(conf)}")
    print(f"  类型分布: {dict(sorted(type_counter.items()))}")
    if specials:
        print(f"  _special: {', '.join(specials)}")
    if missing_default:
        print(
            f"  [warn] 未显式写 default（AstrBot 会用类型默认值）: {', '.join(missing_default)}"
        )
    if problems:
        for item in problems:
            print(f"  [FAIL] {item}")
        return False

    print("  [PASS] 类型全部合法、可正常生成默认配置")
    return True


def main() -> int:
    real = load_real_impl()
    if real:
        convert, value_map, source = real
    else:
        convert, value_map, source = fallback_impl, FALLBACK_VALUE_MAP, "等价实现"

    print(f"校验器来源: {source}")
    print(f"AstrBot 支持的类型: {list(value_map.keys())}")

    results = [
        audit(
            PLUGIN_DIR / "_conf_schema.json",
            convert,
            value_map,
            f"本插件 {PLUGIN_DIR.name}",
        )
    ]

    # 若提供了社区插件目录（PLUGIN_STORE），顺带对照其他插件，验证校验规则本身没问题
    if PLUGIN_STORE and PLUGIN_STORE.is_dir():
        for other in sorted(PLUGIN_STORE.iterdir()):
            schema_file = other / "_conf_schema.json"
            if other.is_dir() and schema_file.is_file() and other != PLUGIN_DIR:
                results.append(
                    audit(schema_file, convert, value_map, f"对照 {other.name}")
                )
    else:
        print("\n（未设置 PLUGIN_STORE，跳过与其他插件的对照）")

    # 额外展示本插件生成的默认配置，便于人工核对
    schema = json.loads((PLUGIN_DIR / "_conf_schema.json").read_text(encoding="utf-8"))
    conf = convert(schema)
    print("\n--- 本插件默认配置节选")
    for key in (
        "auto_publish_enabled",
        "publish_cron",
        "content_source",
        "llm_mode",
        "llm_provider_id",
        "llm_life_provider_id",
        "life_source",
        "life_inject_enabled",
        "interact_uins",
        "interact_like",
        "draft_enabled",
        "draft_umo",
        "notify_umo",
    ):
        if key in conf:
            print(f"  {key} = {conf[key]!r}")
    pool = conf.get("life_pool")
    if isinstance(pool, dict):
        print(
            "  life_pool（object 递归展开）-> "
            f"{[f'{k}:{len(v)}项' for k, v in pool.items()]}"
        )

    ok = all(results)
    print("\n" + ("全部通过 ✅" if ok else "存在问题 ❌"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
