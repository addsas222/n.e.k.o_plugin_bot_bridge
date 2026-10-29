"""MaiBot 兼容层测试用的旧版插件（``src.plugin_system`` 风格）。

模块里 **没有** ``create_plugin()`` 工厂，靠 ``@register_plugin`` 标记 +
``maibot_sdk.compat`` 导入钩子被识别，因此必须走
``LegacyPluginAdapter`` 这条路径。
"""

from __future__ import annotations

from typing import Any

from src.plugin_system import (
    BaseAction,
    BaseCommand,
    BaseEventHandler,
    BasePlugin,
    BaseTool,
    EventType,
    ToolParamType,
    register_plugin,
)
from src.plugin_system.apis import send_api


class LegacyEchoCommand(BaseCommand):
    """旧版命令：正则带命名捕获组。"""

    command_name = "legacy_echo"
    command_description = "旧版回显命令"
    command_pattern = r"^!legacy\s+(?P<text>.+)$"

    async def execute(self) -> tuple[bool, str | None, int]:
        """回显 ``!legacy <text>`` 后面的内容。"""
        payload = str(self.matched_groups.get("text") or "")
        await send_api.text_to_stream(text=f"legacy echo：{payload}", stream_id=self._get_stream_id())
        return True, f"legacy echo：{payload}", 1


class LegacySumTool(BaseTool):
    """旧版工具：相加两个数。"""

    name = "legacy_sum"
    description = "旧版求和工具"
    parameters = [
        ("left", ToolParamType.FLOAT, "左操作数", True, None),
        ("right", ToolParamType.FLOAT, "右操作数", True, None),
    ]

    async def execute(self, function_args: dict[str, Any]) -> dict[str, Any]:
        """返回两数之和。"""
        total = float(function_args.get("left") or 0) + float(function_args.get("right") or 0)
        return {"name": self.name, "content": str(total)}


class LegacyGreetAction(BaseAction):
    """旧版 Action：用配置里的问候语回一句话。"""

    action_name = "legacy_greet"
    action_description = "旧版问候动作"
    action_parameters = {"who": "要问候的对象"}
    associated_types = ["text"]

    async def execute(self) -> tuple[bool, str]:
        """执行问候动作（参数由 ``action_data`` 注入）。"""
        who = str(self.action_data.get("who") or "朋友")
        # 旧版 BaseAction 没有 _get_stream_id()，只有 chat_id / _stream_id 两个属性。
        stream_id = str(self.chat_id or self._stream_id or "")
        if stream_id:
            await send_api.text_to_stream(text=f"你好，{who}！", stream_id=stream_id)
        return True, f"legacy greet：{who}"


class LegacyMessageHandler(BaseEventHandler):
    """旧版事件处理器：记录每条入站消息。"""

    handler_name = "legacy_message_handler"
    handler_description = "旧版消息事件处理器"
    event_type = EventType.ON_MESSAGE
    weight = 10
    intercept_message = False

    def __init__(self) -> None:
        super().__init__()
        self.handled_texts: list[str] = []

    async def execute(self, message: Any) -> tuple[bool, bool, str | None, None, None]:
        """把消息文本记下来。"""
        text = ""
        if isinstance(message, dict):
            text = str(message.get("plain_text") or "")
        self.handled_texts.append(text)
        return True, False, None, None, None


@register_plugin
class LegacyDemoPlugin(BasePlugin):
    """旧版插件入口。"""

    plugin_name = "legacy_maibot"
    enable_plugin = True
    dependencies = []
    python_dependencies = []
    config_file_name = "config.toml"

    def __init__(self) -> None:
        self.loaded = False
        self.unloaded = False

    async def on_load(self) -> None:
        """生命周期：加载。"""
        self.loaded = True

    async def on_unload(self) -> None:
        """生命周期：卸载。"""
        self.unloaded = True

    def get_plugin_components(self) -> list[tuple[Any, type]]:
        """声明插件包含的组件。"""
        return [
            (LegacyEchoCommand.get_command_info(), LegacyEchoCommand),
            (LegacySumTool.get_tool_info(), LegacySumTool),
            (LegacyGreetAction.get_action_info(), LegacyGreetAction),
            (LegacyMessageHandler.get_handler_info(), LegacyMessageHandler),
        ]
