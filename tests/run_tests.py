"""插件自测脚本（可在没有安装 AstrBot 的环境直接跑）。

用桩模块替换 AstrBot 运行时，然后：
1. 校验纯逻辑（g_tk 计算、响应解析、Cron 归一化、Cookie 解析、历史存储、内容生成）；
2. 启动本地假 QQ空间 服务与假 OpenAI 兼容接口，端到端跑通
   「上传图片 + 发表说说 + 登录失效重试 + 好友说说互动 + 草稿确认」链路；
3. 直接调用插件指令处理函数，校验指令行为。

运行（依赖仅 aiohttp 与 apscheduler，AstrBot 自带这两个库）：
    python tests/run_tests.py
"""

import asyncio
import base64
import importlib
import json
import sys
import tempfile
import time
import types
from pathlib import Path
from typing import ClassVar

# Windows 控制台默认 GBK，直接打印中文与 ✅ 之类符号会 UnicodeEncodeError 把自测打断，
# 这里统一把标准输出切到 UTF-8（老版本 Python 没有 reconfigure 时自动跳过）。
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKSPACE = REPO_ROOT.parent  # 让 `import <插件目录名>` 生效
PLUGIN_NAME = REPO_ROOT.name  # 以目录名为准，插件改名后测试照常可用
DATA_DIR = Path(tempfile.mkdtemp(prefix="qzone_data_"))

PASSED: list[str] = []
FAILED: list[str] = []

# 统计插件是否调用了 AstrBot 自带的联网搜索配置归一化
NORMALIZE_CALLS = {"count": 0}


