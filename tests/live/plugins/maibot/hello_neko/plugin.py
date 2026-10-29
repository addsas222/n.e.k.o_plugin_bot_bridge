"""示例麦麦（MaiBot）插件 —— 用来真机验证 bot_bridge 的转接链路。

声明了一条命令、一个工具、一个公开 API、一个事件处理器，并实现了 SDK 要求的
``on_load`` / ``on_unload`` / ``on_config_update``。命令命中时会调用
``self.ctx.send.text(...)``，这条文本应该经由 bot_bridge 出现在 N.E.K.O. 对话里。
"""

from __future__ import annotations

from typing import Any

from maibot_sdk import API, Command, EventHandler, MaiBotPlugin, Tool
from maibot_sdk.types import EventType, ToolParamType, ToolParameterInfo


class HelloNekoPlugin(MaiBotPlugin):
    """最小但完整的 SDK v2 插件。"""

    @Command(
        "hello",
        description="跟麦麦插件打个招呼",
        pattern=r"^\s*/?hello\b",
        aliases=["你好"],
    )
    async def handle_hello(self, stream_id: str = "", **kwargs: Any) -> dict[str, Any]:
        delivered = await self.ctx.send.text("你好呀，我是跑在 N.E.K.O. 里的麦麦插件 hello_neko。", stream_id)
        return {"content": "greeted", "plugin": "hello_neko", "delivered": bool(delivered)}

    @Tool(
        "echo",
        description="把传入的文本原样回显",
        parameters=[
            ToolParameterInfo(
                name="text",
                param_type=ToolParamType.STRING,
                description="要回显的文本",
                required=True,
            )
        ],
    )
    async def handle_echo(self, text: str = "", **kwargs: Any) -> dict[str, Any]:
        summary = await self.ctx.llm.generate(prompt=f"把这句话复述一遍：{text}")
        return {
            "content": text,
            "echoed": text,
            "llm_success": bool(summary.get("success")),
            "llm_response": summary.get("response", ""),
        }

    @API("greet", description="生成一句问候语", version="1", public=True)
    async def api_greet(self, name: str = "朋友", **kwargs: Any) -> dict[str, Any]:
        return {"content": f"你好，{name}！", "plugin": "hello_neko"}

    @EventHandler("neko_message_logger", description="记录看到的消息", event_type=EventType.ON_MESSAGE)
    async def on_message(self, **kwargs: Any) -> dict[str, Any]:
        self.ctx.logger.info("hello_neko 看到消息: %s", str(kwargs.get("plain_text", ""))[:120])
        return {"content": "logged"}

    async def on_load(self) -> None:
        self.ctx.logger.info("hello_neko 已加载（经 bot_bridge）")

    async def on_unload(self) -> None:
        self.ctx.logger.info("hello_neko 已卸载（经 bot_bridge）")

    async def on_config_update(self, scope: str, config_data: dict[str, Any], version: str) -> None:
        self.ctx.logger.info("hello_neko 收到配置更新 scope=%s version=%s", scope, version)


def create_plugin() -> HelloNekoPlugin:
    """MaiBot 运行时要求的工厂函数。"""
    return HelloNekoPlugin()
