# 测试与检查脚本

这些脚本不需要安装 AstrBot，也不依赖你的私人配置，可以直接在本地或 CI 里跑。

## 依赖

```bash
pip install aiohttp apscheduler pyyaml
```

> `aiohttp` 与 `apscheduler` 是 AstrBot 自带的库，插件运行时也需要它们；
> `pyyaml` 只在元数据检查脚本里用到。

## 脚本一览

| 脚本 | 作用 |
| :--- | :--- |
| `run_tests.py` | 主自测套件（168 项断言）。用桩模块替换 AstrBot 运行时，并起本地假 QQ空间服务与假 OpenAI 兼容接口，端到端验证发布、上传图片、登录失效重试、好友说说互动、草稿确认、AI 分流等链路 |
| `check_metadata.py` | 按 AstrBot 安装器的真实规则校验 `metadata.yaml` 与必需文件；附带**隐私体检**（默认配置里不许出现个人 QQ 号、本机绝对路径） |
| `check_schema.py` | 调用 AstrBot 真实的 `_config_schema_to_default_config` 校验 `_conf_schema.json`（类型白名单、object 递归、默认值生成） |
| `check_logo.py` | 解码 `logo.png` 校验结构与关键像素，确认 `tools/make_logo.py` 的产物没坏 |
| `probe_astrbot_api.py` | 从 GitHub 逐版本拉取 AstrBot 源码，验证本插件用到的 API 从哪个版本开始存在（用于支撑 `metadata.yaml` 里的 `astrbot_version` 声明） |

## 运行

```bash
python tests/run_tests.py
python tests/check_metadata.py
python tests/check_schema.py
python tests/check_logo.py
python tests/probe_astrbot_api.py   # 需要联网
```

## 可选环境变量

| 变量 | 说明 |
| :--- | :--- |
| `ASTRBOT_REPO` | 本地 AstrBot 源码目录，用于 `check_schema.py` 调用真实实现；不设置时自动退回等价实现 |
| `PLUGIN_STORE` | AstrBot 的插件目录（如 `AstrBot/data/plugins`），设置后会顺带对照其中其他插件的 schema |

## 注意

- `check_metadata.py` 在 `metadata.yaml` 仍是 `your-github-name` 占位符时会**故意失败**，
  提醒你上传前替换成真实仓库地址与作者名。
- `run_tests.py` 全部使用临时目录，不会写入你的 AstrBot 数据目录。
- 代码风格由仓库根目录的 `ruff.toml` 定义（`ruff check .` / `ruff format --check .`），
  中文全角标点相关的 `RUF001-003` 已关闭，`BLE001`/`DTZ` 因插件需要兜异常与按本地时区调度而关闭。
