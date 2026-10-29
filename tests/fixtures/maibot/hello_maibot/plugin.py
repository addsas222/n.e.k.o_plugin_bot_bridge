"""MaiBot 兼容层测试用的 SDK v2 插件。

覆盖：命令（正则 + 命名捕获组）、工具、API、事件处理器、配置模型、
``on_load`` / ``on_unload`` 生命周期，以及 ``ctx.send.*`` / ``ctx.llm.*`` /
``ctx.db.*`` / ``ctx.emoji.*`` 能力调用。
"""

from __future__ import annotations

from typing import Any

from maibot_sdk import API, Command, EventHandler, Field, MaiBotPlugin, PluginConfigBase, Tool
from maibot_sdk.types import EventType, ToolParameterInfo, ToolParamType


class PluginSection(PluginConfigBase):
    """插件基础配置。"""

    config_version: str = Field(default="1.0.0", description="配置版本")
    enabled: bool = Field(default=True, description="是否启用插件")


class GreetingSection(PluginConfigBase):
    """问候配置。"""

    message: str = Field(default="你好！", description="默认问候语")


class HelloMaibotConfig(PluginConfigBase):
    """测试插件配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    greeting: GreetingSection = Field(default_factory=GreetingSection)


class HelloMaibotPlugin(MaiBotPlugin):
    """一个功能完整的测试插件。"""

    config_model = HelloMaibotConfig

    def __init__(self) -> None:
        super().__init__()
        self.seen_messages: list[dict[str, Any]] = []
        self.llm_calls: list[dict[str, Any]] = []
        self.loaded = False
        self.unloaded = False

    async def on_load(self) -> None:
        """生命周期：加载。"""
        self.ctx.logger.info("hello_maibot on_load：%s", self.ctx.plugin_id)
        self.loaded = True

    async def on_unload(self) -> None:
        """生命周期：卸载。"""
        self.ctx.logger.info("hello_maibot on_unload")
        self.unloaded = True

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        """生命周期：配置热更新（SDK 契约要求实现）。"""
        self.ctx.logger.info("hello_maibot on_config_update scope=%s version=%s", scope, version)
        if scope == "self":
            self.set_plugin_config(config_data)

    # ------------------------------------------------------------ 命令

    @Command("ping", description="检查插件是否在线", pattern=r"^/ping$")
    async def handle_ping(self, stream_id: str = "", **kwargs: Any):
        """回复 pong，并记录发送结果。"""
        del kwargs

        details = await self.ctx.send.text("pong from hello_maibot", stream_id, return_details=True)
        return True, f"pong（发送结果：{details}）", 1

    @Command("echo", description="回显一段文本", pattern=r"^/echo\s+(?P<text>.+)$")
    async def handle_echo(
        self,
        text: str = "",
        matched_groups: dict[str, str] | None = None,
        stream_id: str = "",
        **kwargs: Any,
    ):
        """使用命名捕获组回显，并顺带调用一次大模型能力。"""
        del kwargs

        payload = str((matched_groups or {}).get("text") or text or "")
        llm_result = await self.ctx.llm.generate(f"复述这句话：{payload}")
        self.llm_calls.append(dict(llm_result) if isinstance(llm_result, dict) else {"response": str(llm_result)})
        await self.ctx.send.text(f"echo：{payload}", stream_id)
        return True, f"echo：{payload}", 1

    @Command("status", description="返回结构化状态", pattern=r"^/status$")
    async def handle_status(self, stream_id: str = "", **kwargs: Any):
        """返回 dict 形状的结果，用来验证桥接层不会丢掉处理函数的返回值。"""
        del kwargs

        delivered = await self.ctx.send.text("状态：在线", stream_id)
        return {"content": "状态：在线", "plugin": "hello_maibot", "delivered": bool(delivered), "seen": len(self.seen_messages)}

    @Command("config_probe", description="读取强类型配置", pattern=r"^/config_probe$")
    async def handle_config_probe(self, stream_id: str = "", **kwargs: Any):
        """把配置里的问候语读出来。"""
        del kwargs

        message = self.config.greeting.message
        await self.ctx.send.text(f"greeting={message}", stream_id)
        return True, f"greeting={message}", 1

    @Command("db_probe", description="写入并统计插件自己的数据库记录", pattern=r"^/db_probe\s+(?P<value>\S+)$")
    async def handle_db_probe(self, matched_groups: dict[str, str] | None = None, **kwargs: Any):
        """用 database.* 能力写一条记录再计数。"""
        del kwargs

        value = str((matched_groups or {}).get("value") or "")
        await self.ctx.db.save("notes", {"value": value})
        count = await self.ctx.db.count("notes")
        return True, f"db_count={count}", 1

    @Command("emoji_probe", description="探测没有等价实现的能力", pattern=r"^/emoji_probe$")
    async def handle_emoji_probe(self, **kwargs: Any):
        """调用 ``emoji.get_count``（N.E.K.O. 侧不支持），把原始返回值带回去。"""
        del kwargs

        result = await self.ctx.emoji.get_count()
        return True, f"emoji_probe={result}", 1

    # ------------------------------------------------------------ 工具

    @Tool(
        "maibot_add",
        description="把两个数字相加",
        parameters=[
            ToolParameterInfo(name="left", param_type=ToolParamType.FLOAT, description="左操作数", required=True),
            ToolParameterInfo(name="right", param_type=ToolParamType.FLOAT, description="右操作数", required=True),
        ],
    )
    async def handle_add(self, left: float = 0.0, right: float = 0.0, **kwargs: Any):
        """返回两数之和。"""
        del kwargs

        return {"name": "maibot_add", "content": str(left + right)}

    @Tool("maibot_fail", description="总是抛出异常，用于验证错误透传")
    async def handle_fail(self, **kwargs: Any):
        """故意失败。"""
        del kwargs

        raise RuntimeError("maibot_fail 按约定失败")

    # ------------------------------------------------------------ API

    @API("hello_status", description="返回插件内部状态", version="1", public=True)
    async def api_hello_status(self, **kwargs: Any):
        """供其它插件互调的 API。"""
        return {
            "plugin": "hello_maibot",
            "loaded": self.loaded,
            "seen": len(self.seen_messages),
            "arguments": dict(kwargs),
        }

    # ------------------------------------------------------------ 事件

    @EventHandler("hello_message_watcher", description="记录入站消息", event_type=EventType.ON_MESSAGE)
    async def handle_message_watch(self, message: Any = None, **kwargs: Any):
        """把收到的消息记到插件状态里。"""
        del kwargs

        if isinstance(message, dict):
            self.seen_messages.append(dict(message))
        return True, False, None, None, None


def create_plugin() -> HelloMaibotPlugin:
    """SDK 约定的插件工厂函数。"""
    return HelloMaibotPlugin()
