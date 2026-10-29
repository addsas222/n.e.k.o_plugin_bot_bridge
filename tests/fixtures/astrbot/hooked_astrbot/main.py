"""bot_bridge 的 AstrBot 兼容层测试插件（钩子 / Web API / 旧接口密集）。

覆盖：所有 ``@filter.on_*`` 钩子、消息监听器、``context.register_web_api``、
``context.register_llm_tool``（旧接口）、以及 Handler 抛异常时的
``on_plugin_error`` 链路。
"""

from __future__ import annotations

from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools


class HookedAstrBot(Star):
    """钩子密集型测试插件。"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
        self.hook_calls: list[tuple[str, str]] = []
        self.error_hooks: list[str] = []
        self.terminated = False
        self.data_dir = StarTools.get_data_dir()
        self.named_data_dir = StarTools.get_data_dir("hooked_astrbot")
        self.context.register_web_api("/hooked/ping", self.web_ping, ["GET"], "健康检查")
        self.context.register_llm_tool(
            "legacy_echo",
            [{"type": "string", "name": "text", "description": "文本"}],
            "回显文本",
            self.legacy_echo,
        )

    # -- 旧接口注册的东西 -------------------------------------------------- #

    async def web_ping(self) -> dict[str, Any]:
        """Web API 视图（桥接层只登记，不承载 HTTP 路由）。"""
        return {"ok": True}

    async def legacy_echo(self, event: AstrMessageEvent, text: str) -> str:
        """旧接口注册的 LLM 工具（旧接口约定：第一个参数是 event）。"""
        return f"legacy:{text}"

    def _record(self, name: str, detail: str = "") -> None:
        self.hook_calls.append((name, detail))

    # -- 指令 -------------------------------------------------------------- #

    @filter.command("star_tools")
    async def star_tools(self, event: AstrMessageEvent):
        """通过 StarTools 主动发消息。"""
        sent = await StarTools.send_message(
            event.unified_msg_origin,
            MessageChain().message("star tools"),
        )
        yield event.plain_result(f"star_tools={sent}")

    @filter.command("boom")
    async def boom(self, event: AstrMessageEvent):
        """故意抛异常，触发 on_plugin_error。"""
        raise RuntimeError("boom")
        yield event.plain_result("never")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent) -> None:
        """所有消息都记一笔。"""
        self._record("message", event.get_message_str())

    # -- 钩子 -------------------------------------------------------------- #

    @filter.on_astrbot_loaded()
    async def loaded(self) -> None:
        """AstrBot 加载完成。"""
        self._record("on_astrbot_loaded")

    @filter.on_platform_loaded()
    async def platform_loaded(self) -> None:
        """平台加载完成。"""
        self._record("on_platform_loaded")

    @filter.on_plugin_loaded()
    async def plugin_loaded(self, metadata: Any) -> None:
        """有插件加载完成。"""
        name = metadata.get("name") if isinstance(metadata, dict) else getattr(metadata, "name", "")
        self._record("on_plugin_loaded", str(name))

    @filter.on_plugin_unloaded()
    async def plugin_unloaded(self, metadata: Any) -> None:
        """有插件卸载完成。"""
        name = metadata.get("name") if isinstance(metadata, dict) else getattr(metadata, "name", "")
        self._record("on_plugin_unloaded", str(name))

    @filter.on_plugin_error()
    async def plugin_error(
        self,
        event: AstrMessageEvent,
        plugin_name: str,
        handler_name: str,
        error: Any,
        traceback_text: str,
    ) -> None:
        """插件处理消息异常。"""
        self.error_hooks.append(f"{plugin_name}:{handler_name}:{error}")
        self._record("on_plugin_error", f"{handler_name}:{traceback_text.splitlines()[-1] if traceback_text else ''}")

    @filter.on_waiting_llm_request()
    async def waiting_llm(self, event: AstrMessageEvent) -> None:
        """等待 LLM 请求。"""
        self._record("on_waiting_llm_request", event.unified_msg_origin)

    @filter.on_llm_request()
    async def llm_request(self, event: AstrMessageEvent, req: Any) -> None:
        """LLM 请求前。"""
        req.system_prompt = f"{req.system_prompt or ''}[neko-hooked]"
        self._record("on_llm_request", f"{event.unified_msg_origin}|{req.system_prompt}")

    @filter.on_llm_response()
    async def llm_response(self, event: AstrMessageEvent, response: Any) -> None:
        """LLM 响应后。"""
        self._record("on_llm_response", str(getattr(response, "completion_text", "")))

    @filter.on_agent_begin()
    async def agent_begin(self, event: AstrMessageEvent, run_context: Any) -> None:
        """Agent 开始。"""
        self._record("on_agent_begin", repr(run_context))

    @filter.on_agent_done()
    async def agent_done(self, event: AstrMessageEvent, run_context: Any, response: Any) -> None:
        """Agent 结束。"""
        self._record("on_agent_done", repr(run_context))

    @filter.on_using_llm_tool()
    async def using_tool(self, event: AstrMessageEvent, tool: Any, tool_args: Any) -> None:
        """调用工具前。"""
        self._record("on_using_llm_tool", str(getattr(tool, "name", tool)))

    @filter.on_llm_tool_respond()
    async def tool_respond(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: Any,
        tool_result: Any,
    ) -> None:
        """调用工具后。"""
        self._record("on_llm_tool_respond", str(getattr(tool, "name", tool)))

    @filter.on_decorating_result()
    async def decorating_result(self, event: AstrMessageEvent) -> None:
        """发送消息前。"""
        self._record("on_decorating_result", event.get_message_str())

    @filter.after_message_sent()
    async def message_sent(self, event: AstrMessageEvent) -> None:
        """消息发送后。"""
        self._record("after_message_sent", event.get_message_str())

    # -- 生命周期 ---------------------------------------------------------- #

    async def initialize(self) -> None:
        """插件激活。"""
        logger.info("hooked_astrbot initialize")

    async def terminate(self) -> None:
        """插件停用。"""
        self.terminated = True
