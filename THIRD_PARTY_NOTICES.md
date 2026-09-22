# 第三方许可与致谢

本项目在编写过程中参考了以下开源项目的**实现思路、协议参数与提示词结构**。
按各自许可协议的要求，在此保留原始版权声明。

> 说明：本项目的代码为独立实现，未整文件复制上述项目源码；
> 但 QQ空间网页端接口的地址、表单字段、`richval` 拼接格式等属于从这些项目
> 的实践中获得的协议细节，属于「借鉴实现思路与协议参数」，故一并声明。

---

## 1. astrbot_plugin_qzone

- 仓库：https://github.com/Zhalslar/astrbot_plugin_qzone
- 作者：Zhalslar
- 许可：**GNU General Public License v3.0**（GPL-3.0）

参考内容：

- QQ空间登录态来源：调用 OneBot 的 `get_cookies` 获取 `user.qzone.qq.com` 域 Cookie；
- 发表说说、上传图片、点赞、评论、删除说说的接口地址与表单参数；
- `g_tk` 的计算方式、`pic_bo` / `richval` 的拼接格式；
- 说说列表接口 `emotion_cgi_msglist_v6` 的调用参数。

GPL-3.0 与 AGPL-3.0 相互兼容：本项目整体以 AGPL-3.0 发布，
上述 GPL-3.0 部分的相关权利与义务按 GPL-3.0 保留。

## 2. astrbot_plugin_life_scheduler

- 仓库：https://github.com/muyouzhi6/astrbot_plugin_life_scheduler
- 作者：木有知
- 许可：**MIT License**，Copyright (c) 2025 木有知

参考内容：

- 「每日穿搭 + 日程」的数据结构与按天缓存、懒加载思路；
- 创意池（主题 / 心情色彩 / 穿搭风格 / 日程类型）随机取值的做法；
- 注入 system prompt 时的「内在状态」文案与对话原则措辞。

> 说明：本插件**不读取也不依赖该插件生成的数据**，日程完全由自己生成；
> 上列内容均为设计思路参考。

MIT 许可全文：

```text
MIT License

Copyright (c) 2025 木有知

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## 3. AstrBot

- 仓库：https://github.com/AstrBotDevs/AstrBot
- 许可：AGPL-3.0

本插件基于 AstrBot 的插件接口开发，配置面板由 AstrBot 读取本仓库的
`_conf_schema.json` 自动生成，未包含 AstrBot 的代码。
