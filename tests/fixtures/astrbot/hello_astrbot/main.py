"""bot_bridge 的 AstrBot 兼容层测试插件（SDK-v2 风格：装饰器 + Star 子类）。

覆盖的 AstrBot 插件 API：指令 / 带参指令 / 指令组 / GreedyStr / 正则过滤器 /
消息类型与平台过滤器 / 权限过滤器 / 自定义过滤器 / LLM 工具 / 生命周期钩子 /
配置 / 主动发送 / 模型调用 / 键值存储。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Face, Plain
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr


class NoNekoFilter(filter.CustomFilter):
    """示例自定义过滤器：消息里出现 ``neko`` 就不处理。"""

    def filter(self, event: AstrMessageEvent, cfg: object) -> bool:
        del cfg
        return "neko" not in event.get_message_str()


class HelloAstrBot(Star):
    """兼容层测试用插件。"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.lifecycle: list[str] = []
        self.handled_events: list[str] = []
        self.loaded_hooks = 0
        self.stopped_handlers: list[str] = []

    # -- 指令 -------------------------------------------------------------- #

    @filter.command("hello")
    async def hello(self, event: AstrMessageEvent, name: str = "world"):
        """打招呼。"""
        logger.info("hello 指令被触发")
        greeting = self.context.get_config().get("greeting") or "Hello"
        yield event.plain_result(f"{greeting}, {name}!")

    @filter.command("echo", alias={"echoes"})
    async def echo(self, event: AstrMessageEvent, message: GreedyStr):
        """回显所有剩余文本。"""
        yield event.plain_result(f"你发了: {message}")

    @filter.command_group("math")
    def math(self) -> None:
        """数学指令组。"""

    @math.command("add")
    async def add(self, event: AstrMessageEvent, a: int, b: int):
        """两数相加。"""
        yield event.plain_result(f"结果是: {a + b}")

    @filter.regex(r"^ping$")
    async def ping(self, event: AstrMessageEvent):
        """正则指令：ping。"""
        yield event.plain_result("pong")

    @filter.command("where")
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def where(self, event: AstrMessageEvent):
        """输出当前平台名。"""
        yield event.plain_result(f"platform={event.get_platform_name()}")

    @filter.command("admin_only")
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def admin_only(self, event: AstrMessageEvent):
        """仅管理员可用。"""
        yield event.plain_result("admin ok")

    @filter.command("blocked")
    @filter.custom_filter(NoNekoFilter)
    async def blocked(self, event: AstrMessageEvent):
        """被自定义过滤器保护的指令。"""
        yield event.plain_result("blocked ok")

    @filter.command("quit")
    async def quit(self, event: AstrMessageEvent):
        """回复后终止事件传播。"""
        self.stopped_handlers.append("quit")
        yield event.plain_result("bye")
        event.stop_event()

    @filter.command("emoji")
    async def emoji(self, event: AstrMessageEvent):
        """返回含非文本消息段的链（表情 + 文本）。"""
        yield event.chain_result([Face(id=21), Plain("你好")])

    # -- 主动消息 / 模型 / 存储 -------------------------------------------- #

    @filter.command("notify")
    async def notify(self, event: AstrMessageEvent):
        """通过 context.send_message 主动发一条消息。"""
        sent = await self.context.send_message(
            event.unified_msg_origin,
            MessageChain().message("主动消息"),
        )
        yield event.plain_result(f"sent={sent}")

    @filter.command("push")
    async def push(self, event: AstrMessageEvent):
        """通过 event.send 立即发一条消息。"""
        await event.send(MessageChain().message("via event.send"))
        yield event.plain_result("pushed")

    @filter.command("ask")
    async def ask(self, event: AstrMessageEvent, question: GreedyStr):
        """调用当前对话模型。"""
        provider = await self.context.get_using_provider_async(umo=event.unified_msg_origin)
        response = await provider.text_chat(
            prompt=str(question),
            system_prompt="You are terse.",
        )
        yield event.plain_result(str(response.completion_text))

    @filter.command("kv")
    async def kv(self, event: AstrMessageEvent, value: int):
        """写入并读回键值存储。"""
        await self.put_kv_data("visits", value)
        stored = await self.get_kv_data("visits", 0)
        yield event.plain_result(f"kv={stored}")

    @filter.command("unsupported")
    async def unsupported(self, event: AstrMessageEvent):
        """调用一项 N.E.K.O. 没有的能力（应当结构化失败）。"""
        self.context.get_db()
        yield event.plain_result("never")

    # -- 监听器 / 钩子 ------------------------------------------------------ #

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.platform_adapter_type(filter.PlatformAdapterType.ALL)
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        """群聊消息监听器（不会回复，只记录）。"""
        self.handled_events.append(f"group:{event.get_group_id()}")

    @filter.on_astrbot_loaded()
    async def on_loaded(self) -> None:
        """加载完成钩子。"""
        self.loaded_hooks += 1
        logger.info("hello_astrbot on_astrbot_loaded")

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req) -> None:
        """LLM 请求前修改系统提示词。"""
        req.system_prompt = f"{req.system_prompt or ''}[hello-hook]"

    # -- LLM 工具 ---------------------------------------------------------- #

    @filter.llm_tool(name="get_weather")
    async def get_weather(self, event: AstrMessageEvent, location: str) -> str:
        """获取天气信息。

        Args:
            location(string): 地点
        """
        return f"{location} 晴 25℃"

    # -- 生命周期 ---------------------------------------------------------- #

    async def initialize(self) -> None:
        """插件激活。"""
        self.lifecycle.append("initialize")
        await self.put_kv_data("boots", 1)

    async def terminate(self) -> None:
        """插件停用。"""
        self.lifecycle.append("terminate")