class FakeHtmlRenderer:
    """模拟 AstrBot 的文转图渲染器（astrbot.api.html_renderer）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.result: str = ""
        self.error: Exception | None = None

    async def render_t2i(
        self,
        text: str,
        use_network: bool = True,
        return_url: bool = False,
        template_name: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "text": text,
                "use_network": use_network,
                "return_url": return_url,
                "template_name": template_name,
            }
        )
        if self.error is not None:
            raise self.error
        return self.result


FAKE_RENDERER = FakeHtmlRenderer()


def check(name: str, condition: bool, detail: str = "") -> None:
    """记录一条断言结果。"""
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  FAIL  {name} :: {detail}")


def expect_raises(name: str, fn, exc=Exception) -> None:
    """断言调用会抛出指定异常。"""
    try:
        fn()
    except exc as e:
        check(name, True, str(e)[:60])
    except Exception as e:
        check(name, False, f"抛出 {type(e).__name__} 而非 {exc.__name__}: {e}")
    else:
        check(name, False, "未抛出异常")


class StubAstrBotConfig(dict):
    """模拟 AstrBotConfig：dict 语义 + save_config 计数。"""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.saved = 0

    def save_config(self) -> None:
        self.saved += 1


async def expect_raises_async(
    name: str, factory, exc=Exception, contains: str = ""
) -> None:
    """断言异步调用会抛出指定异常（可附带消息包含校验）。"""
    try:
        await factory()
    except exc as e:
        ok = contains in str(e) if contains else True
        check(name, ok, f"消息不匹配: {e}" if not ok else str(e)[:60])
    except Exception as e:
        check(name, False, f"抛出 {type(e).__name__} 而非 {exc.__name__}: {e}")
    else:
        check(name, False, "未抛出异常")


class FakeProvider:
    """模拟 AstrBot 的 LLM 提供商。"""

    def __init__(self, text: str = "提供商生成的内容") -> None:
        self.text = text
        self.calls: list[dict] = []

    async def text_chat(
        self, *, system_prompt=None, prompt=None, contexts=None, **kwargs
    ):
        self.calls.append(
            {"system_prompt": system_prompt, "prompt": prompt, "contexts": contexts}
        )
        return types.SimpleNamespace(completion_text=self.text)


# ----------------------------------------------------------------------
# AstrBot 桩
# ----------------------------------------------------------------------


def install_stubs() -> None:
    """把最小可用的 astrbot 桩模块注入 sys.modules。"""

    def make(name: str) -> types.ModuleType:
        module = types.ModuleType(name)
        sys.modules[name] = module
        return module

    class Logger:
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            print(f"    [info] {args[0] if args else ''}")

        def warning(self, *args, **kwargs):
            print(f"    [warn] {args[0] if args else ''}")

        def error(self, *args, **kwargs):
            print(f"    [error] {args[0] if args else ''}")

        def exception(self, *args, **kwargs):
            print(f"    [exc] {args[0] if args else ''}")

    logger = Logger()

    astrbot = make("astrbot")
    astrbot.logger = logger

    api = make("astrbot.api")
    api.logger = logger
    api.html_renderer = FAKE_RENDERER
    astrbot.api = api

    core = make("astrbot.core")
    astrbot.core = core

    # astrbot.api.star
    star_mod = make("astrbot.api.star")
    api.star = star_mod

    class StarTools:
        sent: ClassVar[list] = []

        @classmethod
        def get_data_dir(cls, plugin_name: str | None = None) -> Path:
            target = DATA_DIR / (plugin_name or "unknown")
            target.mkdir(parents=True, exist_ok=True)
            return target

        @classmethod
        async def send_message(cls, session, message_chain) -> bool:
            cls.sent.append((session, message_chain))
            return True

    class Star:
        def __init__(self, context=None) -> None:
            self.context = context

    class Context:
        pass

    star_mod.StarTools = StarTools
    star_mod.Star = Star
    star_mod.Context = Context

    # astrbot.api.event
    event_mod = make("astrbot.api.event")
    api.event = event_mod

    class Filter:
        class PermissionType:
            ADMIN = "admin"
            MEMBER = "member"

        @staticmethod
        def permission_type(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def command(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def on_llm_request(*args, **kwargs):
            return lambda func: func

        @staticmethod
        def platform_adapter_type(*args, **kwargs):
            return lambda func: func

    class AstrMessageEvent:
        pass

    event_mod.filter = Filter()
    event_mod.AstrMessageEvent = AstrMessageEvent

    # astrbot.core.config.astrbot_config
    config_pkg = make("astrbot.core.config")
    core.config = config_pkg
    config_mod = make("astrbot.core.config.astrbot_config")
    config_pkg.astrbot_config = config_mod
    config_mod.AstrBotConfig = StubAstrBotConfig

    # astrbot.core.message.*
    message_pkg = make("astrbot.core.message")
    core.message = message_pkg

    components = make("astrbot.core.message.components")
    message_pkg.components = components

    class Image:
        def __init__(self, file: str | None = None, url: str | None = None) -> None:
            self.file = file
            self.url = url
            self.path = None
            self._payload = b"fake-image-bytes"

        async def convert_to_base64(self) -> str:
            return base64.b64encode(self._payload).decode()

        @staticmethod
        def fromFileSystem(path, **kwargs) -> "Image":
            image = Image(file=str(path), **kwargs)
            image.path = str(path)
            return image

        @staticmethod
        def fromURL(url: str, **kwargs) -> "Image":
            return Image(file=url, url=url, **kwargs)

    class Plain:
        def __init__(self, text: str = "") -> None:
            self.text = text

    components.Image = Image
    components.Plain = Plain

    result_mod = make("astrbot.core.message.message_event_result")
    message_pkg.message_event_result = result_mod

    class MessageChain:
        def __init__(self, chain=None) -> None:
            self.chain = chain or []

    result_mod.MessageChain = MessageChain

    # astrbot.core.star.*
    star_pkg = make("astrbot.core.star")
    core.star = star_pkg
    star_pkg.Star = Star
    star_pkg.Context = Context
    star_pkg.StarTools = StarTools

    context_mod = make("astrbot.core.star.context")
    star_pkg.context = context_mod
    context_mod.Context = Context
    context_mod.PluginConfig = None

    filter_pkg = make("astrbot.core.star.filter")
    star_pkg.filter = filter_pkg
    command_mod = make("astrbot.core.star.filter.command")
    filter_pkg.command = command_mod

    class GreedyStr(str):
        pass

    command_mod.GreedyStr = GreedyStr

    # astrbot.core.tools.web_search_tools
    # 插件会复用 AstrBot 的旧配置归一化函数，这里提供桩并计数，验证「确实复用了自带实现」
    tools_pkg = make("astrbot.core.tools")
    core.tools = tools_pkg
    ws_mod = make("astrbot.core.tools.web_search_tools")
    tools_pkg.web_search_tools = ws_mod

    def normalize_legacy_web_search_config(cfg) -> None:
        settings = cfg.get("provider_settings") if hasattr(cfg, "get") else None
        if isinstance(settings, dict):
            for name in list(settings):
                if name.startswith("websearch_") and name.endswith("_key"):
                    value = settings[name]
                    if isinstance(value, str):
                        settings[name] = [value] if value else []
        NORMALIZE_CALLS["count"] += 1

    ws_mod.normalize_legacy_web_search_config = normalize_legacy_web_search_config


class FakeOneBot:
    """模拟 OneBot 客户端的 get_cookies / get_login_info。"""

    def __init__(self, cookie: str, nickname: str = "测试小号") -> None:
        self.cookie = cookie
        self.nickname = nickname
        self.cookie_calls = 0
        self.domain_seen: list[str] = []

    async def get_cookies(self, domain: str | None = None, **kwargs):
        self.cookie_calls += 1
        self.domain_seen.append(domain or "")
        if domain is None:
            return {"cookies": ""}
        return {"cookies": self.cookie}

    async def get_login_info(self):
        return {"nickname": self.nickname, "user_id": 123456}


class FakeSearchTool:
    """模拟 AstrBot 内置的联网搜索工具（web_search_*）。

    真实工具返回的是 ``json.dumps({"results": [...]})`` 字符串，
    出错时返回以 ``Error`` 开头的字符串，这里保持一致以便验证解析逻辑。
    """

    def __init__(
        self,
        name: str,
        payload: str | None = None,
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.error = error
        self.payload = (
            payload
            if payload is not None
            else json.dumps(
                {
                    "results": [
                        {
                            "title": "标题一",
                            "url": "https://news.example.com/a",
                            "snippet": "摘要一",
                        },
                        {
                            "title": "标题二",
                            "url": "https://blog.example.org/b",
                            "snippet": "摘要二",
                        },
                    ]
                },
                ensure_ascii=False,
            )
        )
        self.calls: list[dict] = []
        self.agent_context = None

    async def call(self, context, **kwargs):
        self.calls.append(kwargs)
        self.agent_context = context
        if self.error is not None:
            raise self.error
        return self.payload


class FakeToolManager:
    """模拟 AstrBot 的 LLM 工具管理器。"""

    def __init__(self, tools: dict[str, FakeSearchTool] | None = None) -> None:
        self.tools = tools or {}

    def get_builtin_tool(self, name):
        if not isinstance(name, str):
            return None
        return self.tools.get(name)


class FakeContext:
    """模拟 AstrBot 插件上下文。"""

    def __init__(
        self, onebot: FakeOneBot | None = None, admins: list | None = None
    ) -> None:
        self.onebot = onebot
        self.conversation_manager = None
        self.persona_manager = None
        self.admins = admins if admins is not None else ["10001"]
        self.provider = None
        self.providers: dict[str, object] = {}
        self.provider_settings: dict = {}
        self.tool_manager = None

        outer = self

        class _Platform:
            def meta(self):
                return types.SimpleNamespace(name="aiocqhttp", id="aiocqhttp")

            @property
            def bot(self):
                return outer.onebot

        self.platform_manager = types.SimpleNamespace(
            get_insts=lambda: [_Platform()], platform_insts=[_Platform()]
        )

    def get_config(self, umo=None):
        return {
            "timezone": "Asia/Shanghai",
            "admins_id": self.admins,
            "provider_settings": dict(self.provider_settings),
        }

    def get_llm_tool_manager(self):
        if self.tool_manager is None:
            raise AttributeError("测试桩未提供 llm 工具管理器")
        return self.tool_manager

    def get_provider_by_id(self, provider_id):
        if provider_id in self.providers:
            return self.providers[provider_id]
        return self.provider

    def get_using_provider(self, umo=None):
        return self.provider


class FakeEvent:
    """模拟消息事件。"""

    def __init__(self, message=None, bot=None) -> None:
        self.unified_msg_origin = "aiocqhttp:FriendMessage:123456"
        self.message_obj = types.SimpleNamespace(message=message or [])
        self.bot = bot
        self.results: list[str] = []

    def plain_result(self, text: str) -> str:
        return text


async def collect(agen) -> list[str]:
    """收集异步生成器指令处理函数的全部输出。"""
    return [item async for item in agen]


# ----------------------------------------------------------------------
# 测试主体
# ----------------------------------------------------------------------


async def main() -> int:
    install_stubs()
    sys.path.insert(0, str(WORKSPACE))

    from astrbot.api.star import StarTools

    def _imp(dotted: str):
        """按插件目录名动态导入，避免把插件名写死在测试里。"""
        return importlib.import_module(f"{PLUGIN_NAME}.{dotted}")

    PluginConfig = _imp("core.config").PluginConfig
    ContentGenerator = _imp("core.content").ContentGenerator
    _draft = _imp("core.draft")
    Draft, DraftBox = _draft.Draft, _draft.DraftBox
    InteractService = _imp("core.interact").InteractService
    GreetingService = _imp("core.greet").GreetingService
    _life = _imp("core.life")
    LifeManager = _life.LifeManager
    extract_json_object = _life.extract_json_object
    time_desc = _life.time_desc
    AIClient = _imp("core.llm").AIClient
    _qzone = _imp("core.qzone")
    FeedPost = _qzone.FeedPost
    QzoneAPI = _qzone.QzoneAPI
    QzoneSession = _qzone.QzoneSession
    _qzone_model = _imp("core.qzone.model")
    ApiResponse = _qzone_model.ApiResponse
    QzoneParser = _imp("core.qzone.parser").QzoneParser
    _scheduler = _imp("core.scheduler")
    CronTask = _scheduler.CronTask
    normalize_cron = _scheduler.normalize_cron
    _store = _imp("core.store")
    PublishRecord = _store.PublishRecord
    PublishStore = _store.PublishStore
    QzonePublisherPlugin = _imp("main").QzonePublisherPlugin

    _cfg_paths = _imp("core.config").PATHS

    def cfg_set(raw: dict, key: str, value) -> None:
        """按板块路径写配置值。

        面板 schema 已分组，测试里不能再直接写扁平键；顺带每一层都复制一份，
        避免浅拷贝出来的子字典被改动后污染源配置。
        """
        path = _cfg_paths[key]
        node = raw
        for part in path[:-1]:
            child = node.get(part)
            child = dict(child) if isinstance(child, dict) else {}
            node[part] = child
            node = child
        node[path[-1]] = value

    def cfg_peek(raw: dict, key: str):
        """按板块路径读原始值，读不到返回 None。"""
        node: object = raw
        for part in _cfg_paths[key]:
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    cookie = "uin=o123456; skey=@Abc123; p_skey=Zz99Kk"
    onebot = FakeOneBot(cookie)

    print("\n[1] g_tk 计算与登录上下文")
    ctx = QzoneSession._parse_cookie(cookie)
    check("Cookie 解析 uin", ctx.uin == 123456, str(ctx.uin))
    check("Cookie 解析 p_skey", ctx.p_skey == "Zz99Kk", ctx.p_skey)

    # 用 hash*33 的等价形式独立复算，交叉验证位移算法
    expected = 5381
    for ch in "Zz99Kk":
        expected = expected * 33 + ord(ch)
    expected &= 0x7FFFFFFF
    check("g_tk 与乘法等价式一致", ctx.gtk == str(expected), f"{ctx.gtk} vs {expected}")
    check("g_tk 在 31 位内", 0 <= int(ctx.gtk) < 2**31, ctx.gtk)
    check("cookies 形如 o<uin>", ctx.cookies()["uin"] == "o123456", str(ctx.cookies()))

    ctx_no_pskey = QzoneSession._parse_cookie("uin=123456; skey=only_skey")
    check(
        "p_skey 缺失时回退 skey",
        ctx_no_pskey.p_skey == "only_skey" and ctx_no_pskey.gtk != "",
        ctx_no_pskey.p_skey,
    )
    expect_raises(
        "缺少 uin 报错", lambda: QzoneSession._parse_cookie("skey=abc"), RuntimeError
    )
    expect_raises(
        "缺少 skey 报错", lambda: QzoneSession._parse_cookie("uin=o123"), RuntimeError
    )

    print("\n[2] 响应解析")
    parsed = QzoneParser.parse_response(
        '_preloadCallback({"code":0,"data":{"tid":"abc"}});'
    )
    check("JSONP 解析", parsed.get("code") == 0, str(parsed))
    parsed = QzoneParser.parse_response('{"code":0,"msg":undefined}')
    check("undefined 替换为 null", parsed.get("msg") is None, str(parsed))
    parsed = QzoneParser.parse_response("<html>403 Forbidden</html>")
    check("非 JSON 返回错误码", parsed.get("code") == -1, str(parsed))
    parsed = QzoneParser.parse_response("")
    check("空响应返回错误码", parsed.get("code") == -1, str(parsed))

    pic_bo, richval = QzoneParser.parse_upload_result(
        {
            "data": {
                "url": "http://x/psb?/V1/xx*yy!/b/AAA&bo=BO_TOKEN_123&rf=viewer_311",
                "albumid": "alb",
                "lloc": "LL",
                "sloc": "SL",
                "type": 1,
                "height": 100,
                "width": 200,
            }
        }
    )
    check("pic_bo 提取", pic_bo == "BO_TOKEN_123", pic_bo)
    check(
        "richval 段数",
        len(richval.split(",")) == 10 and richval.startswith(","),
        richval,
    )

    print("\n[3] 统一响应与发布结果归一化")
    ok_resp = ApiResponse.from_raw({"code": 0, "message": "", "tid": "T1"})
    check("成功响应 ok", ok_resp.ok and ok_resp.data.get("tid") == "T1", str(ok_resp))
    bad_resp = ApiResponse.from_raw({"code": -3000, "message": "登录态失效"})
    check(
        "失败响应带消息",
        (not bad_resp.ok) and bad_resp.message == "登录态失效",
        str(bad_resp.message),
    )
    nested = QzoneAPI._normalize_publish(
        {"code": 0, "data": {"tid": "NESTED", "now": 1700000000}}
    )
    check(
        "嵌套 data 中的 tid", nested.ok and nested.data["tid"] == "NESTED", str(nested)
    )
    flat = QzoneAPI._normalize_publish({"code": 0, "tid": "FLAT", "now": 1})
    check("顶层 tid", flat.ok and flat.data["tid"] == "FLAT", str(flat))
    failed = QzoneAPI._normalize_publish({"code": 0, "message": ""})
    check("无 tid 视为失败", not failed.ok, str(failed.message))

    print("\n[4] Cron 归一化")
    check(
        "HH:MM 转换",
        normalize_cron("08:30") == "30 8 * * *",
        str(normalize_cron("08:30")),
    )
    check("5 段原样保留", normalize_cron("30 8 * * *") == "30 8 * * *")
    check("空串返回 None", normalize_cron("") is None)
    check("off 之外的空格", normalize_cron("   ") is None)
    expect_raises("非法时间报错", lambda: normalize_cron("99:99"), ValueError)
    expect_raises("乱写报错", lambda: normalize_cron("每天八点"), ValueError)

    print("\n[5] 发布历史存储")
    store_path = DATA_DIR / "history.json"
    store = PublishStore(store_path, limit=3)
    for index in range(4):
        store.append(
            PublishRecord(
                time=1700000000 + index,
                text=f"内容{index}",
                tid=f"tid{index}",
                uin=123456,
                source="pool",
                ok=index != 1,
                error="" if index != 1 else "模拟失败",
            )
        )
    check("按上限裁剪", len(store._records) == 3, str(len(store._records)))
    check(
        "recent 最新在前", store.recent(1)[0].text == "内容3", store.recent(1)[0].text
    )
    check(
        "last_success 跳过失败项",
        store.last_success() is not None and store.last_success().text == "内容3",
        str(store.last_success()),
    )
    reloaded = PublishStore(store_path, limit=3)
    check(
        "持久化往返",
        len(reloaded._records) == 3 and reloaded._records[-1].text == "内容3",
        str(len(reloaded._records)),
    )

    print("\n[6] 内容生成（文案池 / 文件）")
    raw_config = StubAstrBotConfig(
        {
            "text_pool": ["文案A", "文案B"],
            "content_source": "pool",
            "history_limit": 200,
            "timeout": 15,
            "max_images": 9,
            "publish_cron": "30 8 * * *",
            "publish_jitter": 600,
            "auto_publish_enabled": True,
            "cookie": cookie,
            "cookie_ttl": 600,
            "notify_enabled": True,
            "notify_umo": "aiocqhttp:FriendMessage:123456",
            "llm_provider_id": "",
            "llm_prompt": "写一条说说",
            "llm_use_persona": False,
            "llm_reference_chat": False,
            "llm_chat_umo": "",
            "llm_chat_count": 30,
            "llm_max_chars": 200,
            "content_file": "",
        }
    )
    cfg = PluginConfig(raw_config, FakeContext(onebot))
    check("配置读取默认值", cfg.max_images == 9, str(cfg.max_images))
    check("未定义项报 AttributeError", not hasattr(cfg, "not_a_field"))
    cfg.set("publish_cron", "0 9 * * *")
    check(
        "配置写回并持久化（写入对应板块）",
        raw_config["sec_post"]["publish_cron"] == "0 9 * * *" and raw_config.saved >= 1,
        str(raw_config.saved),
    )
    check(
        "旧扁平配置已迁移到板块结构",
        "publish_cron" not in raw_config
        and raw_config["_flat_keys_migrated"] is True
        and raw_config["sec_post"]["text_pool"] == ["文案A", "文案B"],
        str(sorted(raw_config.keys())[:6]),
    )
    check(
        "配置项能报出所属板块",
        cfg.section_of.get("publish_cron") == "空间说说"
        and cfg.section_of.get("greet_users") == "私聊问候",
        str(cfg.section_of.get("publish_cron")),
    )

    content = ContentGenerator(cfg, AIClient(cfg, FakeContext(onebot)), None)
    text, source = await content.generate()
    check(
        "文案池生成",
        text in {"文案A", "文案B"} and source == "pool",
        f"{text}/{source}",
    )

    text_file = DATA_DIR / "lines.txt"
    text_file.write_text("# 注释\n\n第一行\n第二行\n", encoding="utf-8")
    cfg.set("content_source", "file")
    cfg.set("content_file", str(text_file))
    text, source = await content.generate()
    check(
        "文件生成忽略注释空行",
        text in {"第一行", "第二行"} and source == "file",
        f"{text}/{source}",
    )

    cfg.set("content_source", "file")
    cfg.set("content_file", str(DATA_DIR / "missing.txt"))
    try:
        await content.generate()
        check("缺失文件报错", False, "未抛异常")
    except RuntimeError as e:
        check("缺失文件报错", "不存在" in str(e), str(e))

    cfg.set("content_source", "llm")
    try:
        await content.generate()
        check("无可用 AI 时报错", False, "未抛异常")
    except RuntimeError as e:
        check("无可用 AI 时报错", "没有可用的 AI" in str(e), str(e))

    print("\n[7] 本地假 QQ空间 服务：上传图片 + 发表说说")
    from aiohttp import web

    received: dict = {}

    async def handle_upload(request):
        form = await request.post()
        received["upload"] = {
            "form": dict(form),
            "cookies": dict(request.cookies),
        }
        payload = {
            "ret": 0,
            "msg": "",
            "data": {
                "url": "http://fake/img?bo=BO_TOKEN_ABC",
                "albumid": "alb1",
                "lloc": "lloc1",
                "sloc": "sloc1",
                "type": 1,
                "height": 640,
                "width": 480,
            },
        }
        return web.Response(
            text=f"_preloadCallback({json.dumps(payload)});", content_type="text/plain"
        )

    async def handle_publish(request):
        form = await request.post()
        received["publish"] = {
            "form": dict(form),
            "cookies": dict(request.cookies),
            "query": dict(request.query),
        }
        payload = {
            "code": 0,
            "subcode": 0,
            "message": "",
            "data": {"tid": "TID_REAL", "now": 1700000123},
        }
        return web.Response(text=json.dumps(payload), content_type="text/plain")

    retry_counter = {"count": 0}

    async def handle_publish_retry(request):
        retry_counter["count"] += 1
        if retry_counter["count"] == 1:
            return web.Response(
                text=json.dumps({"code": -3000, "message": "请先登录"}),
                content_type="text/plain",
            )
        return web.Response(
            text=json.dumps({"code": 0, "data": {"tid": "TID_AFTER_RETRY", "now": 1}}),
            content_type="text/plain",
        )

    app = web.Application()
    app.router.add_post("/upload", handle_upload)
    app.router.add_post("/publish", handle_publish)
    app.router.add_post("/publish_retry", handle_publish_retry)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 8791)
    await site.start()

    api = QzoneAPI(QzoneSession(cfg, lambda: onebot), timeout=10)
    api.UPLOAD_IMAGE_URL = "http://127.0.0.1:8791/upload"
    api.EMOTION_URL = "http://127.0.0.1:8791/publish"

    resp = await api.publish("今天天气不错", [b"fake-image-bytes"])
    check(
        "发布成功并解析嵌套 tid", resp.ok and resp.data["tid"] == "TID_REAL", str(resp)
    )
    check("now 字段解析", resp.data["now"] == 1700000123, str(resp.data))

    publish_req = received.get("publish", {})
    form = publish_req.get("form", {})
    query = publish_req.get("query", {})
    cookies = publish_req.get("cookies", {})
    check(
        "正文以 con 字段提交", form.get("con") == "今天天气不错", str(form.get("con"))
    )
    check(
        "hostuin 正确", str(form.get("hostuin")) == "123456", str(form.get("hostuin"))
    )
    check(
        "g_tk 与 uin 作为查询参数",
        query.get("g_tk") == QzoneSession._parse_cookie(cookie).gtk
        and query.get("uin") == "123456",
        str(query),
    )
    check(
        "Cookie 正确透传",
        cookies.get("uin") == "o123456"
        and cookies.get("skey") == "@Abc123"
        and cookies.get("p_skey") == "Zz99Kk",
        str(cookies),
    )
    check(
        "图片字段 pic_bo/richtype",
        form.get("pic_bo") == "BO_TOKEN_ABC" and form.get("richtype") == "1",
        f"{form.get('pic_bo')}/{form.get('richtype')}",
    )
    check(
        "单图 richval 不含多余分隔符",
        "\t" not in str(form.get("richval"))
        and str(form.get("richval")).count(",") == 9,
        str(form.get("richval")),
    )

    upload_form = received.get("upload", {}).get("form", {})
    check(
        "上传接口收到 base64 图片",
        upload_form.get("base64") == "1"
        and upload_form.get("picfile")
        == base64.b64encode(b"fake-image-bytes").decode(),
        str(list(upload_form.keys())),
    )
    check(
        "上传接口收到 skey/uin",
        upload_form.get("skey") == "@Abc123"
        and str(upload_form.get("uin")) == "123456",
        str(upload_form.get("uin")),
    )

    resp_multi = await api.publish("两张图", [b"img1", b"img2"])
    multi_form = received["publish"]["form"]
    check("多图发布成功", resp_multi.ok, str(resp_multi))
    check(
        "多图 pic_bo 以逗号分隔",
        str(multi_form.get("pic_bo")).count(",") == 1,
        str(multi_form.get("pic_bo")),
    )
    check(
        "多图 richval 以制表符分隔",
        str(multi_form.get("richval")).count("\t") == 1,
        repr(multi_form.get("richval")),
    )
    check(
        "多图 picfile 数量正确",
        "," not in str(received["upload"]["form"].get("picfile")),
        str(list(received["upload"]["form"].keys())),
    )

    print("\n[8] 登录态失效自动重试")
    calls_before = onebot.cookie_calls
    api2 = QzoneAPI(QzoneSession(cfg, lambda: onebot), timeout=10)
    api2.EMOTION_URL = "http://127.0.0.1:8791/publish_retry"
    resp2 = await api2.publish("重试测试")
    check(
        "失效后重试成功",
        resp2.ok and resp2.data["tid"] == "TID_AFTER_RETRY",
        str(resp2),
    )
    check(
        "重试时重新获取 Cookie（手动模式只复用手动配置）",
        retry_counter["count"] == 2,
        str(retry_counter["count"]),
    )
    _ = calls_before

    print("\n[9] 定时调度器")
    sched_cfg = StubAstrBotConfig(
        {
            "timezone": "Asia/Shanghai",
            "auto_publish_enabled": True,
            "publish_cron": "30 8 * * *",
            "publish_jitter": 600,
        }
    )
    plugin_cfg = PluginConfig(sched_cfg, FakeContext(onebot))
    fired = {"count": 0}

    async def job():
        fired["count"] += 1

    task = CronTask.from_config(
        plugin_cfg,
        name="test_task",
        job=job,
        cron_key="publish_cron",
        jitter_key="publish_jitter",
        enabled_key="auto_publish_enabled",
    )
    cron = task.start()
    check("调度器启动", cron == "30 8 * * *" and task.running, str(cron))
    check("下次执行时间可读", task.next_run_time != "未调度", task.next_run_time)
    trigger = task._build_trigger()
    check("触发器携带抖动", trigger.jitter == 600, str(trigger.jitter))
    check(
        "触发器时区正确",
        str(trigger.timezone) == "Asia/Shanghai",
        str(trigger.timezone),
    )

    cron = task.reconfigure(cron="08:30")
    check("热更新 HH:MM", cron == "30 8 * * *" and task.running, str(cron))
    cron = task.reconfigure(cron="乱写的时间")
    check(
        "非法时间不崩溃且不调度",
        cron is None and not task.running and bool(task.error),
        f"{cron}/{task.error}",
    )
    cron = task.reconfigure(cron="30 8 * * *", enabled=False)
    check("关闭开关后不启动", cron is None and not task.running, str(cron))
    cron = task.reconfigure(enabled=True)
    check("重新开启后启动", cron == "30 8 * * *" and task.running, str(cron))
    task.stop()
    check("调度器停止", not task.running)

    print("\n[10] 插件指令")
    plugin = QzonePublisherPlugin(FakeContext(onebot), raw_config)
    check("插件构造完成", plugin.cfg is not None and plugin.api is not None)
    # 关键：任何指令调用前先把接口指向本地假服务，避免误触真实 QQ空间
    plugin.api.EMOTION_URL = "http://127.0.0.1:8791/publish"
    plugin.api.UPLOAD_IMAGE_URL = "http://127.0.0.1:8791/upload"

    out = await collect(plugin.cmd_publish(FakeEvent(), "指令发布测试"))
    check(
        "指令回复包含成功与 tid",
        any("发布成功" in item for item in out)
        and any("TID_REAL" in item for item in out),
        str(out),
    )

    out = await collect(plugin.cmd_publish(FakeEvent(), ""))
    check("空内容给出用法提示", any("用法" in item for item in out), str(out))

    image_event = FakeEvent(
        message=[
            __import__("astrbot.core.message.components", fromlist=["Image"]).Image(
                file="fake"
            )
        ]
    )
    out = await collect(plugin.cmd_publish(image_event, "带图说说"))
    check(
        "附带图片时上传并发布",
        any("图片: 1 张" in item for item in out),
        str(out),
    )

    out = await collect(plugin.cmd_status(FakeEvent()))
    status_text = out[0]
    check("状态指令显示登录态", "登录态: 正常" in status_text, status_text[:120])
    check("状态指令显示内容来源", "内容来源" in status_text, status_text[:120])

    out = await collect(plugin.cmd_history(FakeEvent(), 3))
    check("历史指令有输出", "发布记录" in out[0], out[0][:120])

    out = await collect(plugin.cmd_schedule(FakeEvent(), ""))
    check("定时指令查看模式", "当前自动发布时间" in out[0], out[0][:120])

    out = await collect(plugin.cmd_schedule(FakeEvent(), "09:45"))
    check(
        "定时指令设置 HH:MM",
        any("45 9 * * *" in item for item in out)
        and raw_config["sec_post"]["publish_cron"] == "45 9 * * *",
        str(out),
    )

    out = await collect(plugin.cmd_schedule(FakeEvent(), "乱写"))
    check("定时指令拒绝非法格式", any("设置失败" in item for item in out), str(out))

    out = await collect(plugin.cmd_schedule(FakeEvent(), "off"))
    check(
        "定时指令关闭",
        raw_config["sec_post"]["publish_cron"] == ""
        and not plugin.publish_task.running,
        str(out),
    )

    out = await collect(plugin.cmd_toggle(FakeEvent(), "on"))
    check("开关指令提示缺少时间", any("空间定时" in item for item in out), str(out))

    out = await collect(plugin.cmd_toggle(FakeEvent(), "off"))
    check(
        "开关指令关闭",
        raw_config["sec_post"]["auto_publish_enabled"] is False
        and not plugin.publish_task.running,
        str(out),
    )

    out = await collect(plugin.cmd_delete(FakeEvent(), ""))
    check("删除指令要求 tid", any("用法" in item for item in out), str(out))

    print("\n[11] 自动发布全链路（含通知）")
    plugin.cfg.set("publish_cron", "30 8 * * *")
    plugin.cfg.set("auto_publish_enabled", True)
    plugin.cfg.set("text_pool", ["自动发布内容"])
    plugin.cfg.set("content_source", "pool")
    plugin.api.EMOTION_URL = "http://127.0.0.1:8791/publish"
    await plugin._auto_publish()
    last = plugin.store.last_success()
    check(
        "自动发布写入历史",
        last is not None and last.text == "自动发布内容" and last.source == "pool",
        str(last),
    )
    check(
        "通知发送到配置的会话",
        len(StarTools.sent) >= 1
        and StarTools.sent[-1][0] == "aiocqhttp:FriendMessage:123456",
        str(StarTools.sent[-1][0] if StarTools.sent else None),
    )

    await plugin.api.close()
    await api.close()
    await api2.close()
    await runner.cleanup()

    print("\n[12] OneBot Cookie 自动获取路径")
    auto_cfg = StubAstrBotConfig({"sec_network": {"cookie": "", "cookie_ttl": 600}})
    session_auto = QzoneSession(
        PluginConfig(auto_cfg, FakeContext(onebot)), lambda: onebot
    )
    ctx_auto = await session_auto.get_ctx()
    check(
        "自动从 OneBot 获取 Cookie",
        ctx_auto.uin == 123456 and session_auto.source == "onebot",
        f"{ctx_auto.uin}/{session_auto.source}",
    )
    check(
        "优先携带 domain 查询",
        onebot.domain_seen[-1] == "user.qzone.qq.com",
        str(onebot.domain_seen[-1]),
    )
    check(
        "g_tk 由 Cookie 计算",
        ctx_auto.gtk == QzoneSession._parse_cookie(cookie).gtk,
        ctx_auto.gtk,
    )

    calls = onebot.cookie_calls
    await session_auto.get_ctx()
    check("TTL 内复用缓存", onebot.cookie_calls == calls, str(onebot.cookie_calls))
    await session_auto.invalidate()
    await session_auto.get_ctx()
    check(
        "invalidate 后重新获取",
        onebot.cookie_calls == calls + 1,
        str(onebot.cookie_calls),
    )

    class NoDomainOneBot(FakeOneBot):
        """模拟不支持 domain 参数的 OneBot 实现。"""

        async def get_cookies(self, domain=None, **kwargs):
            self.cookie_calls += 1
            self.domain_seen.append(domain or "")
            if domain:
                raise RuntimeError("action not supported")
            return {"cookies": self.cookie}

    no_domain = NoDomainOneBot(cookie)
    auto_cfg2 = StubAstrBotConfig({"sec_network": {"cookie": "", "cookie_ttl": 600}})
    session_fallback = QzoneSession(
        PluginConfig(auto_cfg2, FakeContext(no_domain)), lambda: no_domain
    )
    ctx_fallback = await session_fallback.get_ctx()
    check(
        "domain 不支持时回退无参调用",
        ctx_fallback.uin == 123456 and no_domain.cookie_calls == 2,
        str(no_domain.cookie_calls),
    )

    class EmptyCookieOneBot(FakeOneBot):
        async def get_cookies(self, domain=None, **kwargs):
            return {"cookies": ""}

    empty_cfg = StubAstrBotConfig({"sec_network": {"cookie": ""}})
    empty_session = QzoneSession(
        PluginConfig(empty_cfg, FakeContext(EmptyCookieOneBot(cookie))),
        lambda: EmptyCookieOneBot(cookie),
    )
    try:
        await empty_session.get_ctx()
        check("Empty Cookie 报错", False, "未抛异常")
    except RuntimeError as e:
        check("空 Cookie 给出可操作报错", "get_cookies" in str(e), str(e)[:70])

    no_client_cfg = StubAstrBotConfig({"sec_network": {"cookie": ""}})
    no_client_session = QzoneSession(
        PluginConfig(no_client_cfg, FakeContext(None)), lambda: None
    )
    try:
        await no_client_session.get_ctx()
        check("无 OneBot 平台时报错", False, "未抛异常")
    except RuntimeError as e:
        check(
            "无 OneBot 平台时提示配置手动 Cookie", "手动 Cookie" in str(e), str(e)[:70]
        )

    nickname = await session_auto.get_nickname()
    check("通过 OneBot 取昵称", nickname == "测试小号", nickname)

    # ==================================================================
    # 第二轮新增功能：AI 接入 / 一体化日程 / 互动 / 草稿 / 注入
    # ==================================================================

    ai_requests: list[dict] = []
    feeds_calls: list[dict] = []
    likes: list[dict] = []
    comments: list[dict] = []
    replies: list[dict] = []
    details: list[dict] = []
    detail_comments: list[dict] = []

    # 假空间返回的说说列表：测试里可以整体替换，用来构造各种时间窗口场景
    now_ts = int(time.time())
    SELF_UIN = 123456

    def friend_post(
        tid: str, ago_hours: float, uin: int = 999999, name: str = "小明"
    ) -> dict:
        """造一条「ago_hours 小时前发布」的好友说说。"""
        return {
            "uin": uin,
            "tid": tid,
            "name": name,
            "content": "今天天气不错[em]e100[/em]",
            "created_time": int(now_ts - ago_hours * 3600),
            "pic": [{"url2": "http://img/1.jpg"}],
            "commentlist": [{"tid": 1}],
        }

    def comment_item(
        tid: str,
        content: str,
        uin: int = 999999,
        name: str = "小明",
        **extra,
    ) -> dict:
        """造一条评论明细（字段名沿用接口的真实写法，便于验证容错）。"""
        item = {
            "uin": uin,
            "name": name,
            "tid": tid,
            "content": content,
            "createTime": now_ts - 600,
        }
        item.update(extra)
        return item

    def my_post(
        tid: str,
        ago_hours: float,
        commentlist: list[dict] | None = None,
        **extra,
    ) -> dict:
        """造一条自己发布的说说，可附带评论明细。"""
        post = {
            "uin": SELF_UIN,
            "tid": tid,
            "name": "我自己",
            "content": "我今天发的说说",
            "created_time": int(now_ts - ago_hours * 3600),
            "commentlist": commentlist or [],
        }
        post.update(extra)
        return post

    feeds_payload: list[dict] = [
        friend_post("T1", 1),
        {
            "uin": 123456,
            "tid": "T2",
            "name": "我自己",
            "content": "我发的说说",
            "created_time": now_ts - 2 * 3600,
        },
    ]

    async def handle_ai(request):
        body = await request.json()
        ai_requests.append(
            {
                "body": body,
                "auth": request.headers.get("Authorization", ""),
                "path": request.path,
            }
        )
        model = body.get("model")
        if model == "boom":
            return web.Response(status=500, text="server error")
        if model == "garbage":
            return web.Response(text="<html>not json</html>", content_type="text/html")
        if model == "empty":
            return web.json_response({"choices": [{"message": {"content": "   "}}]})
        if model == "listform":
            return web.json_response(
                {
                    "choices": [
                        {
                            "message": {
                                "content": [
                                    {"type": "text", "text": "分段"},
                                    {"type": "text", "text": "内容"},
                                ]
                            }
                        }
                    ]
                }
            )
        if model == "errfield":
            return web.json_response({"error": {"message": "余额不足"}})
        return web.json_response(
            {"choices": [{"message": {"content": "AI生成的内容"}}]}
        )

    async def handle_feeds(request):
        feeds_calls.append(dict(request.query))
        return web.json_response({"code": 0, "msglist": feeds_payload})

    async def handle_like(request):
        form = await request.post()
        likes.append({"form": dict(form), "query": dict(request.query)})
        return web.json_response({"code": 0})

    async def handle_comment(request):
        form = await request.post()
        comments.append({"form": dict(form), "query": dict(request.query)})
        return web.json_response({"code": 0})

    async def handle_publish_fail(request):
        return web.json_response({"code": -10000, "message": "发太快了"})

    async def handle_reply(request):
        form = await request.post()
        replies.append({"form": dict(form), "query": dict(request.query)})
        return web.json_response({"code": 0})

    async def handle_detail(request):
        details.append(dict(request.query))
        return web.json_response({"code": 0, "commentlist": detail_comments})

    async def handle_publish_ok(request):
        form = await request.post()
        received["publish"] = {
            "form": dict(form),
            "cookies": dict(request.cookies),
            "query": dict(request.query),
        }
        return web.json_response(
            {"code": 0, "data": {"tid": "TID_DRAFT", "now": 1700000999}}
        )

    app2 = web.Application()
    app2.router.add_post("/v1/chat/completions", handle_ai)
    app2.router.add_get("/feeds", handle_feeds)
    app2.router.add_post("/like", handle_like)
    app2.router.add_post("/comment", handle_comment)
    app2.router.add_post("/publish", handle_publish_ok)
    app2.router.add_post("/publish_fail", handle_publish_fail)
    app2.router.add_post("/reply", handle_reply)
    app2.router.add_get("/detail", handle_detail)
    runner2 = web.AppRunner(app2)
    await runner2.setup()
    site2 = web.TCPSite(runner2, "127.0.0.1", 8792)
    await site2.start()

    AI_BASE = "http://127.0.0.1:8792"

    def make_ai_config(**overrides) -> PluginConfig:
        """构造一份插件配置（AI 相关只涉及 AstrBot 提供商）。"""
        data = StubAstrBotConfig(dict(raw_config))
        for key, value in overrides.items():
            cfg_set(data, key, value)
        return PluginConfig(data, FakeContext(onebot))

    print("\n[13] AI 接入层（只走 AstrBot 提供商）")
    provider_ctx = FakeContext(onebot)
    provider_ctx.provider = FakeProvider("来自 AstrBot 提供商")
    ai_provider = AIClient(make_ai_config(), provider_ctx)

    check(
        "有提供商时判定可用",
        ai_provider.available() is True,
        str(ai_provider.available()),
    )
    check(
        "describe 说明用的是 AstrBot 提供商",
        "AstrBot 提供商" in ai_provider.describe(),
        ai_provider.describe(),
    )

    text = await ai_provider.chat(system_prompt="SYS-P", prompt="P")
    check("provider 返回正文", text == "来自 AstrBot 提供商", text)
    check(
        "provider 收到 system_prompt 与 prompt",
        provider_ctx.provider.calls[-1]["system_prompt"] == "SYS-P"
        and provider_ctx.provider.calls[-1]["prompt"] == "P",
        str(provider_ctx.provider.calls[-1])[:80],
    )

    no_provider_ctx = FakeContext(onebot)
    ai_none = AIClient(make_ai_config(), no_provider_ctx)
    check(
        "无提供商时判定不可用", ai_none.available() is False, str(ai_none.available())
    )
    check(
        "describe 提示去 AstrBot 面板配置",
        "服务提供商" in ai_none.describe(),
        ai_none.describe(),
    )
    await expect_raises_async(
        "无可用 AI 时给出可操作报错",
        lambda: ai_none.chat(system_prompt="S"),
        RuntimeError,
        "没有可用的 AI",
    )

    failing_ctx = FakeContext(onebot)

    class _FailingProvider:
        async def text_chat(self, **kwargs):
            raise RuntimeError("上游 500")

    failing_ctx.provider = _FailingProvider()
    await expect_raises_async(
        "提供商报错时被包装成可读信息",
        lambda: AIClient(make_ai_config(), failing_ctx).chat(system_prompt="S"),
        RuntimeError,
        "调用 AstrBot 提供商失败",
    )

    empty_ctx = FakeContext(onebot)
    empty_ctx.provider = FakeProvider("   ")
    await expect_raises_async(
        "提供商返回空内容时报错",
        lambda: AIClient(make_ai_config(), empty_ctx).chat(system_prompt="S"),
        RuntimeError,
        "返回内容为空",
    )

    # 插件里不应再存在任何「绕过 AstrBot 提供商」的配置项
    import json as _json

    schema = _json.loads((REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    for removed in (
        "llm_mode",
        "llm_api_base",
        "llm_api_key",
        "llm_model",
        "llm_timeout",
    ):
        check(f"配置里已移除直连 API 项 {removed}", removed not in schema, removed)
    for removed in ("life_source", "life_external_path"):
        check(f"配置里已移除与其他插件联动项 {removed}", removed not in schema, removed)
    check(
        "生活日程不再有读外部文件的方法",
        not hasattr(LifeManager, "external_path")
        and not hasattr(LifeManager, "source_mode"),
        "仍存在",
    )

    print("\n[14] 一体化生活日程")
    check(
        "JSON 提取支持代码围栏",
        extract_json_object('```json\n{"outfit": "裙子"}\n```') == {"outfit": "裙子"},
        "fail",
    )
    check(
        "JSON 提取不受字符串内花括号影响",
        extract_json_object('{"a": "含有{}的文本", "b": 2}')
        == {"a": "含有{}的文本", "b": 2},
        "fail",
    )
    check(
        "JSON 提取失败返回 None", extract_json_object("完全不是 JSON") is None, "fail"
    )
    check(
        "time_desc 边界",
        time_desc(3) == "深夜" and time_desc(10) == "上午" and time_desc(23) == "深夜",
        time_desc(10),
    )

    life_ai_calls = {"count": 0}

    async def life_chat_json(
        *, system_prompt, prompt=None, contexts=None, provider_id=None, feature=None
    ):
        life_ai_calls["count"] += 1
        return '```json\n{"outfit": "米色针织衫", "schedule": "上午写代码，下午去散步"}\n```'

    async def life_chat_text(
        *, system_prompt, prompt=None, contexts=None, provider_id=None, feature=None
    ):
        life_ai_calls["count"] += 1
        return "今天想在家躺平，点个外卖看剧。"

    async def life_chat_fail(
        *, system_prompt, prompt=None, contexts=None, provider_id=None, feature=None
    ):
        raise RuntimeError("AI 挂了")

    plugin.life._states.clear()
    plugin.ai.chat = life_chat_json
    state = await plugin.life.get_state()
    check(
        "builtin 生成日程",
        state is not None and state.outfit == "米色针织衫" and "散步" in state.schedule,
        str(state),
    )
    check("生成来源标记 builtin", state.source == "builtin", state.source)
    calls_before = life_ai_calls["count"]
    await plugin.life.get_state()
    check(
        "当天缓存命中不重复生成",
        life_ai_calls["count"] == calls_before,
        str(life_ai_calls["count"]),
    )
    await plugin.life.get_state(force=True)
    check(
        "force 强制重新生成",
        life_ai_calls["count"] == calls_before + 1,
        str(life_ai_calls["count"]),
    )

    plugin.life._states.clear()
    plugin.ai.chat = life_chat_text
    state = await plugin.life.get_state()
    check(
        "非 JSON 输出降级为文本日程",
        state.status == "ok"
        and "躺平" in state.schedule
        and state.outfit == "日常休闲装",
        str(state),
    )

    plugin.life._states.clear()
    plugin.ai.chat = life_chat_fail
    state = await plugin.life.get_state()
    check("AI 失败时标记 failed", state.status == "failed", str(state))

    plugin.life._states.clear()
    plugin.ai.chat = life_chat_json
    await plugin.life.get_state()
    life_context = await plugin.life.prompt_context()
    check(
        "prompt_context 含穿搭与日程",
        "穿搭" in life_context and "日程" in life_context,
        life_context[:60],
    )
    injection = await plugin.life.injection_text()
    check(
        "injection_text 含内在状态与对话原则",
        "内在状态" in injection and "对话原则" in injection,
        injection[:60],
    )
    life_prompt_text = plugin.life._fill(
        "日期：{date_str}｜{unknown_key}", {"date_str": "X"}
    )
    check("模板未提供字段留空", life_prompt_text == "日期：X｜", life_prompt_text)

    print("\n[15] 说说互动（默认只读）")
    plugin.api.LIST_URL = f"{AI_BASE}/feeds"
    plugin.api.DOLIKE_URL = f"{AI_BASE}/like"
    plugin.api.COMMENT_URL = f"{AI_BASE}/comment"

    async def comment_chat(
        *, system_prompt, prompt=None, contexts=None, provider_id=None, feature=None
    ):
        return "哈哈哈这天气我也喜欢"

    plugin.ai.chat = comment_chat
    cfg.set("interact_uins", ["999999"])
    cfg.set("interact_count", 3)
    cfg.set("interact_days", 3)
    cfg.set("interact_like", False)
    cfg.set("interact_comment", False)
    cfg.set("interact_skip_self", True)
    cfg.set("draft_for_comment", True)
    cfg.set("interact_enabled", True)

    feeds_payload[:] = [
        friend_post("T1", 1),
        {
            "uin": 123456,
            "tid": "T2",
            "name": "我自己",
            "content": "我发的说说",
            "created_time": now_ts - 2 * 3600,
        },
    ]
    plugin.interact._seen = []
    likes.clear()
    comments.clear()
    plugin.drafts.clear()
    result = await plugin.interact.run_once()
    check(
        "只读模式：每个好友只取窗口内最新一条",
        result.checked == 1 and result.skipped == 0 and result.stale == 0,
        result.summary(),
    )
    check(
        "只读模式不点赞不评论",
        len(likes) == 0 and len(comments) == 0,
        f"{len(likes)}/{len(comments)}",
    )
    check(
        "拉取参数带目标与条数",
        feeds_calls[-1].get("uin") == "999999" and feeds_calls[-1].get("num") == "3",
        str(feeds_calls[-1]),
    )

    result = await plugin.interact.run_once()
    check(
        "第二轮被去重",
        result.checked == 1 and result.skipped == 1 and result.liked == 0,
        result.summary(),
    )
    result = await plugin.interact.run_once(force=True)
    check(
        "force 忽略去重", result.checked == 1 and result.skipped == 0, result.summary()
    )

    print("\n[15a] 互动时间窗口：只看最近 N 天内最新的一条")
    plugin.interact._seen = []
    likes.clear()
    comments.clear()
    plugin.drafts.clear()
    cfg.set("interact_like", True)
    cfg.set("interact_comment", True)
    cfg.set("draft_for_comment", True)

    feeds_payload[:] = [friend_post("T_OLD", 24 * 5)]
    result = await plugin.interact.run_once()
    check(
        "最新一条在 5 天前：整条跳过",
        result.checked == 0
        and result.stale == 1
        and result.liked == 0
        and result.commented == 0
        and result.drafted == 0,
        result.summary(),
    )
    check(
        "窗口外不发任何请求",
        len(likes) == 0 and len(comments) == 0 and plugin.drafts.pending is None,
        f"{len(likes)}/{len(comments)}",
    )
    check("窗口外写在汇总里", "超出时间窗口" in result.summary(), result.summary())

    plugin.interact._seen = []
    comments.clear()
    plugin.drafts.clear()
    cfg.set("interact_like", False)
    feeds_payload[:] = [friend_post("T_TWO_DAYS", 24 * 2), friend_post("T_NEW", 1)]
    result = await plugin.interact.run_once()
    pending_comment = plugin.drafts.pending
    check(
        "窗口内多条只处理最新一条",
        result.checked == 1
        and result.drafted == 1
        and pending_comment is not None
        and pending_comment.target_tid == "T_NEW",
        result.summary(),
    )

    plugin.interact._seen = []
    plugin.drafts.clear()
    feeds_payload[:] = [friend_post("T_2DAYS", 24 * 2)]
    cfg.set("interact_days", 1)
    result = await plugin.interact.run_once()
    check(
        "interact_days=1 时 2 天前的说说被跳过",
        result.checked == 0 and result.stale == 1,
        result.summary(),
    )
    cfg.set("interact_days", 3)
    result = await plugin.interact.run_once()
    check(
        "interact_days=3 时同一条会被处理",
        result.checked == 1 and result.stale == 0,
        result.summary(),
    )

    plugin.interact._seen = []
    plugin.drafts.clear()
    feeds_payload[:] = [
        friend_post("T_FRIEND", 5),
        {
            "uin": 123456,
            "tid": "T_SELF",
            "name": "我自己",
            "content": "我发的说说",
            "created_time": now_ts - 3600,
        },
    ]
    result = await plugin.interact.run_once()
    check(
        "窗口内最新一条是自己发的：跳过自己",
        result.checked == 1 and result.skipped == 1 and result.drafted == 0,
        result.summary(),
    )

    plugin.interact._seen = []
    plugin.drafts.clear()
    feeds_payload[:] = [
        {
            "uin": 999999,
            "tid": "T_NOTIME",
            "name": "小明",
            "content": "没有时间戳的说说",
            "created_time": 0,
        }
    ]
    result = await plugin.interact.run_once()
    check(
        "时间戳缺失时按窗口内处理，不整批跳过",
        result.checked == 1 and result.stale == 0,
        result.summary(),
    )

    # 复原成默认那条，后面的指令测试继续用它
    feeds_payload[:] = [friend_post("T1", 1)]
    plugin.interact._seen = []
    plugin.drafts.clear()
    cfg.set("interact_comment", False)
    cfg.set("interact_days", 3)

    plugin.interact._seen = []
    likes.clear()
    cfg.set("interact_like", True)
    result = await plugin.interact.run_once()
    check("开启点赞后发出请求", result.liked == 1 and len(likes) == 1, result.summary())
    check(
        "点赞参数含 unikey 与 g_tk",
        "999999/mood/T1" in str(likes[-1]["form"].get("unikey"))
        and likes[-1]["query"].get("g_tk"),
        str(likes[-1])[:100],
    )

    plugin.interact._seen = []
    comments.clear()
    plugin.drafts.clear()
    cfg.set("interact_comment", True)
    cfg.set("draft_for_comment", True)
    result = await plugin.interact.run_once()
    pending_comment = plugin.drafts.pending
    check(
        "评论转草稿且不直接发送",
        result.drafted == 1 and len(comments) == 0 and pending_comment is not None,
        result.summary(),
    )
    check(
        "评论草稿带目标与上下文",
        pending_comment.kind == "comment"
        and pending_comment.target_tid == "T1"
        and pending_comment.target_uin == 999999
        and "天气" in pending_comment.target_text,
        str(pending_comment)[:100],
    )

    plugin.interact._seen = []
    comments.clear()
    plugin.drafts.clear()
    cfg.set("draft_for_comment", False)
    result = await plugin.interact.run_once()
    check(
        "关闭草稿后直接评论",
        result.commented == 1 and len(comments) == 1 and plugin.drafts.pending is None,
        result.summary(),
    )
    check(
        "评论表单 topicId/hostUin 正确",
        comments[-1]["form"].get("topicId") == "999999_T1__1"
        and comments[-1]["form"].get("hostUin") == "999999"
        and comments[-1]["form"].get("content") == "哈哈哈这天气我也喜欢",
        str(comments[-1]["form"])[:100],
    )

    plugin.interact._seen = []
    cfg.set("interact_uins", [])
    result = await plugin.interact.run_once()
    check(
        "未配置关注对象时给出错误提示",
        result.checked == 0 and bool(result.errors),
        result.summary(),
    )

    cfg.set("interact_uins", ["999999"])
    cfg.set("interact_like", False)
    cfg.set("interact_comment", True)
    cfg.set("draft_for_comment", True)
    check(
        "互动模式描述",
        plugin.interact.mode_text() == "只读 + 3 天内最新一条 + 评论(先确认)",
        plugin.interact.mode_text(),
    )

    print("\n[16] 草稿箱与草稿流程")
    box_path = DATA_DIR / "draft_unit.json"
    box = DraftBox(box_path)
    box.put(
        Draft(
            kind="comment",
            text="草稿正文",
            target_uin=999999,
            target_tid="T1",
            target_name="小明",
        )
    )
    reloaded_box = DraftBox(box_path)
    check(
        "草稿持久化往返",
        reloaded_box.pending is not None
        and reloaded_box.pending.kind == "comment"
        and reloaded_box.pending.target_tid == "T1",
        str(reloaded_box.pending)[:80],
    )
    check(
        "草稿描述含确认指令提示",
        "空间确认" in reloaded_box.pending.describe()
        and "草稿正文" in reloaded_box.pending.describe(),
        reloaded_box.pending.describe()[:80],
    )
    popped = reloaded_box.pop()
    check(
        "取出后草稿清空",
        popped is not None
        and popped.text == "草稿正文"
        and reloaded_box.pending is None,
        str(popped),
    )
    check("无草稿时 clear 返回 False", reloaded_box.clear() is False)

    plugin.api.EMOTION_URL = f"{AI_BASE}/publish"
    cfg.set("draft_enabled", True)
    cfg.set("draft_admin", True)
    cfg.set("draft_umo", "aiocqhttp:FriendMessage:5555")
    cfg.set("content_source", "pool")
    cfg.set("text_pool", ["草稿模式生成的说说"])
    plugin.drafts.clear()
    StarTools.sent.clear()
    history_before = len(plugin.store._records)
    await plugin._auto_publish()
    pending_post = plugin.drafts.pending
    check(
        "草稿模式不直接发布",
        pending_post is not None
        and pending_post.kind == "post"
        and len(plugin.store._records) == history_before,
        f"{pending_post}/{len(plugin.store._records)}",
    )
    sent_targets = [item[0] for item in StarTools.sent]
    check(
        "草稿同时发管理员与指定会话",
        "aiocqhttp:FriendMessage:10001" in sent_targets
        and "aiocqhttp:FriendMessage:5555" in sent_targets,
        str(sent_targets),
    )
    check(
        "草稿不会顺带发到 notify_umo",
        "aiocqhttp:FriendMessage:123456" not in sent_targets,
        str(sent_targets),
    )

    out = await collect(plugin.cmd_confirm(FakeEvent()))
    check(
        "确认草稿后发布成功",
        any("草稿已发布" in item for item in out) and plugin.drafts.pending is None,
        str(out)[:120],
    )
    check(
        "确认后写入历史",
        plugin.store.last_success() is not None
        and plugin.store.last_success().text == "草稿模式生成的说说",
        str(plugin.store.last_success())[:80],
    )

    out = await collect(plugin.cmd_confirm(FakeEvent()))
    check(
        "无草稿时确认给出提示", any("没有待确认" in item for item in out), str(out)[:80]
    )

    plugin.drafts.put(Draft(kind="post", text="要被丢弃的内容"))
    out = await collect(plugin.cmd_drop(FakeEvent()))
    check(
        "放弃草稿生效",
        plugin.drafts.pending is None and any("已丢弃" in item for item in out),
        str(out),
    )

    plugin.ai.chat = comment_chat
    plugin.drafts.put(Draft(kind="post", text="旧的一版内容"))
    out = await collect(plugin.cmd_redo(FakeEvent()))
    check(
        "重写草稿走 AI 并替换内容",
        plugin.drafts.pending is not None
        and plugin.drafts.pending.text != "旧的一版内容",
        str(plugin.drafts.pending)[:80],
    )
    check(
        "重写来源标记",
        plugin.drafts.pending.source == "rewrite",
        plugin.drafts.pending.source,
    )

    plugin.api.EMOTION_URL = f"{AI_BASE}/publish_fail"
    plugin.drafts.put(Draft(kind="post", text="注定失败的内容"))
    out = await collect(plugin.cmd_confirm(FakeEvent()))
    check(
        "发布失败时草稿保留",
        plugin.drafts.pending is not None and any("发布失败" in item for item in out),
        str(out)[:100],
    )
    plugin.api.EMOTION_URL = f"{AI_BASE}/publish"
    plugin.drafts.clear()

    print("\n[17] 指令与提示词注入")
    out = await collect(plugin.cmd_life(FakeEvent(), ""))
    check(
        "日程指令输出穿搭与日程",
        any("穿搭" in item and "日程" in item for item in out),
        str(out)[:100],
    )
    out = await collect(plugin.cmd_life(FakeEvent(), "renew"))
    check("日程指令支持 renew", any("生活状态" in item for item in out), str(out)[:100])

    out = await collect(plugin.cmd_status(FakeEvent()))
    status_text = out[0]
    check("状态含 AI 接入信息", "AI 接入" in status_text, status_text[:100])
    check(
        "状态含互动/草稿/日程",
        "说说互动" in status_text
        and "草稿确认" in status_text
        and "生活日程" in status_text,
        status_text[:160],
    )

    out = await collect(plugin.cmd_interact(FakeEvent(), ""))
    check(
        "互动指令展示模式",
        any("说说互动当前为" in item for item in out),
        str(out)[:100],
    )
    out = await collect(plugin.cmd_interact(FakeEvent(), "off"))
    check(
        "互动指令关闭任务",
        raw_config["sec_interact"]["interact_enabled"] is False
        and not plugin.interact_task.running,
        str(out),
    )
    out = await collect(plugin.cmd_interact(FakeEvent(), "on"))
    check(
        "互动指令开启任务",
        raw_config["sec_interact"]["interact_enabled"] is True
        and plugin.interact_task.running,
        str(out),
    )
    plugin.interact._seen = []
    out = await collect(plugin.cmd_read(FakeEvent(), ""))
    check("读说说指令跑完一轮", any("巡检完成" in item for item in out), str(out)[:120])

    cfg.set("life_inject_enabled", False)
    req = types.SimpleNamespace(system_prompt="BASE")
    await plugin.on_llm_request(FakeEvent(), req)
    check("默认不注入生活状态", req.system_prompt == "BASE", req.system_prompt)

    cfg.set("life_inject_enabled", True)
    plugin.life._states.clear()
    plugin.ai.chat = life_chat_json
    await plugin.life.get_state()
    req2 = types.SimpleNamespace(system_prompt="BASE")
    await plugin.on_llm_request(FakeEvent(), req2)
    check(
        "开启后注入生活状态",
        "内在状态" in req2.system_prompt and req2.system_prompt.startswith("BASE"),
        req2.system_prompt[:80],
    )

    print("\n[18] 按功能选择 AstrBot 提供商")
    provider_global = FakeProvider("全局提供商输出")
    provider_life = FakeProvider('{"outfit": "米色风衣", "schedule": "上午开会"}')
    provider_comment = FakeProvider("评论提供商输出")
    provider_greet = FakeProvider("问候提供商输出")

    ctx_multi = FakeContext(onebot)
    ctx_multi.providers = {
        "global-model": provider_global,
        "life-model": provider_life,
        "comment-model": provider_comment,
        "greet-model": provider_greet,
    }
    multi_data = StubAstrBotConfig(dict(raw_config))
    for key, value in {
        "llm_provider_id": "global-model",
        "llm_life_provider_id": "life-model",
        "llm_comment_provider_id": "comment-model",
        "llm_greet_provider_id": "greet-model",
        "life_inject_enabled": False,
        "interact_comment_prompt": "随便评一句",
        "interact_comment_max_chars": 60,
    }.items():
        cfg_set(multi_data, key, value)
    multi_cfg = PluginConfig(multi_data, ctx_multi)
    ai_multi = AIClient(multi_cfg, ctx_multi)

    check(
        "overrides_text 列出单独指定的功能",
        ai_multi.overrides_text()
        == "日程=life-model，评论=comment-model，问候=greet-model",
        ai_multi.overrides_text(),
    )
    check(
        "指定提供商时判定可用",
        ai_multi.available() is True and ai_multi.available("life-model") is True,
        str(ai_multi.available()),
    )

    life_multi = LifeManager(multi_cfg, ctx_multi, ai_multi)
    # 前面几轮已在同一数据目录写过今日缓存，强制重新生成才能验证走的是哪个提供商
    state = await life_multi.get_state(force=True)
    check(
        "日程走单独指定的提供商",
        state.outfit == "米色风衣"
        and provider_life.calls
        and not provider_global.calls,
        f"{state.outfit}/life={len(provider_life.calls)}/global={len(provider_global.calls)}",
    )

    interact_multi = InteractService(
        multi_cfg, ai_multi, plugin.api, DraftBox(DATA_DIR / "draft_multi.json")
    )
    comment_text = await interact_multi._generate_comment(
        FeedPost(uin=999999, tid="T9", name="小红", text="今天很开心")
    )
    check(
        "评论走单独指定的提供商",
        comment_text == "评论提供商输出"
        and provider_comment.calls
        and not provider_global.calls,
        f"{comment_text}/comment={len(provider_comment.calls)}",
    )

    empty_data = StubAstrBotConfig(dict(multi_data))
    cfg_set(empty_data, "llm_life_provider_id", "")
    cfg_set(empty_data, "llm_comment_provider_id", "")
    cfg_set(empty_data, "llm_greet_provider_id", "")
    empty_cfg = PluginConfig(empty_data, ctx_multi)
    check(
        "留空时不显示单独指定",
        AIClient(empty_cfg, ctx_multi).overrides_text() == "",
        "fail",
    )
    ai_empty_multi = AIClient(empty_cfg, ctx_multi)
    await ai_empty_multi.chat(system_prompt="S", prompt="P", provider_id="")
    check(
        "留空时回退到全局提供商",
        provider_global.calls and provider_global.calls[-1]["system_prompt"] == "S",
        str(len(provider_global.calls)),
    )
    await ai_empty_multi.chat(system_prompt="S2", provider_id="comment-model")
    check(
        "显式传 provider_id 时生效",
        provider_comment.calls[-1]["system_prompt"] == "S2",
        str(provider_comment.calls[-1])[:50],
    )

    print("\n[19] 接入 AstrBot 自带联网搜索")
    WebSearchBridge = _imp("core.web").WebSearchBridge

    default_payload = FakeSearchTool("x").payload
    tavily_tool = FakeSearchTool("web_search_tavily")
    bocha_tool = FakeSearchTool("web_search_bocha")
    baidu_tool = FakeSearchTool("web_search_baidu")
    web_ctx = FakeContext(onebot)
    web_ctx.tool_manager = FakeToolManager(
        {
            "web_search_tavily": tavily_tool,
            "web_search_bocha": bocha_tool,
            "web_search_baidu": baidu_tool,
        }
    )
    web_data = StubAstrBotConfig(dict(raw_config))
    for key, value in {
        "web_search_enabled": True,
        "web_search_query_mode": "fixed",
        "web_search_query_pool": ["固定关键词"],
        "web_search_count": 5,
    }.items():
        cfg_set(web_data, key, value)
    web_cfg = PluginConfig(web_data, web_ctx)
    bridge = WebSearchBridge(web_cfg, web_ctx)

    web_ctx.provider_settings = {"web_search": False, "websearch_provider": "tavily"}
    ok, reason = bridge.readiness()
    check("AstrBot 未开启联网时不可用", ok is False and "未开启" in reason, reason)

    web_ctx.provider_settings = {
        "web_search": True,
        "websearch_provider": "tavily",
        "websearch_tavily_key": [],
    }
    ok, reason = bridge.readiness()
    check("密钥为空时不可用并说明原因", ok is False and "密钥" in reason, reason)
    calls_before = len(tavily_tool.calls)
    empty_outcome = await bridge.search("测试")
    check(
        "密钥为空时不去调用工具",
        len(tavily_tool.calls) == calls_before and not empty_outcome.hits,
        empty_outcome.error,
    )

    web_ctx.provider_settings = {
        "web_search": True,
        "websearch_provider": "unknown_provider",
        "websearch_tavily_key": ["k"],
    }
    ok, reason = bridge.readiness()
    check("未知服务商不可用", ok is False and "不在已知列表" in reason, reason)

    web_ctx.provider_settings = {
        "web_search": True,
        "websearch_provider": "tavily",
        "websearch_tavily_key": ["key-1"],
    }
    info = bridge.settings()
    check(
        "tavily 映射到内置工具与参数名",
        info["tool_name"] == "web_search_tavily"
        and info["count_param"] == "max_results"
        and info["key_ready"] is True,
        str(info),
    )
    ok, reason = bridge.readiness()
    check("就绪状态正确", ok and "tavily" in reason, reason)

    outcome = await bridge.search(
        "春日骑行", count=5, umo="aiocqhttp:FriendMessage:123456"
    )
    check(
        "搜索解析出两条结果",
        len(outcome.hits) == 2 and outcome.hits[0].title == "标题一",
        str(outcome.hits[:1]),
    )
    check(
        "按服务商适配条数参数（max_results）并传 query",
        tavily_tool.calls[-1].get("max_results") == 5
        and tavily_tool.calls[-1].get("query") == "春日骑行",
        str(tavily_tool.calls[-1]),
    )
    passed_umo = getattr(
        getattr(tavily_tool.agent_context, "event", None), "unified_msg_origin", ""
    )
    check(
        "传给工具的上下文带会话标识",
        passed_umo == "aiocqhttp:FriendMessage:123456",
        passed_umo,
    )
    check(
        "复用了 AstrBot 自带的配置归一化",
        NORMALIZE_CALLS["count"] > 0,
        str(NORMALIZE_CALLS["count"]),
    )
    check(
        "解析出结果域名",
        outcome.hits[0].domain == "news.example.com",
        outcome.hits[0].domain,
    )

    material = WebSearchBridge.format_for_prompt(outcome.hits)
    check(
        "素材含标题与摘要", "标题一" in material and "摘要一" in material, material[:60]
    )
    check("素材不含完整链接", "http" not in material, material[:80])
    check("素材标注来源域名", "news.example.com" in material, material[:80])
    listing = WebSearchBridge.format_hits(outcome.hits)
    check(
        "人工查看的列表含完整链接",
        "https://news.example.com/a" in listing,
        listing[:80],
    )

    web_ctx.provider_settings = {
        "web_search": True,
        "websearch_provider": "bocha",
        "websearch_bocha_key": ["k"],
    }
    await bridge.search("问题", count=3)
    check(
        "bocha 用 count 参数",
        bocha_tool.calls[-1].get("count") == 3
        and "max_results" not in bocha_tool.calls[-1],
        str(bocha_tool.calls[-1]),
    )

    web_ctx.provider_settings = {
        "web_search": True,
        "websearch_provider": "baidu_ai_search",
        "websearch_baidu_app_builder_key": "baidu-key",
    }
    check(
        "baidu 的字符串密钥也能识别",
        bridge.settings()["key_ready"] is True,
        str(bridge.settings()),
    )
    await bridge.search("问题", count=3)
    check(
        "无条数参数的服务商只传 query",
        set(baidu_tool.calls[-1]) == {"query"},
        str(baidu_tool.calls[-1]),
    )

    web_ctx.provider_settings = {
        "web_search": True,
        "websearch_provider": "tavily",
        "websearch_tavily_key": "legacy-key",
    }
    check(
        "旧格式字符串密钥也识别",
        bridge.settings()["key_ready"] is True,
        str(bridge.settings()),
    )

    web_ctx.provider_settings = {
        "web_search": True,
        "websearch_provider": "tavily",
        "websearch_tavily_key": ["k"],
    }
    tavily_tool.payload = "Error: Tavily API key is not configured in AstrBot."
    outcome = await bridge.search("问题")
    check(
        "工具返回 Error 被识别为失败",
        not outcome.hits and "Error" in outcome.error,
        outcome.error,
    )
    check("失败原因记录在 last_error", "Error" in bridge.last_error, bridge.last_error)

    tavily_tool.payload = default_payload
    tavily_tool.error = RuntimeError("网络断了")
    outcome = await bridge.search("问题")
    check(
        "工具抛异常被兜住", not outcome.hits and "异常" in outcome.error, outcome.error
    )

    tavily_tool.error = None
    tavily_tool.payload = "<html>不是 JSON</html>"
    outcome = await bridge.search("问题")
    check(
        "非 JSON 返回被兜住",
        not outcome.hits and "无法解析" in outcome.error,
        outcome.error,
    )

    tavily_tool.payload = default_payload
    web_ctx.tool_manager = FakeToolManager({})
    outcome = await bridge.search("问题")
    check(
        "取不到内置工具时说明原因",
        not outcome.hits and "未取到内置工具" in outcome.error,
        outcome.error,
    )
    web_ctx.tool_manager = FakeToolManager({"web_search_tavily": tavily_tool})

    # 模拟老版本 AstrBot（没有内置联网搜索工具管理器）：必须优雅降级并提示版本要求
    web_ctx.tool_manager = None
    ok, reason = bridge.readiness()
    check(
        "老版本 AstrBot 上降级并提示版本要求",
        ok is False and "未取到内置工具" in reason and "4.26" in reason,
        reason,
    )
    old_outcome = await bridge.search("问题")
    check(
        "老版本上搜索失败但不抛异常",
        not old_outcome.hits and bool(old_outcome.error),
        old_outcome.error,
    )
    web_ctx.tool_manager = FakeToolManager({"web_search_tavily": tavily_tool})

    print("\n[20] 内容生成接入联网素材")
    cfg.set("web_search_enabled", True)
    cfg.set("web_search_query_mode", "ai")
    cfg.set("web_search_query_pool", ["摄影", "露营"])
    plugin.context.provider_settings = {
        "web_search": True,
        "websearch_provider": "tavily",
        "websearch_tavily_key": ["k"],
    }
    plugin.context.tool_manager = FakeToolManager({"web_search_tavily": tavily_tool})
    tavily_tool.calls.clear()

    web_ai_calls: list[dict] = []

    async def web_ai(
        *, system_prompt, prompt=None, contexts=None, provider_id=None, feature=None
    ):
        web_ai_calls.append({"system_prompt": system_prompt, "prompt": prompt})
        if "搜索关键词" in system_prompt:
            return "“春日骑行”"
        return "今天骑车出门，风很舒服。"

    plugin.ai.chat = web_ai
    text = await plugin.content.rewrite()
    check("联网素材模式下生成成功", text == "今天骑车出门，风很舒服。", text)
    check(
        "AI 拟的搜索词被清理后传给工具",
        tavily_tool.calls and tavily_tool.calls[-1].get("query") == "春日骑行",
        str(tavily_tool.calls[-1:]),
    )
    generation_prompt = web_ai_calls[-1]["system_prompt"]
    check(
        "素材被拼进提示词",
        "刚刚联网查到的近期资料" in generation_prompt and "标题一" in generation_prompt,
        generation_prompt[-160:],
    )
    check(
        "带防幻觉规则",
        "不要照抄标题" in generation_prompt and "不要贴链接" in generation_prompt,
        generation_prompt[-120:],
    )
    check(
        "联网素材计入来源标记",
        plugin.web.status_text().startswith("AstrBot")
        or "tavily" in plugin.web.status_text(),
        plugin.web.status_text(),
    )

    cfg.set("web_search_query_mode", "fixed")
    cfg.set("web_search_query_pool", ["固定关键词"])
    web_ai_calls.clear()
    tavily_tool.calls.clear()
    await plugin.content.rewrite()
    check(
        "fixed 模式用关键词池且不再让 AI 拟词",
        tavily_tool.calls[-1].get("query") == "固定关键词" and len(web_ai_calls) == 1,
        f"{tavily_tool.calls[-1:]}/{len(web_ai_calls)}",
    )

    cfg.set("web_search_query_mode", "ai")
    tavily_tool.error = RuntimeError("boom")
    web_ai_calls.clear()
    text = await plugin.content.rewrite()
    check(
        "联网失败时降级为普通生成",
        text == "今天骑车出门，风很舒服。"
        and "刚刚联网查到的近期资料" not in web_ai_calls[-1]["system_prompt"],
        text,
    )
    tavily_tool.error = None

    cfg.set("web_search_enabled", False)
    tavily_tool.calls.clear()
    await plugin.content.rewrite()
    check("关闭联网时不发起搜索", not tavily_tool.calls, str(len(tavily_tool.calls)))
    cfg.set("web_search_enabled", True)

    print("\n[21] 联网相关指令")
    out = await collect(plugin.cmd_search(FakeEvent(), ""))
    check(
        "不带参数时显示接入状态",
        any("联网素材开关" in item and "AstrBot 联网搜索" in item for item in out),
        str(out)[:140],
    )
    out = await collect(plugin.cmd_search(FakeEvent(), "露营装备"))
    check(
        "搜索指令返回结果与链接",
        any("搜到 2 条" in item and "news.example.com" in item for item in out),
        str(out)[:160],
    )
    tavily_tool.error = RuntimeError("boom")
    out = await collect(plugin.cmd_search(FakeEvent(), "露营装备"))
    check(
        "搜索失败时提示去哪排查",
        any("搜索失败" in item and "AstrBot 面板" in item for item in out),
        str(out)[:160],
    )
    tavily_tool.error = None

    out = await collect(plugin.cmd_status(FakeEvent()))
    check(
        "状态指令含联网素材行", any("联网素材" in item for item in out), str(out)[:200]
    )

    print("\n[22] 管理员识别")
    admin_cfg = StubAstrBotConfig(dict(raw_config))
    admin_ctx = FakeContext(onebot, admins=["10001", "10002", "not-a-qq"])
    admin_plugin = QzonePublisherPlugin(admin_ctx, admin_cfg)

    qqs, source = admin_plugin._admin_qqs()
    check(
        "默认沿用 AstrBot 的 admins_id 并过滤非数字",
        qqs == ["10001", "10002"] and "AstrBot" in source,
        f"{qqs}/{source}",
    )
    check(
        "管理员私聊 UMO 拼装正确",
        admin_plugin._admin_umos()
        == ["aiocqhttp:FriendMessage:10001", "aiocqhttp:FriendMessage:10002"],
        str(admin_plugin._admin_umos()),
    )

    cfg_set(admin_cfg, "admin_uins", ["20001"])
    qqs, source = admin_plugin._admin_qqs()
    check(
        "插件内名单优先于 AstrBot 的",
        qqs == ["20001"] and "插件配置" in source,
        f"{qqs}/{source}",
    )
    check(
        "草稿接收人跟着插件名单走",
        admin_plugin._admin_umos() == ["aiocqhttp:FriendMessage:20001"],
        str(admin_plugin._admin_umos()),
    )

    out = await collect(admin_plugin.cmd_admin(FakeEvent(), ""))
    check(
        "管理员指令显示名单与来源",
        any("管理员名单" in item and "20001" in item for item in out),
        str(out)[:120],
    )
    out = await collect(admin_plugin.cmd_admin(FakeEvent(), "add 30003"))
    check(
        "add 生效并写回配置",
        cfg_peek(admin_cfg, "admin_uins") == ["20001", "30003"]
        and any("已添加" in item for item in out),
        str(cfg_peek(admin_cfg, "admin_uins")),
    )
    out = await collect(admin_plugin.cmd_admin(FakeEvent(), "add 30003"))
    check("重复添加给出提示", any("已在名单" in item for item in out), str(out)[:80])
    out = await collect(admin_plugin.cmd_admin(FakeEvent(), "remove 20001"))
    check(
        "remove 生效",
        cfg_peek(admin_cfg, "admin_uins") == ["30003"],
        str(cfg_peek(admin_cfg, "admin_uins")),
    )
    out = await collect(admin_plugin.cmd_admin(FakeEvent(), "add abc"))
    check("非数字 QQ 被拒绝", any("不是纯数字" in item for item in out), str(out)[:80])
    out = await collect(admin_plugin.cmd_admin(FakeEvent(), "乱写"))
    check("未知子命令给出用法", any("用法" in item for item in out), str(out)[:80])

    out = await collect(admin_plugin.cmd_status(FakeEvent()))
    check(
        "状态里显示管理员与来源",
        any("管理员:" in item and "插件配置 admin_uins" in item for item in out),
        str(out)[:200],
    )

    print("\n[23] 定时问候")
    greet_cfg = StubAstrBotConfig(dict(raw_config))
    for key, value in {
        "greet_enabled": True,
        "greet_users": ["10001", "10002"],
        "greet_morning_pool": ["早上好呀"],
        "greet_night_pool": ["晚安咯"],
        "greet_use_ai": False,
        "llm_greet_provider_id": "greet-model",
        "greet_prompt": "写一句{slot}问候",
    }.items():
        cfg_set(greet_cfg, key, value)
    greet_ctx = FakeContext(onebot)
    greet_ctx.providers = {
        "greet-model": provider_greet,
        "global-model": provider_global,
    }
    greet_cfg_obj = PluginConfig(greet_cfg, greet_ctx)
    greet_ai = AIClient(greet_cfg_obj, greet_ctx)

    sent_messages: list[tuple[str, str]] = []

    async def fake_sender(umo: str, text: str) -> int:
        sent_messages.append((umo, text))
        return 1

    greet = GreetingService(
        greet_cfg_obj, greet_ai, lambda: "aiocqhttp", sender=fake_sender
    )
    check("问候目标来自配置", greet.targets == ["10001", "10002"], str(greet.targets))
    check(
        "问候 UMO 是私聊格式",
        greet.umo_for("10001") == "aiocqhttp:FriendMessage:10001",
        greet.umo_for("10001"),
    )

    morning = greet.slot_of("morning")
    check(
        "认识 morning/night 两个时段",
        morning is not None and greet.slot_of("night") is not None,
        str(morning),
    )
    check("未知时段返回 None", greet.slot_of("noon") is None, "fail")
    check("早安文案取自文案池", await greet.build_text(morning) == "早上好呀", "fail")

    sent_messages.clear()
    result = await greet.send("morning")
    check(
        "早安发给所有人",
        result.sent == 2 and len(sent_messages) == 2,
        f"{result.sent}/{len(sent_messages)}",
    )
    check(
        "同一时段当天不重复发送",
        (await greet.send("morning")).skipped == 2,
        "fail",
    )
    check(
        "force 可忽略去重（手动测试用）",
        (await greet.send("morning", force=True)).sent == 2,
        "fail",
    )
    check(
        "今日已发计数正确",
        greet.sent_today("morning") == 2,
        str(greet.sent_today("morning")),
    )

    cfg_set(greet_cfg, "greet_users", [])
    morning_again = greet.slot_of("morning")
    result = await greet.send("morning")
    check(
        "未配置目标时给出提示且不发",
        result.sent == 0 and bool(result.errors),
        result.summary(),
    )
    cfg_set(greet_cfg, "greet_users", ["10001"])
    _ = morning_again

    await expect_raises_async(
        "未定义时段报错",
        lambda: greet.send("noon"),
        RuntimeError,
        "未知的问候时段",
    )

    cfg_set(greet_cfg, "greet_night_pool", [])
    night = greet.slot_of("night")
    await expect_raises_async(
        "文案池为空且未开 AI 时报错",
        lambda: greet.build_text(night),
        RuntimeError,
        "文案池为空",
    )

    cfg_set(greet_cfg, "greet_use_ai", True)
    cfg_set(greet_cfg, "greet_night_pool", ["晚安咯"])
    provider_greet.calls.clear()
    provider_global.calls.clear()
    ai_text = await greet.build_text(greet.slot_of("night"))
    check("开启 AI 后用提供商生成问候", ai_text == "问候提供商输出", ai_text)
    check(
        "问候走单独指定的提供商且提示词带时段名",
        provider_greet.calls
        and provider_greet.calls[-1]["system_prompt"].startswith("写一句晚安问候")
        and not provider_global.calls,
        str(provider_greet.calls[-1])[:80],
    )

    greet_ctx.providers["greet-model"] = FakeProvider("")  # 返回空 → 回退文案池
    fallback_text = await greet.build_text(greet.slot_of("night"))
    check("AI 返回空时回退文案池", fallback_text == "晚安咯", fallback_text)

    cfg_set(greet_cfg, "greet_use_ai", False)
    plugin.cfg.set("greet_enabled", True)
    plugin.cfg.set("greet_users", ["10001"])
    plugin.cfg.set("greet_use_ai", False)
    plugin.cfg.set("greet_morning_pool", ["早上好呀"])
    plugin.cfg.set("greet_night_pool", ["晚安咯"])
    plugin.cfg.set("llm_greet_provider_id", "")
    plugin.greet._sent.clear()

    out = await collect(plugin.cmd_greet(FakeEvent(), ""))
    check(
        "问候指令显示开关与对象",
        any("问候开关" in item and "问候对象" in item for item in out),
        str(out)[:140],
    )
    out = await collect(plugin.cmd_greet(FakeEvent(), "afternoon"))
    check(
        "问候指令拒绝未知参数",
        any("morning 123456" in item for item in out),
        str(out)[:80],
    )

    StarTools.sent.clear()
    out = await collect(plugin.cmd_greet(FakeEvent(), "morning 10001"))
    sent_targets = [item[0] for item in StarTools.sent]
    check(
        "问候指令能立即私聊发送",
        any("成功 1 人" in item for item in out)
        and "aiocqhttp:FriendMessage:10001" in sent_targets,
        f"{str(out)[:80]}/{sent_targets}",
    )

    plugin.cfg.set("greet_users", [])
    out = await collect(plugin.cmd_greet(FakeEvent(), "morning"))
    check(
        "没目标也没指定 QQ 时提示用法",
        any("greet_users 也是空的" in item for item in out),
        str(out)[:120],
    )
    plugin.cfg.set("greet_users", ["10001"])

    check(
        "早安定时任务读到了配置时间",
        plugin.greet_morning_task.cron == "0 8 * * *",
        str(plugin.greet_morning_task.cron),
    )
    check(
        "晚安定时任务读到了配置时间",
        plugin.greet_night_task.cron == "0 23 * * *",
        str(plugin.greet_night_task.cron),
    )
    check(
        "问候任务带抖动",
        plugin.greet_morning_task.jitter == int(plugin.cfg.greet_jitter),
        str(plugin.greet_morning_task.jitter),
    )
    cron_morning = plugin.greet_morning_task.start()
    check(
        "早安任务可以启动并给出下次时间",
        cron_morning is None and not plugin.greet_morning_task.running,
        f"{cron_morning}/{plugin.greet_morning_task.next_run_time}",
    )
    out = await collect(plugin.cmd_greet(FakeEvent(), "on"))
    check(
        "问候指令可开启定时任务",
        plugin.greet_morning_task.running
        and plugin.greet_night_task.running
        and any("已开启" in item for item in out),
        str(out)[:160],
    )
    check(
        "开启后给出两个时段的下次时间",
        plugin.greet_morning_task.next_run_time != "未调度"
        and plugin.greet_night_task.next_run_time != "未调度",
        f"{plugin.greet_morning_task.next_run_time}/{plugin.greet_night_task.next_run_time}",
    )
    out = await collect(plugin.cmd_greet(FakeEvent(), "off"))
    check(
        "问候指令可关闭定时任务",
        not plugin.greet_morning_task.running
        and not plugin.greet_night_task.running
        and any("已关闭" in item for item in out),
        str(out)[:120],
    )

    plugin.greet._sent.clear()
    StarTools.sent.clear()
    await plugin._run_greet("morning")
    targets = [item[0] for item in StarTools.sent]
    check(
        "定时任务链路给目标发私聊",
        "aiocqhttp:FriendMessage:10001" in targets,
        str(targets),
    )
    check(
        "并给 notify_umo 发汇总",
        "aiocqhttp:FriendMessage:123456" in targets,
        str(targets),
    )

    out = await collect(plugin.cmd_status(FakeEvent()))
    check(
        "状态含定时问候与今日已发",
        any("定时问候" in item and "今日已发" in item for item in out),
        str(out)[-200:],
    )

    print("\n[24] Token 用量估算与「AI 自己确认并引用」")
    estimate_tokens = _imp("core.usage").estimate_tokens
    check("空文本不产生用量", estimate_tokens("") == 0, str(estimate_tokens("")))
    cn_tokens = estimate_tokens("今天天气不错")
    en_tokens = estimate_tokens("hello world")
    check("中文约 0.7 token/字", 3 <= cn_tokens <= 9, str(cn_tokens))
    check("英文约 1 token/4 字符", 1 <= en_tokens <= 5, str(en_tokens))
    check(
        "长文本估算单调递增",
        estimate_tokens("字" * 100) > estimate_tokens("字" * 10),
        str(estimate_tokens("字" * 100)),
    )

    usage_ctx = FakeContext(onebot)
    usage_ctx.provider = FakeProvider("这是一条生成的说说内容")
    usage_ai = AIClient(
        PluginConfig(StubAstrBotConfig(dict(raw_config)), usage_ctx), usage_ctx
    )
    calls_before = usage_ai.usage.summary(1)["calls"]
    await usage_ai.chat(system_prompt="你是助手", prompt="写点东西", feature="说说")
    usage_after = usage_ai.usage.summary(1)
    check(
        "调用后用量计数 +1",
        usage_after["calls"] == calls_before + 1,
        str(usage_after["calls"]),
    )
    check(
        "分别记录输入与输出",
        usage_after["prompt"] > 0 and usage_after["completion"] > 0,
        str(usage_after),
    )
    check(
        "按功能分组统计",
        "说说" in usage_after["by_feature"],
        str(list(usage_after["by_feature"])),
    )
    check(
        "last_call 带功能名与总量",
        usage_ai.last_call.get("feature") == "说说"
        and usage_ai.last_call.get("total", 0) > 0,
        str(usage_ai.last_call),
    )
    check(
        "last_call_text 可读",
        "tokens" in usage_ai.last_call_text(),
        usage_ai.last_call_text(),
    )
    check(
        "汇总文本含功能明细",
        "说说" in usage_ai.usage.format_summary(1),
        usage_ai.usage.format_summary(1),
    )

    async def fake_persona():
        return {"name": "测试人格", "prompt": "你是温柔的学生，喜欢摄影"}

    plugin.context.persona_manager = types.SimpleNamespace(
        get_default_persona_v3=fake_persona
    )
    ai_calls: list[tuple[str, str]] = []

    async def dispatching_ai(
        *, system_prompt, prompt=None, contexts=None, provider_id=None, feature=None
    ):
        ai_calls.append((str(feature), f"{system_prompt}\n{prompt or ''}"))
        if feature == "日程":
            return (
                '```json\n{"outfit": "米色针织衫", '
                '"schedule": "上午在市图书馆查资料，下午去江边拍照"}\n```'
            )
        return "今天在图书馆待到闭馆，很安静。"

    plugin.ai.chat = dispatching_ai
    plugin.life._states.clear()
    ai_calls.clear()
    plugin.cfg.set("llm_use_life_context", True)
    plugin.cfg.set("llm_life_must_reference", True)
    plugin.cfg.set("llm_use_persona", True)
    text = await plugin.content.rewrite()
    check("生成成功", text.startswith("今天在图书馆"), text)
    scheduled = [item for item in ai_calls if item[0] == "日程"]
    check("没有日程时先生成日程", bool(scheduled), str([item[0] for item in ai_calls]))
    check(
        "日程生成时带上人设并要求贴合",
        "温柔的学生" in scheduled[-1][1] and "自己确认" in scheduled[-1][1],
        scheduled[-1][1][:80],
    )
    generation_prompt = [item for item in ai_calls if item[0] == "说说"][-1][1]
    check(
        "说说提示词要求 AI 先自己确认人设与日程",
        "生成前请先自己确认" in generation_prompt,
        generation_prompt[:120],
    )
    check("提示词里带上了人设内容", "温柔的学生" in generation_prompt, "fail")
    check("提示词里带上了今日日程", "市图书馆" in generation_prompt, "fail")
    check(
        "要求引用行程具体细节",
        "引用要求" in generation_prompt and "具体细节" in generation_prompt,
        "fail",
    )
    check(
        "记录本次生成依据",
        plugin.content.last_generation.get("persona") == "测试人格"
        and plugin.content.last_generation.get("life") is True,
        str(plugin.content.last_generation),
    )

    plugin.cfg.set("llm_life_must_reference", False)
    ai_calls.clear()
    await plugin.content.rewrite()
    relaxed_prompt = [item for item in ai_calls if item[0] == "说说"][-1][1]
    check(
        "关闭强制引用后不再要求细节",
        "引用要求" not in relaxed_prompt and "温柔的学生" in relaxed_prompt,
        relaxed_prompt[:120],
    )
    plugin.cfg.set("llm_life_must_reference", True)

    async def failing_life_ai(
        *, system_prompt, prompt=None, contexts=None, provider_id=None, feature=None
    ):
        ai_calls.append((str(feature), f"{system_prompt}\n{prompt or ''}"))
        if feature == "日程":
            raise RuntimeError("AI 挂了")
        return "随便写一句。"

    plugin.ai.chat = failing_life_ai
    plugin.life._states.clear()
    ai_calls.clear()
    await plugin.content.rewrite()
    fallback_prompt = [item for item in ai_calls if item[0] == "说说"][-1][1]
    check(
        "日程拿不到时让 AI 自己先安排再写",
        "今天还没有安排" in fallback_prompt,
        fallback_prompt[:120],
    )
    check(
        "并在依据里标出未引用日程",
        plugin.content.last_generation.get("life") is False
        and bool(plugin.content.last_generation.get("warnings")),
        str(plugin.content.last_generation.get("warnings")),
    )

    out = await collect(plugin.cmd_status(FakeEvent()))
    status_text = out[0]
    check("状态里显示上次生成依据", "上次生成依据" in status_text, status_text[:200])
    check("状态里显示 Token 用量", "Token 用量" in status_text, status_text[-260:])

    # 让命令里有可读的用量数据（前面的用例把 ai.chat 换成了假函数，不会自动记账）
    plugin.ai.usage.record("说说", 120, 40)
    out = await collect(plugin.cmd_usage(FakeEvent(), "7"))
    check(
        "用量指令给出明细",
        "AI 用量估算" in out[0] and "说说" in out[0] and "误差" in out[0],
        out[0][:160],
    )
    out = await collect(plugin.cmd_usage(FakeEvent(), "abc"))
    check("用量指令校验参数", "用法" in out[0], out[0][:80])

    print("\n[25] 回执图渲染")
    ReceiptRenderer = _imp("core.render").ReceiptRenderer
    receipt_file = DATA_DIR / "receipt.png"
    receipt_file.write_bytes(b"\x89PNG\r\n\x1a\nfake-receipt")

    cfg.set("notify_render_image", False)
    cfg.set("notify_render_network", False)
    receipt = ReceiptRenderer(plugin.cfg)
    check(
        "默认关闭",
        receipt.enabled is False and receipt.status_text() == "关闭",
        receipt.status_text(),
    )
    check(
        "关闭时不渲染",
        await receipt.render("这是一段足够长的回执文本，" * 5) is None,
        "fail",
    )

    FAKE_RENDERER.calls.clear()
    FAKE_RENDERER.error = None
    FAKE_RENDERER.result = str(receipt_file)
    cfg.set("notify_render_image", True)
    check(
        "开启后状态标为本地渲染",
        "本地渲染" in receipt.status_text(),
        receipt.status_text(),
    )

    check("文本过短不渲染", await receipt.render("太短") is None, "fail")
    check(
        "文本过短时连渲染器都不调用",
        not FAKE_RENDERER.calls,
        str(len(FAKE_RENDERER.calls)),
    )

    long_text = "这是一段足够长的回执文本，" * 5
    result = await receipt.render(long_text)
    check("渲染成功返回图片路径", result == str(receipt_file), str(result))
    check(
        "默认走本地渲染",
        FAKE_RENDERER.calls[-1]["use_network"] is False
        and FAKE_RENDERER.calls[-1]["return_url"] is False,
        str(FAKE_RENDERER.calls[-1]),
    )

    cfg.set("notify_render_network", True)
    check(
        "开启网络渲染后状态变化", "网络" in receipt.status_text(), receipt.status_text()
    )
    await receipt.render(long_text)
    check(
        "网络开关透传给渲染器",
        FAKE_RENDERER.calls[-1]["use_network"] is True,
        str(FAKE_RENDERER.calls[-1]),
    )
    cfg.set("notify_render_network", False)

    FAKE_RENDERER.result = "https://example.com/receipt.png"
    check(
        "返回 URL 时原样带回",
        (await receipt.render(long_text)) == "https://example.com/receipt.png",
        "fail",
    )

    FAKE_RENDERER.error = RuntimeError("渲染服务挂了")
    check("渲染抛异常时降级", await receipt.render(long_text) is None, "fail")
    FAKE_RENDERER.error = None
    FAKE_RENDERER.result = ""
    check("渲染返回空时降级", await receipt.render(long_text) is None, "fail")
    FAKE_RENDERER.result = "not-a-real-file"
    check(
        "渲染结果不是文件也不是 URL 时降级",
        await receipt.render(long_text) is None,
        "fail",
    )
    FAKE_RENDERER.result = str(receipt_file)

    StarTools.sent.clear()
    await plugin._notify(long_text)
    chains = [item[1].chain for item in StarTools.sent]
    check(
        "开启后通知消息链里带图",
        bool(chains) and all(len(chain) == 2 for chain in chains),
        str([len(chain) for chain in chains]),
    )
    check(
        "图片段用的是本地文件",
        any(
            getattr(component, "path", None) == str(receipt_file)
            for chain in chains
            for component in chain
        ),
        str(chains)[:120],
    )

    plugin.drafts.clear()
    StarTools.sent.clear()
    plugin.drafts.put(Draft(kind="post", text="草稿内容" * 10))
    await plugin._send_draft(plugin.drafts.pending)
    draft_chains = [item[1].chain for item in StarTools.sent]
    check(
        "草稿确认也带图",
        bool(draft_chains) and all(len(chain) == 2 for chain in draft_chains),
        str([len(chain) for chain in draft_chains]),
    )
    plugin.drafts.clear()

    cfg.set("notify_render_image", False)
    StarTools.sent.clear()
    await plugin._notify(long_text)
    plain_chains = [item[1].chain for item in StarTools.sent]
    check(
        "关闭后只发纯文本",
        bool(plain_chains) and all(len(chain) == 1 for chain in plain_chains),
        str([len(chain) for chain in plain_chains]),
    )

    out = await collect(plugin.cmd_status(FakeEvent()))
    check(
        "状态里显示回执图设置", any("回执图渲染" in item for item in out), str(out)[:80]
    )

    print("\n[26] 审核限制选项")
    plugin.cfg.set("draft_for_greet", True)
    plugin.cfg.set("draft_timeout_minutes", 0)
    plugin.cfg.set("admin_uins", ["20001"])  # 管理员与问候对象分开，便于区分发给谁
    plugin.cfg.set("greet_users", ["10001"])
    plugin.cfg.set("greet_use_ai", False)
    plugin.cfg.set("greet_morning_pool", ["早上好呀"])
    plugin.drafts.clear()
    plugin.greet._sent.clear()
    StarTools.sent.clear()
    await plugin._run_greet("morning")
    pending_greet = plugin.drafts.pending
    check(
        "开启后问候转为草稿",
        pending_greet is not None
        and pending_greet.kind == "greet"
        and pending_greet.targets == ["10001"],
        str(pending_greet),
    )
    check(
        "问候草稿写清发送对象",
        "发送对象" in pending_greet.describe() and "10001" in pending_greet.describe(),
        pending_greet.describe()[:120],
    )
    recipients = [item[0] for item in StarTools.sent]
    check(
        "草稿发给了管理员而不是问候对象",
        "aiocqhttp:FriendMessage:20001" in recipients
        and "aiocqhttp:FriendMessage:10001" not in recipients,
        str(recipients),
    )
    check(
        "状态里提示有待确认草稿",
        any("待确认" in item for item in await collect(plugin.cmd_status(FakeEvent()))),
        "fail",
    )

    StarTools.sent.clear()
    out = await collect(plugin.cmd_confirm(FakeEvent()))
    check(
        "确认问候草稿后才真正私聊发出",
        "aiocqhttp:FriendMessage:10001" in [item[0] for item in StarTools.sent],
        str([item[0] for item in StarTools.sent]),
    )
    check("确认后有结果反馈", any("问候已发送" in item for item in out), str(out)[:120])
    plugin.cfg.set("admin_uins", [])

    plugin.cfg.set("draft_for_greet", False)
    plugin.cfg.set("draft_timeout_minutes", 30)
    plugin.cfg.set("draft_enabled", True)
    plugin.cfg.set("content_source", "pool")
    plugin.cfg.set("text_pool", ["超时放行的说说"])
    plugin.api.EMOTION_URL = f"{AI_BASE}/publish"
    plugin.drafts.clear()
    StarTools.sent.clear()
    await plugin._dispatch_post("超时测试内容", source="pool", prefix="定时发布")
    timed_draft = plugin.drafts.pending
    check(
        "草稿超时计时已挂上", plugin._draft_timer is not None, str(plugin._draft_timer)
    )

    history_before = len(plugin.store._records)
    await plugin._draft_timeout_watch(timed_draft, 0)
    check(
        "超时无人处理则自动发布",
        len(plugin.store._records) == history_before + 1
        and plugin.drafts.pending is None,
        str(len(plugin.store._records)),
    )
    check(
        "超时执行后有通知",
        any(
            "自动执行" in item[1].chain[0].text
            for item in StarTools.sent
            if item[1].chain
        ),
        str(StarTools.sent)[:140],
    )

    await plugin._dispatch_post("会被人工放弃的内容", source="pool", prefix="定时发布")
    check("新草稿重新挂上计时", plugin._draft_timer is not None, "fail")
    out = await collect(plugin.cmd_drop(FakeEvent()))
    check(
        "人工放弃后计时被取消",
        plugin._draft_timer is None and any("已丢弃" in item for item in out),
        str(plugin._draft_timer),
    )

    plugin.cfg.set("draft_timeout_minutes", 0)
    plugin.cfg.set("draft_enabled", False)
    plugin._cancel_draft_timer()

    print("\n[27] 手动触发 AI 自动发说说（/空间自动发）")

    async def auto_chat(
        *, system_prompt, prompt=None, contexts=None, provider_id=None, feature=None
    ):
        return "手动自动发的说说正文"

    plugin.ai.chat = auto_chat
    plugin.cfg.set("content_source", "llm")
    plugin.cfg.set("llm_use_life_context", False)
    plugin.cfg.set("draft_enabled", False)
    plugin.drafts.clear()
    received.pop("publish", None)

    out = await collect(plugin.cmd_auto_publish(FakeEvent()))
    check(
        "手动自动发：直接发布并回报 tid",
        any("手动自动发成功" in item for item in out)
        and any("tid:" in item for item in out),
        str(out)[:140],
    )
    check(
        "手动自动发：发出去的就是 AI 生成结果",
        str(received.get("publish", {}).get("form", {}).get("con"))
        == "手动自动发的说说正文",
        str(received.get("publish", {}).get("form", {}).get("con"))[:60],
    )

    plugin.cfg.set("draft_enabled", True)
    plugin.drafts.clear()
    received.pop("publish", None)
    out = await collect(plugin.cmd_auto_publish(FakeEvent()))
    pending_post = plugin.drafts.pending
    check(
        "手动自动发：开着草稿确认时转成草稿",
        pending_post is not None
        and pending_post.kind == "post"
        and any("草稿" in item for item in out),
        f"{str(out)[:80]} | {pending_post}",
    )
    check(
        "草稿正文来自 AI 生成",
        pending_post is not None and "手动自动发的说说正文" in pending_post.text,
        str(pending_post)[:80],
    )
    check(
        "草稿模式下没有真的发出去",
        "publish" not in received,
        str(sorted(received)),
    )

    plugin.cfg.set("draft_enabled", False)
    plugin.drafts.clear()

    print("\n[28] 问候发送结果校验（「日志说成功但没收到」的两个成因）")

    async def failing_sender(umo: str, text: str) -> bool:
        return False

    verify = GreetingService(
        greet_cfg_obj, greet_ai, lambda: "aiocqhttp", sender=failing_sender
    )
    verify._sent = {}
    res_fail = await verify.send("morning", force=True, record=False)
    check(
        "平台返回 False 时不算成功",
        res_fail.sent == 0 and any("没有发出" in item for item in res_fail.errors),
        res_fail.summary(),
    )
    check(
        "发送失败不写今日记录（下次还会重试）",
        verify._already_sent("morning", "10001") is False,
        str(verify._sent),
    )

    no_platform = GreetingService(
        greet_cfg_obj, greet_ai, lambda: "", sender=failing_sender
    )
    no_platform._sent = {}
    res_np = await no_platform.send("morning", force=True, record=False)
    check(
        "找不到平台实例时明确报错且不发送",
        res_np.sent == 0 and any("平台实例" in item for item in res_np.errors),
        res_np.summary(),
    )

    manual = GreetingService(
        greet_cfg_obj, greet_ai, lambda: "aiocqhttp", sender=fake_sender
    )
    manual._sent = {}
    sent_messages.clear()
    two = ["10001", "10002"]
    res_manual = await manual.send("morning", targets=two, force=True, record=False)
    check(
        "手动发送标记为不占用今日名额",
        res_manual.sent == 2 and "不占用" in res_manual.summary(),
        res_manual.summary(),
    )
    res_auto = await manual.send("morning", targets=two)
    check(
        "手动发送后定时任务仍会真的发出去",
        res_auto.sent == 2 and res_auto.skipped == 0,
        res_auto.summary(),
    )

    resolved = GreetingService(
        greet_cfg_obj,
        greet_ai,
        lambda: "aiocqhttp",
        umo_resolver=lambda qq: f"睦:FriendMessage:{qq}" if qq == "10001" else "",
        sender=fake_sender,
    )
    check(
        "优先用记住的真实会话地址，缺失时回退按平台拼",
        resolved.umo_for("10001") == "睦:FriendMessage:10001"
        and resolved.umo_for("10002") == "aiocqhttp:FriendMessage:10002",
        f"{resolved.umo_for('10001')} / {resolved.umo_for('10002')}",
    )
    resolved._sent = {}
    sent_messages.clear()
    res_addr = await resolved.send("morning", targets=two, force=True)
    check(
        "汇总里带出实际发送地址",
        res_addr.targets_used.get("10001") == "睦:FriendMessage:10001"
        and len(sent_messages) == 2,
        str(res_addr.targets_used),
    )

    plugin.greet._sent = {}
    StarTools.sent.clear()
    out = await collect(plugin.cmd_greet(FakeEvent(), "morning 1611729294"))
    check(
        "手动指令不占用今日自动问候名额，并回报发送地址",
        plugin.greet._already_sent("morning", "1611729294") is False
        and any("发送地址" in item for item in out),
        str(out)[:160],
    )
    check(
        "手动指令用真实私聊会话地址",
        "aiocqhttp:FriendMessage:1611729294" in str(out),
        str(out)[:160],
    )
    plugin.greet._sent = {}

    print("\n[29] 面板板块结构与旧配置自动迁移")

    schema_now = json.loads(
        (REPO_ROOT / "_conf_schema.json").read_text(encoding="utf-8")
    )
    visible_sections = {
        key: meta
        for key, meta in schema_now.items()
        if isinstance(meta, dict)
        and meta.get("type") == "object"
        and not meta.get("condition")
    }
    titles = [str(meta.get("description")) for meta in visible_sections.values()]
    check(
        "面板分成 7 个板块且顺序固定",
        titles
        == [
            "基础设置",
            "私聊问候",
            "空间说说",
            "说说互动",
            "生活日程",
            "草稿确认",
            "网络与登录",
        ],
        str(titles),
    )
    check(
        "每个板块都带标题与说明",
        all(
            str(meta.get("description") or "").strip()
            and str(meta.get("hint") or "").strip()
            for meta in visible_sections.values()
        ),
        str(titles),
    )
    order_ok = True
    for meta in visible_sections.values():
        keys = list((meta.get("items") or {}).keys())
        providers = [i for i, k in enumerate(keys) if k.endswith("_provider_id")]
        prompts = [i for i, k in enumerate(keys) if k.endswith("_prompt")]
        if providers and prompts and min(providers) > min(prompts):
            order_ok = False
    check("板块内「模型提供商」统一排在提示词之前", order_ok, "板块内顺序")

    hidden = [
        key
        for key, meta in schema_now.items()
        if isinstance(meta, dict) and meta.get("condition")
    ]
    check(
        "旧扁平键以隐藏项保留（迁移用，不出现在面板）",
        len(hidden) == 65 and "_flat_keys_migrated" in hidden,
        f"隐藏项 {len(hidden)} 个",
    )

    legacy_raw = StubAstrBotConfig(
        {"greet_users": ["10001"], "publish_cron": "0 6 * * *", "content_source": "llm"}
    )
    legacy_cfg = PluginConfig(legacy_raw, FakeContext(onebot))
    check(
        "旧扁平配置被搬到对应板块",
        legacy_raw["sec_private"]["greet_users"] == ["10001"]
        and legacy_raw["sec_post"]["publish_cron"] == "0 6 * * *"
        and legacy_raw["sec_post"]["content_source"] == "llm",
        str(legacy_raw["sec_private"]),
    )
    check(
        "迁移后旧键删除并打上标记",
        "greet_users" not in legacy_raw and legacy_raw["_flat_keys_migrated"] is True,
        str(sorted(legacy_raw.keys())[:4]),
    )
    check(
        "迁移后的值照常读得到",
        legacy_cfg.greet_users == ["10001"] and legacy_cfg.publish_cron == "0 6 * * *",
        str(legacy_cfg.greet_users),
    )
    legacy_raw["sec_post"]["publish_cron"] = "0 7 * * *"
    again = PluginConfig(legacy_raw, FakeContext(onebot))
    check(
        "再次启动不会用旧值覆盖面板里改过的新值",
        again.publish_cron == "0 7 * * *",
        str(again.publish_cron),
    )

    print("\n[30] 回复自己说说下的评论")

    plugin.api.LIST_URL = f"{AI_BASE}/feeds"
    plugin.api.REPLY_URL = f"{AI_BASE}/reply"
    plugin.api.DETAIL_URL = f"{AI_BASE}/detail"

    async def reply_chat(
        *, system_prompt, prompt=None, contexts=None, provider_id=None, feature=None
    ):
        return "收到了，我这边也一样。"

    plugin.ai.chat = reply_chat
    cfg.set("interact_reply_enabled", True)
    cfg.set("interact_reply_max_per_run", 3)
    cfg.set("interact_reply_max_chars", 80)
    cfg.set("draft_for_reply", False)
    cfg.set("draft_enabled", False)
    cfg.set("interact_days", 3)
    plugin.drafts.clear()
    plugin.interact._replied = []

    parsed_post = FeedPost.from_raw(
        {
            "uin": SELF_UIN,
            "tid": "P1",
            "content": "正文",
            "cmtnum": 3,
            "commentlist": [
                {
                    "uin": 999999,
                    "name": "小明",
                    "tid": "C1",
                    "content": "不错[em]e100[/em]",
                    "createTime": 1700000000,
                },
                {"uin": 888888, "tid": "C2", "content": "   "},
                {"uin": 777777, "tid": "", "content": "缺评论 ID"},
                "不是字典",
            ],
        }
    )
    check(
        "评论明细解析（剥离表情、容忍字段缺失与脏数据）",
        len(parsed_post.comments) == 2
        and parsed_post.comments[0].nickname == "小明"
        and parsed_post.comments[0].content == "不错"
        and parsed_post.comments[0].create_time == 1700000000
        and parsed_post.comment_count == 3,
        str([(item.tid, item.content) for item in parsed_post.comments]),
    )
    check(
        "详情响应的评论解析同源",
        [
            item.tid
            for item in QzoneParser.parse_comments(
                {"commentlist": [{"uin": 1, "tid": "X", "content": "hi"}]}
            )
        ]
        == ["X"],
        "parse_comments",
    )

    plugin.interact._replied = []
    replies.clear()
    feeds_payload[:] = [
        my_post(
            "S1",
            1,
            [
                comment_item("C_SELF", "我自己顶一下", uin=SELF_UIN, name="我自己"),
                comment_item("C_EMPTY", "   "),
            ],
        )
    ]
    res = await plugin.interact.run_replies_once()
    check(
        "自己的评论与空内容评论都跳过",
        res.checked == 2 and res.replied == 0 and res.skipped == 2 and not replies,
        res.summary(),
    )

    plugin.interact._replied = []
    replies.clear()
    feeds_payload[:] = [
        my_post("S_OLD", 24 * 5, [comment_item("C_OLD", "老说说上的评论")])
    ]
    res = await plugin.interact.run_replies_once()
    check(
        "自己 5 天前的说说超出窗口，不回复",
        res.checked == 0 and res.replied == 0 and not replies,
        res.summary(),
    )

    plugin.interact._replied = []
    replies.clear()
    feeds_payload[:] = [my_post("S2", 1, [comment_item("C2", "你今天去哪了")])]
    res = await plugin.interact.run_replies_once()
    check(
        "直接模式：回复一条评论",
        res.checked == 1 and res.replied == 1 and len(replies) == 1,
        res.summary(),
    )
    reply_form = replies[-1]["form"] if replies else {}
    check(
        "回复表单参数正确",
        reply_form.get("topicId") == f"{SELF_UIN}_S2__1"
        and reply_form.get("commentId") == "C2"
        and reply_form.get("commentUin") == "999999"
        and reply_form.get("hostUin") == str(SELF_UIN)
        and reply_form.get("content") == "收到了，我这边也一样。"
        and reply_form.get("feedsType") == "100"
        and reply_form.get("qzreferrer", "").endswith(f"/{SELF_UIN}/main")
        and replies[-1]["query"].get("g_tk"),
        str(reply_form)[:180],
    )

    res = await plugin.interact.run_replies_once()
    check(
        "同一条评论第二轮不再回复",
        res.replied == 0 and res.skipped == 1 and len(replies) == 1,
        res.summary(),
    )
    check(
        "回复去重记录落盘",
        plugin.interact.replied("S2", "C2")
        and plugin.interact.replied_count == 1
        and (plugin.cfg.data_dir / "replied_comments.json").exists(),
        str(plugin.interact.replied_count),
    )

    plugin.interact._replied = []
    replies.clear()
    feeds_payload[:] = [
        my_post("S3", 1, [comment_item("C3a", "第一条"), comment_item("C3b", "第二条")])
    ]
    res = await plugin.interact.run_replies_once()
    check(
        "同一条说说每轮最多回一条",
        res.replied == 1
        and len(replies) == 1
        and replies[-1]["form"].get("commentId") == "C3a",
        res.summary(),
    )

    plugin.interact._replied = []
    replies.clear()
    cfg.set("interact_reply_max_per_run", 1)
    feeds_payload[:] = [
        my_post("S4", 1, [comment_item("C4", "说说四的评论")]),
        my_post("S5", 2, [comment_item("C5", "说说五的评论")]),
    ]
    res = await plugin.interact.run_replies_once()
    check(
        "每轮上限生效（最多 1 条）",
        res.replied == 1 and len(replies) == 1,
        res.summary(),
    )
    cfg.set("interact_reply_max_per_run", 3)
    plugin.interact._replied = []
    replies.clear()
    plugin.drafts.clear()
    cfg.set("draft_for_reply", True)
    feeds_payload[:] = [
        my_post(
            "S6", 1, [comment_item("C6", "草稿模式的评论", uin=888888, name="小红")]
        )
    ]
    res = await plugin.interact.run_replies_once()
    pending_reply = plugin.drafts.pending
    check(
        "草稿模式：不直接发出，转成 reply 草稿",
        res.drafted == 1
        and res.replied == 0
        and not replies
        and pending_reply is not None
        and pending_reply.kind == "reply",
        res.summary(),
    )
    check(
        "回复草稿带目标说说 / 评论 / 评论人",
        pending_reply is not None
        and pending_reply.target_tid == "S6"
        and pending_reply.target_comment_tid == "C6"
        and pending_reply.target_comment_uin == 888888
        and pending_reply.target_name == "小红"
        and "草稿模式的评论" in pending_reply.target_text,
        str(pending_reply)[:140],
    )
    check(
        "回复草稿标题与描述写清回复谁",
        pending_reply is not None
        and "回复草稿" in pending_reply.title()
        and "小红" in pending_reply.title()
        and "被回复的评论" in pending_reply.describe(),
        pending_reply.title() if pending_reply else "None",
    )

    res = await plugin.interact.run_replies_once()
    check(
        "已有待确认回复草稿时不再重复生成",
        res.drafted == 0 and res.skipped == 1 and not replies,
        res.summary(),
    )

    replies.clear()
    out = await collect(plugin.cmd_confirm(FakeEvent()))
    check(
        "确认回复草稿后真的调用回复接口",
        len(replies) == 1 and replies[-1]["form"].get("commentId") == "C6",
        str(out)[:140],
    )
    check(
        "确认后才记入去重，草稿清空",
        plugin.interact.replied("S6", "C6") and plugin.drafts.pending is None,
        str(plugin.interact.replied_count),
    )

    plugin.interact._replied = []
    replies.clear()
    details.clear()
    cfg.set("draft_for_reply", False)
    detail_comments[:] = [
        comment_item("C7", "详情接口里的评论", uin=777777, name="小刚")
    ]
    feeds_payload[:] = [my_post("S7", 1, [], cmtnum=1)]
    res = await plugin.interact.run_replies_once()
    check(
        "列表没带评论明细时回退到详情接口",
        len(details) == 1 and details[-1].get("tid") == "S7" and res.replied == 1,
        f"{details} / {res.summary()}",
    )
    detail_comments.clear()

    async def empty_reply_chat(
        *, system_prompt, prompt=None, contexts=None, provider_id=None, feature=None
    ):
        return "   "

    plugin.ai.chat = empty_reply_chat
    plugin.interact._replied = []
    replies.clear()
    feeds_payload[:] = [my_post("S8", 1, [comment_item("C8", "空回复场景")])]
    res = await plugin.interact.run_replies_once()
    check(
        "AI 返回空内容时记为错误且不发送",
        res.replied == 0 and bool(res.errors) and not replies,
        res.summary(),
    )
    plugin.ai.chat = reply_chat

    out = await collect(plugin.cmd_reply(FakeEvent(), ""))
    check(
        "/空间回复 无参数显示状态",
        any("回复评论当前为" in item for item in out)
        and any("每轮最多" in item for item in out),
        str(out)[:140],
    )
    out = await collect(plugin.cmd_reply(FakeEvent(), "off"))
    check(
        "/空间回复 off 关闭开关",
        any("已关闭" in item for item in out)
        and not bool(plugin.cfg.interact_reply_enabled),
        str(out)[:100],
    )
    out = await collect(plugin.cmd_reply(FakeEvent(), "on"))
    check(
        "/空间回复 on 打开开关",
        any("已开启" in item for item in out)
        and bool(plugin.cfg.interact_reply_enabled),
        str(out)[:100],
    )
    plugin.interact._replied = []
    replies.clear()
    feeds_payload[:] = [my_post("S9", 1, [comment_item("C9", "指令触发一轮")])]
    out = await collect(plugin.cmd_reply(FakeEvent(), "now"))
    check(
        "/空间回复 now 立刻跑一轮",
        any("评论回复完成" in item for item in out) and len(replies) == 1,
        str(out)[:140],
    )
    out = await collect(plugin.cmd_reply(FakeEvent(), "乱写"))
    check(
        "/空间回复 参数无效时给用法",
        any("参数无效" in item for item in out),
        str(out)[:80],
    )

    check(
        "回复模式描述随开关变化",
        plugin.interact.reply_mode_text().startswith("开启")
        and "已回复" in plugin.interact.reply_mode_text(),
        plugin.interact.reply_mode_text(),
    )
    check(
        "互动模式描述带出回复项",
        "回复" in plugin.interact.mode_text(),
        plugin.interact.mode_text(),
    )
    out = await collect(plugin.cmd_status(FakeEvent()))
    check(
        "状态输出包含评论回复一行",
        any("评论回复" in item for item in out),
        str(out)[:120],
    )

    cfg.set("interact_reply_enabled", False)
    check(
        "关闭后回复模式描述为关闭",
        plugin.interact.reply_mode_text() == "关闭",
        plugin.interact.reply_mode_text(),
    )
    check(
        "关闭时互动模式描述不再带回复项",
        "回复" not in plugin.interact.mode_text(),
        plugin.interact.mode_text(),
    )

    plugin.drafts.clear()
    plugin.interact._replied = []

    plugin.publish_task.stop()
    plugin.interact_task.stop()
    await plugin.api.close()
    await runner2.cleanup()

    print("\n" + "=" * 60)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for item in FAILED:
            print(f"  - {item}")
        return 1
    print("全部自测通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
