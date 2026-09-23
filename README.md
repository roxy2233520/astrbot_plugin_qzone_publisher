# astrbot_plugin_qzone_publisher

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.16%2C%3C5-2E7DF7)](https://github.com/AstrBotDevs/AstrBot)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-292%20passed-2ea44f)](tests/)
[![CI](https://github.com/roxy2233520/astrbot_plugin_qzone_publisher/actions/workflows/ci.yml/badge.svg)](https://github.com/roxy2233520/astrbot_plugin_qzone_publisher/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/roxy2233520/astrbot_plugin_qzone_publisher)](https://github.com/roxy2233520/astrbot_plugin_qzone_publisher/releases)

一个给 [AstrBot](https://github.com/AstrBotDevs/AstrBot) 用的 **QQ空间定时发布** 插件。

**不需要手动抓 Cookie**：复用 AstrBot 里 aiocqhttp(OneBot) 的登录态，
调用 `get_cookies` 取回 `user.qzone.qq.com` 域的 Cookie。

主要能力：定时发说说、AI 写文案、一体化生活日程、自动读/赞/评好友说说（默认只读）、
草稿确认模式。所有涉及内容生成的地方都走统一 AI 层。

> 插件默认**不会自动发任何内容**（定时发布、点赞、评论、草稿确认的默认值全是关闭或只读），

---

## 功能

| 模块 | 能力 |
| :--- | :--- |
| 登录 | 自动向 OneBot 取空间域 Cookie，免抓包；支持手动 Cookie 兜底；登录态失效自动重取并重试一次 |
| 发布 | `/空间发布` 立即发说说，消息里带的图片会一起上传发布（最多 9 张） |
| 定时 | 支持 `HH:MM` 或 5 段 Cron，带随机抖动；改配置即时生效 |
| 内容来源 | 文案池随机 / 文本文件随机一行 / AI 按人设生成（可参考今日日程、最近聊天记录与联网资料） |
| 联网素材 | **接入 AstrBot 自带的联网搜索**：先联网查资料，再让 AI 结合资料写说说；搜索不可用时自动降级 |
| Token 用量 | 估算每次生成大概用多少 token，累计到 `/空间状态` 与 `/空间用量`；草稿与发布通知里也会带上 |
| 回执图 | 通知与草稿可附带一张渲染出来的回执图（用 AstrBot 自带文转图，不加字体、不加体积；渲染失败自动降级纯文本） |
| AI 接入 | **只复用 AstrBot 已配置的 LLM 提供商**，插件不保存密钥、不自己发请求 |
| 生活日程 | 自己用 AI 生成「今日穿搭 + 日程」（按天缓存、懒加载、创意池、防重复）；可选注入 system prompt |
| 说说互动 | 定时读取关注 QQ 号的最近说说（**默认只读**），可选自动点赞与 AI 评论，按 `uin_tid` 去重 |
| 草稿确认 | 自动发布/自动评论前先发给你确认：`/空间确认` 发、`/空间放弃` 丢、`/空间重写` 让 AI 再写一版 |
| 定时问候 | 按时间给指定用户**私聊**发早安 / 晚安，内容可用文案池或 AI 按人设生成，同一天同一时段不重复发 |
| 运维 | 发布历史、失败通知、`/空间状态` 一屏看全、`/空间删除` 按 tid 删说说 |

---

## 💿 安装

### 方式一：在 AstrBot 面板里用仓库地址安装（推荐）

在 WebUI 的 `插件` 页选择「从仓库安装」，填入本仓库地址即可。

### 方式二：手动放入插件目录

```text
<AstrBot数据目录>/data/plugins/astrbot_plugin_qzone_publisher/
```

常见数据目录：

- 源码部署：`<AstrBot仓库>/data/plugins/`
- Windows 桌面版：`%USERPROFILE%\.astrbot\data\plugins\`

放好后在面板点插件卡片上的「重载插件」，或重启 AstrBot。

### 前置条件

1. AstrBot 已配置 **aiocqhttp** 平台（NapCat / Lagrange 等），且 QQ 客户端处于登录状态。
   插件复用这个登录态，所以**你不需要填任何 Cookie**。
2. 依赖 `aiohttp`、`apscheduler` 是 AstrBot 自带库，通常无需额外安装。

### 快速开始

```text
/空间状态            # 看登录态、AI 接入、定时任务是否正常（不会发任何内容）
/空间发布 测试一下    # 手动发一条，确认链路通
/空间定时 08:30      # 设置每天 08:30 自动发布
/空间开关 on         # 打开定时发布
```

---

## AI 接入

插件里所有 AI 调用（写说说、生成日程、生成评论、生成问候语）**只使用 AstrBot 面板里
已经配置好的 LLM 提供商**：

- 密钥、模型、超时、重试全部由 AstrBot 管理，插件不保存任何密钥；
- 插件自己**不发起任何 HTTP 请求**，不存在绕过 AstrBot 提供商的第二套通道。

在 AstrBot 面板「服务提供商」里配好模型（密钥填在那里），然后在插件配置里**用下拉框选**：

| 字段 | 作用 |
| :--- | :--- |
| `llm_provider_id` | 写说说用，同时作为全局默认 |
| `llm_life_provider_id` | 生成生活日程用（留空则沿用全局） |
| `llm_comment_provider_id` | 生成评论用（留空则沿用全局） |
| `llm_greet_provider_id` | 生成问候语用（留空则沿用全局） |

四个都留空时自动使用 AstrBot 当前默认提供商，所以刚装上不用改任何 AI 配置。
`/空间状态` 会显示当前生效的提供商，以及被单独指定的功能。

> AstrBot 里模型是绑在提供商上的，选中提供商就等于选中模型，插件侧不需要再填模型名。

---

### 工作流程

```text
决定搜什么 → 调用 AstrBot 的内置搜索工具 → 拿到标题+摘要
          → 只把摘要喂给 AI，并要求「原创改写、不照抄标题、不贴链接」
          → 生成说说
```

任何一步失败（AstrBot 没开联网、没填密钥、搜索报错）都只记日志并**降级为普通生成**。

### 配置

| 字段 | 默认 | 说明 |
| :--- | :--- | :--- |
| `web_search_enabled` | `false` | 写说说时是否使用联网素材 |
| `web_search_query_mode` | `ai` | `ai`=让 AI 结合当日生活日程自己想一个搜索词；`fixed`=从关键词池随机取 |
| `web_search_query_pool` | 3 条示例 | `fixed` 模式随机取；`ai` 模式下作为「可参考方向」提示给 AI |
| `web_search_query_prompt` | 见默认值 | `ai` 模式拟搜索词用的提示词 |
| `web_search_count` | `5` | 每次搜几条（不同服务商参数名/上限不同，插件自动适配；不支持该参数的服务商则用其默认值） |

### 先验证再开

用 `/空间搜索 关键词` 可以**直接跑一条搜索**（不会发说说），用来确认接入是否正常；
不带参数则显示当前接入状态。确认没问题后再把 `web_search_enabled` 打开。

> **版本要求**：内置联网搜索工具是 **AstrBot 4.26** 起才提供的。
> 更老的版本上插件其余功能正常，联网素材会自动跳过并在 `/空间搜索`、`/空间状态`
> 里提示原因（这是本插件 `astrbot_version` 仍声明 `>=4.16` 的原因：不因为一个可选功能
> 而把老版本用户挡在门外）。

> 独立于本插件，AstrBot 自己的对话联网（`provider_settings.web_search`）是另一套开关：

---

## 👤 管理员识别

分两件事，插件都做对了，但来源不同：

| 用途 | 由谁决定 | 怎么配 |
| :--- | :--- | :--- |
| **谁能用管理指令**（`/空间发布`、`/空间定时`、`/空间确认` …） | **AstrBot** 的 `admins_id` | AstrBot 面板 → 配置 → `admins_id`。AstrBot 收到消息时会把命中的发送者标记为 admin，插件的 `@filter.permission_type(ADMIN)` 直接用它。**插件不会绕过这套机制** |
| **草稿确认、通知发给谁** | 插件配置 `admin_uins`，留空则回退 AstrBot 的 `admins_id` | 面板里填，或用 `/空间管理员 add <QQ号>` |

可以直接用指令维护插件内的名单：

```text
/空间管理员                  # 查看当前名单与来源
/空间管理员 add 123456       # 加入
/空间管理员 remove 123456    # 移除
```

`/空间状态` 第一屏就会显示 `管理员: xxx、yyy（来源: 插件配置 admin_uins / AstrBot 配置 admins_id）`，
如果两边都没配也会明确提示——不会出现"插件不知道管理员是谁"的情况。

> 注意：`admin_uins` 只影响**草稿和通知发给谁**。如果你希望某个人也能用管理指令，
> 需要把他加进 AstrBot 的 `admins_id`（这是 AstrBot 的权限体系，插件无权也不应该越权）。

---

## 定时问候

按时间给指定用户发私聊问候（早安 / 晚安）：

| 字段 | 默认 | 说明 |
| :--- | :--- | :--- |
| `greet_enabled` | `false` | 总开关，也可用 `/空间问候 on` 打开 |
| `greet_users` | `[]` | 问候对象的 QQ 号，例如 `["123456"]` |
| `greet_morning_cron` | `0 8 * * *` | 早安时间，留空表示不发 |
| `greet_night_cron` | `0 23 * * *` | 晚安时间，留空表示不发 |
| `greet_jitter` | `600` | 触发后随机延后 0~N 秒，不固定在同一秒发出 |
| `greet_use_ai` | `false` | 用 AI 结合人设与当日生活日程生成；关闭则从文案池随机取 |
| `greet_prompt` | 见默认值 | AI 提示词，`{slot}` 会被替换成「早安 / 晚安」 |
| `greet_morning_pool` / `greet_night_pool` | 各 3 条示例 | 文案池 |
| `llm_greet_provider_id` | 空 | 问候单独指定 AstrBot 提供商（留空用全局） |

- 同一天同一时段对同一个人只发一次（记录在 `greet_state.json`），
  随机抖动或错过的补偿触发都不会造成重复问候；手动测试会无视这条去重。
- AI 生成失败、返回空都会自动回退到文案池；某个 QQ 发不出去只记日志并汇总，不影响其他人。
- 先用 `/空间问候 morning 你的QQ号` 手动测一条，确认能收到再开定时。

---

## 生成链路（AI 自己确认并引用人格与日程）

```text
到点了（或你手动触发）
  → 读 AstrBot 当前的全局人格（面板里选定的那个 persona），插件不维护第二套人设
  → 检查今日生活日程：还没有就先生成，生成时同样要求「先自己确认这个人是谁」
  →（可选）用 AstrBot 自带联网搜索拿一点近期资料当素材
  → 把「人设 + 今日日程 + 素材」交给 AI，并明确要求它：
      · 先自己确认自己是谁、今天在做什么
      · 在正文里自然带出今天行程里的具体细节（正在做的事、去过的地方、穿搭），至少一处
      · 不要罗列日程表、不要写成流水账、也不要解释自己在引用日程
  → 产出说说 / 评论 / 问候语
```

- 日程**拿不到**时（例如生成时 AI 临时失败），提示词会退化成
  「先按你的身份与性格为自己安排一下今天，再据此写这条说说」，
  同时在 `/空间状态` 的「上次生成依据」里留一条警告，不会静默糊过去。
- `llm_life_must_reference` 关闭后，日程只作为语气参考，不强制带具体细节。
- `/空间状态` 的「上次生成依据」会告诉你这次生成用了哪个人格、有没有引用日程、
  有没有用联网素材与聊天记录——这是给**排查用**的，不是要你去点确认。

---

## 审核与限制

| 开关 | 默认 | 作用 |
| :--- | :--- | :--- |
| `draft_enabled` | `false` | 定时发说说先转草稿，人工确认后才发 |
| `draft_for_comment` | `true` | 自动评论也先转草稿 |
| `draft_for_greet` | `false` | 定时问候也先转草稿（你确认后才私聊发给问候对象） |
| `draft_timeout_minutes` | `0` | 大于 0 时草稿超时无人处理会**自动放行**并通知你；0 = 必须人工处理 |
| `draft_admin` / `draft_umo` | `true` / 空 | 草稿发给谁 |
| `admin_uins` | `[]` | 插件内管理员名单（见上面「管理员识别」） |

- 草稿只发给管理员与 `draft_umo`，**不会**发到 `notify_umo`；
- AstrBot 重启后遗留草稿会按同一套超时规则继续处理：已超时就直接放行，没超时就补上剩余计时；
- 手动 `/空间问候`、`/空间发布` 属于你本人的明确动作，不经过草稿。

---

## 📊 Token 用量（估算）

插件不读计费接口，而是**按文本长度估算**：中文约 0.7 token/字，其它字符约 1 token/4 字符，
整体约 ±20% 误差。它只用来让你"心里有数"，不要拿它对账。

| 位置 | 内容 |
| :--- | :--- |
| `/空间状态` | 今日总用量（输入 + 输出）+ 最近一次调用的用量 |
| `/空间用量 [天数]` | 按功能（说说 / 日程 / 评论 / 问候 / 搜索词）分组的明细 |
| 草稿确认消息 | 附一行「本次生成约用 N tokens」 |
| 发布 / 问候通知 | 同样附带本次用量 |

---

## ⚙️ 配置说明

配置在 AstrBot 面板「插件配置」里修改。

### 管理员

| 字段 | 默认 | 说明 |
| :--- | :--- | :--- |
| `admin_uins` | `[]` | 插件内管理员 QQ 号；填了就用它作为草稿/通知接收人，留空回退 AstrBot 的 `admins_id`（见上面「管理员识别」） |

### 发布控制

| 字段 | 默认 | 说明 |
| :--- | :--- | :--- |
| `auto_publish_enabled` | `false` | 定时自动发布总开关 |
| `publish_cron` | `30 8 * * *` | `HH:MM` 或 5 段 Cron（分 时 日 月 周），留空=不发布 |
| `publish_jitter` | `600` | 触发后随机延后 0~N 秒，0=精确触发 |

### 内容来源

| 字段 | 默认 | 说明 |
| :--- | :--- | :--- |
| `content_source` | `pool` | 下拉选择：文案池 / 文本文件 / AI 生成 |
| `text_pool` | 3 条示例 | 文案池内容 |
| `content_file` | 空 | 文本文件路径，一行一条，`#` 开头忽略 |
| `llm_prompt` | 见默认值 | 写说说的提示词 |
| `llm_use_persona` | `true` | 生成时注入 Bot 人设 |
| `llm_use_life_context` | `true` | 把今日穿搭/日程作为素材交给 AI |
| `llm_reference_chat` | `false` | 是否参考最近聊天记录 |
| `llm_chat_umo` / `llm_chat_count` | 空 / `30` | 参考哪个会话、参考多少条 |
| `llm_max_chars` | `200` | 生成内容最大字数 |

### AI 接入

`llm_provider_id`、`llm_life_provider_id`、`llm_comment_provider_id`、
`llm_greet_provider_id`（见上一节）。

### 联网素材

`web_search_enabled`、`web_search_query_mode`、`web_search_query_pool`、
`web_search_query_prompt`、`web_search_count`（见上一节）。

### 生活日程

| 字段 | 默认 | 说明 |
| :--- | :--- | :--- |
| `life_inject_enabled` | `false` | 是否把日程注入 system prompt。**默认关**：只有本插件负责注入时才打开，避免和其他也在注入生活状态的插件重复 |
| `life_reference_days` | `3` | 生成时参考过去几天日程，避免重复 |
| `life_prompt` | 见默认值 | 日程生成提示词，占位符见字段说明 |
| `life_pool` | 4 个池 | 面板里是「对象」类型，4 个子键：`daily_themes`（主题）/ `mood_colors`（心情色彩）/ `outfit_styles`（穿搭风格）/ `schedule_types`（日程类型），生成时每池随机抽一项 |

### 说说互动

| 字段 | 默认 | 说明 |
| :--- | :--- | :--- |
| `interact_enabled` | `true` | 定时读说说（只读）总开关 |
| `interact_cron` / `interact_jitter` | `0 21 * * *` / `600` | 巡检时间与抖动 |
| `interact_uins` | `[]` | **关注谁的空间**，填 QQ 号；留空则巡检不做任何事 |
| `interact_count` | `3` | 每个对象读几条 |
| `interact_skip_self` | `true` | 跳过自己发的 |
| `interact_like` | `false` | 自动点赞（默认关） |
| `interact_comment` | `false` | 自动评论（默认关，AI 生成） |
| `interact_comment_prompt` / `interact_comment_max_chars` | 见默认值 / `60` | 评论提示词与字数 |
| `interact_notify` | `false` | 巡检结束后发汇总通知 |

### 草稿确认

| 字段 | 默认 | 说明 |
| :--- | :--- | :--- |
| `draft_enabled` | `false` | 定时发布先转草稿，人工确认后才发出 |
| `draft_admin` | `true` | 草稿私聊插件内管理员（回退 AstrBot 的 `admins_id`） |
| `draft_umo` | 空 | 草稿额外发到的会话，格式 `平台ID:消息类型:会话ID` |
| `draft_for_comment` | `true` | 自动评论也先转草稿 |
| `draft_for_greet` | `false` | 定时问候也先转草稿 |
| `draft_timeout_minutes` | `0` | 超时自动放行的分钟数，0 = 必须人工处理 |

### 定时问候

`greet_enabled`、`greet_users`、`greet_morning_cron`、`greet_night_cron`、
`greet_jitter`、`greet_use_ai`、`greet_prompt`、`greet_morning_pool`、
`greet_night_pool`、`llm_greet_provider_id`（见上面「定时问候」）。

### 网络与通知

| 字段 | 默认 | 说明 |
| :--- | :--- | :--- |
| `cookie` | 空 | 手动 Cookie 兜底（需含 `uin`/`skey`/`p_skey`） |
| `cookie_ttl` | `600` | Cookie 缓存秒数，0=不主动刷新 |
| `timeout` | `15` | QQ空间请求超时（秒） |
| `max_images` | `9` | 单条说说最多图片 |
| `notify_enabled` / `notify_umo` | `true` / 空 | 发布结果通知发到哪 |
| `notify_render_image` | `false` | 通知与草稿附带一张渲染好的回执图 |
| `notify_render_network` | `false` | 回执图是否走 AstrBot 的 t2i 端点；默认关 = 本地渲染，内容不出本机 |
| `history_limit` | `200` | 本地保留发布记录条数 |

---

## 📝 指令

| 指令 | 权限 | 说明 |
| :--- | :--- | :--- |
| `/空间发布 <内容>` | 管理员 | 立即发说说，消息里的图片一起发（不走草稿） |
| `/空间状态` | 所有人 | 登录态 / 管理员 / AI 接入 / 生成依据 / 日程 / 定时任务 / 问候 / Token 用量 / 草稿 |
| `/空间用量 [天数]` | 管理员 | 查看 AI token 用量估算（按功能分组） |
| `/空间管理员 [add\|remove] <QQ>` | 管理员 | 查看或维护插件内管理员名单 |
| `/空间问候 [on\|off]` / `/空间问候 morning <QQ>` | 管理员 | 开关定时问候 / 立刻测试发一条 |
| `/空间重登` | 管理员 | 强制重取 Cookie |
| `/空间定时 [时间]` | 管理员 | 查看或设置发布时间（`08:30` / `30 8 * * *` / `off`） |
| `/空间开关 [on\|off]` | 管理员 | 定时发布开关 |
| `/空间互动 [on\|off]` | 管理员 | 说说巡检开关（点赞/评论在配置里单独开） |
| `/空间读说说 [force]` | 管理员 | 立刻巡检一轮 |
| `/空间日程 [renew]` | 所有人 | 查看今日生活日程，`renew` 重新生成 |
| `/空间搜索 [关键词]` | 管理员 | 用 AstrBot 自带联网搜索测一条（不发说说）；不带参数看接入状态 |
| `/空间确认` / `/空间放弃` / `/空间重写` | 管理员 | 处理待确认草稿 |
| `/空间历史 [条数]` | 所有人 | 最近发布记录（含失败原因） |
| `/空间删除 <tid>` | 管理员 | 删除指定说说 |

英文别名：上面每条指令都有 `/space xxx` 与 `/qz xxx` 两种英文写法，可用的词是
`post` / `status` / `relogin` / `cron` / `toggle` / `interact` / `search` / `life` /
`read` / `admin` / `greet` / `usage` / `ok` / `drop` / `redo` / `history` / `delete`
（例如 `/space post`、`/qz post`）。另外 `/空间登录` 等价于 `/空间状态`。

> 别名的匹配对象是**整条指令名**，所以 `/post` 这样只写动词的用法不会触发，
> 必须带 `space ` 或 `qz ` 前缀。

---

## 🧩 工作原理

QQ空间没有公开 API，插件用的是网页端私有协议，关键点：

- **登录态**：调用 OneBot 的 `get_cookies(domain="user.qzone.qq.com")`，
  从已登录的 QQ 客户端拿到 `uin` / `skey` / `p_skey`；`g_tk` 由 `p_skey` 按
  `hash = 5381; hash += (hash << 5) + ord(c); & 0x7FFFFFFF` 计算。
- **发表说说**：`POST .../cgi-bin/emotion_cgi_publish_v6`，正文在 `con` 字段。
- **带图发布**：先 `POST https://up.qzone.qq.com/cgi-bin/upload/cgi_upload_image`
  上传 base64 图片，取回 `pic_bo` 并拼成 `richval` 随说说提交。
- **读/赞/评**：`emotion_cgi_msglist_v6` 拉说说列表，`internal_dolike_app` 点赞，
  `emotion_cgi_re_feeds` 评论。

协议参数的来源与致谢见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

---

## 📂 目录结构与数据文件

```text
astrbot_plugin_qzone_publisher/
├── main.py                    插件入口：指令、生命周期、发布编排、草稿投递、提示词注入
├── metadata.yaml              插件元数据（市场展示、版本、支持平台）
├── _conf_schema.json          配置面板（AstrBot 据此自动生成设置界面）
├── requirements.txt           依赖声明
├── logo.png                   插件图标（可用 tools/make_logo.py 重新生成）
├── README.md / CHANGELOG.md   说明文档与更新日志
├── LICENSE                    许可证（AGPL-3.0）
├── THIRD_PARTY_NOTICES.md     第三方许可与致谢
├── ruff.toml                  代码检查配置
├── .gitattributes             换行符统一为 LF（避免跨平台整文件 diff）
├── .astrbot-plugin/i18n/      插件名与描述的国际化文案
├── .github/workflows/ci.yml   GitHub Actions：lint + 全套检查
├── core/
│   ├── config.py              配置包装（默认值直接读 _conf_schema.json）
│   ├── llm.py                 统一 AI 层（只走 AstrBot 已配置的提供商）
│   ├── life.py                一体化生活日程（生成 / 缓存 / 注入）
│   ├── usage.py               Token 用量估算与统计
│   ├── content.py             内容来源（文案池 / 文件 / AI / 联网素材）
│   ├── greet.py               定时问候（私聊早安/晚安、去重、文案池或 AI）
│   ├── web.py                 接入 AstrBot 自带联网搜索的桥
│   ├── interact.py            说说互动（巡检、去重、点赞、AI 评论）
│   ├── draft.py               草稿箱（持久化、确认 / 放弃 / 重写）
│   ├── scheduler.py           CronTask（Cron + 抖动 + 热更新）
│   ├── store.py               发布历史
│   └── qzone/                 协议层：登录态 / HTTP / 接口 / 解析 / 模型
├── tests/                     自测与检查脚本（见 tests/README.md）
└── tools/                     make_logo.py 生成图标；make_release_zip.py 打包发布 zip
```

运行期数据都写在 AstrBot 的数据目录下，更新插件不会丢：

```text
<数据目录>/plugin_data/astrbot_plugin_qzone_publisher/
├── publish_history.json   发布历史
├── life_schedule.json     生活日程缓存
├── draft.json             待确认草稿
├── greet_state.json       今日已发过谁（问候去重）
└── interacted_tids.json   已处理过的说说（去重用）
```

---

## ❓ 常见问题

**Q：`/空间状态` 显示登录态异常 / 取不到 Cookie？**
A：确认 AstrBot 里 aiocqhttp 平台在线、QQ 客户端已登录，且客户端版本支持 `get_cookies`
（NapCat、Lagrange 等较新版本都支持）。仍不行就在配置里填手动 `cookie` 兜底。

**Q：一直不发说说？**
A：按顺序检查 `/空间状态` 里的：定时发布是否「开启」、`publish_cron` 是否为空、
`content_source` 指向的文案池/文件是否有内容（用 AI 时 AI 是否可用）。

**Q：会不会和别的 QQ空间插件重复发说说？**
A：会。请**只让一个插件的自动发布保持开启**，自动评论同理（或把时间错开）。
本插件默认全关，就是为此。

**Q：AI 提示「没有可用的 AI」？**
A：插件只使用 AstrBot 的提供商。请到 AstrBot 面板「服务提供商」里配置并启用一个
LLM 提供商（密钥也填在那里），然后在插件配置里选中它；留空则用 AstrBot 的默认提供商。

**Q：草稿确认发不出去 / 收不到通知？**
A：先看 `/空间状态` 的「管理员」那一行。两边都没配就会提示；可以用
`/空间管理员 add 你的QQ号` 补上。

**Q：开了定时问候但没收到？**
A：依次检查：`greet_enabled` 是否开启（或 `/空间问候 on`）、`greet_users` 是否填了你的 QQ、
时间是否到了；然后先手动测一条：`/空间问候 morning 你的QQ号`。
另外注意：问候是**私聊**，需要 Bot 能给你发私聊（互为好友或平台允许）。

**Q：同一条问候会不会重复发？**
A：不会。同一天同一时段对同一个人只发一次，记录在 `greet_state.json`；
只有手动 `/空间问候` 测试才会忽略这条去重。

**Q：图片上传失败？**
A：图片接口本身较脆弱。失败时整条说说不会发布（避免发出半截内容），可以少带几张图重试。

**Q：开了联网素材但还是没有联网内容？**
A：先跑 `/空间搜索 关键词` 看具体原因。常见三种：AstrBot「联网搜索」没开、对应服务商的
密钥没填、或填的服务商不在 AstrBot 支持列表里。插件只负责调用，服务商与密钥都在 AstrBot 面板配置。

**Q：联网素材会不会让 AI 编新闻？**
A：插件只把**标题 + 摘要**（不含正文、不含完整链接）交给模型，并在提示词里硬性要求
「只依据资料陈述、不得编造资料之外的细节、不要照抄标题、不要贴链接」。
即便如此，AI 生成内容仍可能出错，建议开启草稿确认模式过一遍。

**Q：会不会被封号？**
A：用的是网页端私有协议，且没有官方保障。请把频率控制在合理范围（一天几条），
不要高频刷屏。风险自负。

---

## 🛠️ 开发与测试

```bash
pip install aiohttp apscheduler pyyaml

python tests/run_tests.py        # 292 项功能自测（不需要安装 AstrBot）
python tests/check_metadata.py   # 元数据 / 必需文件 / 隐私体检
python tests/check_schema.py     # 用 AstrBot 真实逻辑校验配置 schema
python tests/check_logo.py       # 校验 logo.png

ruff check . && ruff format --check .   # 代码检查与格式（配置见 ruff.toml）
```

细节见 [tests/README.md](tests/README.md)。CI 会在 Python 3.10 与 3.12 上跑上面全部检查。

---

## ⚠️ 免责声明

1. 本插件使用 QQ空间**网页端私有协议**，非腾讯官方接口。接口变更、风控、频率限制都可能导致失败。
2. 请遵守相关服务条款与法律法规，不要用于营销刷屏、骚扰他人等用途。
3. 使用本插件产生的任何后果（包括但不限于账号受限）由使用者自行承担。

---

## 🙏 致谢与许可

- [AstrBot](https://github.com/AstrBotDevs/AstrBot)：插件框架与配置面板机制。

完整的第三方许可声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

本项目以 [AGPL-3.0](LICENSE) 许可发布。更新记录见 [CHANGELOG.md](CHANGELOG.md)。
