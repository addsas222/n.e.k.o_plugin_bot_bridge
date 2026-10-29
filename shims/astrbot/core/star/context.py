"""插件接口上下文 ``Context``（N.E.K.O. shim 子集）。

映射到 N.E.K.O. 的能力（真实实现，不是占位）：

===================  =======================================================
AstrBot API          落到哪里
===================  =======================================================
``send_message``     ``HostServices.send_chat``（文本；非文本段记入日志）
``llm_generate``     单个 N.E.K.O. 模型槽位（``HostServices.llm_generate``）
``get_using_provider`` ``Provider`` 代理（``text_chat`` → ``llm_generate``）
``get_config``       插件的用户配置（``_conf_schema.json`` 默认值 + 宿主配置）
``get_registered_star`` / ``get_all_stars``  shim 的 ``star_map`` / ``star_registry``
``register_web_api`` 登记 ``ApiSpec``（路由对外暴露由宿主决定，调用见
                     ``call_api``）
``register_llm_tool`` / ``unregister_llm_tool`` / ``activate_llm_tool`` /
``deactivate_llm_tool`` / ``get_llm_tool_manager``  本地工具注册表
===================  =======================================================

其余上游 API（多 provider、TTS/STT/Embedding provider、平台/数据库/事件队列、
``tool_loop_agent``、``add_llm_tools``、``register_task``）没有 N.E.K.O. 等价实现，
一律抛 :class:`astrbot.UnsupportedCapabilityError` 并登记能力名。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..._runtime import RUNTIME, UnsupportedCapabilityError
from ..config import AstrBotConfig
from ..message.components import Plain
from ..message.message_event_result import MessageChain
from ..platform.message_session import MessageSession
from ..provider import LLMResponse, Provider
from ..star.filter.command import CommandFilter
from ..star.filter.regex import RegexFilter
from ..star.llm_tools import FunctionToolManager, llm_tools
from ..star.star import StarMetadata, star_registry
from ..star.star_handler import EventType, StarHandlerMetadata, star_handlers_registry

_PROVIDER_FACTORY = Callable[..., Provider]

_UNMAPPED_MANAGERS = (
    "provider_manager",
    "platform_manager",
    "conversation_manager",
    "message_history_manager",
    "persona_manager",
    "astrbot_config_mgr",
    "kb_manager",
    "cron_manager",
    "subagent_orchestrator",
)


class _UnsupportedManager:
    """上游 ``Context`` 上的管理器对象在 N.E.K.O. 侧不存在。

    保留一个会在任何属性访问时抛 :class:`astrbot.UnsupportedCapabilityError` 的
    占位对象：``hasattr(context, "provider_manager")`` 仍为 True，但一旦真的取用
    就会得到结构化失败，而不是静默拿到 ``None``。
    """

    def __init__(self, capability: str) -> None:
        object.__setattr__(self, "_capability", capability)

    def __getattr__(self, item: str) -> Any:
        raise UnsupportedCapabilityError(
            f"context.{self._capability}.{item}",
            "N.E.K.O. 没有 AstrBot 的管理器对象",
        )


class _HtmlRendererProxy:
    """``Context.html_renderer`` 的代理：方法名与上游一致，调用即显式失败。"""

    async def render_t2i(self, *args: Any, **kwargs: Any) -> Any:
        from astrbot.core import html_renderer as _renderer

        return await _renderer.render_t2i(*args, **kwargs)

    async def render_custom_template(self, *args: Any, **kwargs: Any) -> Any:
        from astrbot.core import html_renderer as _renderer

        return await _renderer.render_custom_template(*args, **kwargs)


_HTML_RENDERER_PROXY = _HtmlRendererProxy()


class Context:
    """暴露给插件的接口上下文（由兼容层构造，不使用上游的构造签名）。"""

    def __init__(
        self,
        *,
        bridge_plugin_id: str,
        native_plugin_id: str,
        host: Any,
        config: AstrBotConfig,
        provider_factory: _PROVIDER_FACTORY,
        plugin_name: str = "",
    ) -> None:
        self._bridge_plugin_id = bridge_plugin_id
        self._native_plugin_id = native_plugin_id
        self._host = host
        self._config = config
        self._provider_factory = provider_factory
        self._plugin_name = plugin_name or native_plugin_id
        self.registered_web_apis: list[tuple[str, Any, list[str], str]] = []
        for manager in _UNMAPPED_MANAGERS:
            object.__setattr__(self, manager, _UnsupportedManager(manager))

    @property
    def html_renderer(self) -> Any:
        """上游由核心生命周期注入 ``HtmlRenderer``；shim 给一个同名方法的代理。

        ``ctx.html_renderer.render_t2i(...)`` 在上游很常见。这里让属性存在、方法存在，
        但调用时抛 ``UnsupportedCapabilityError``（N.E.K.O. 没有 HTML→图片渲染服务），
        而不是 AttributeError —— 前者能告诉插件作者「这个能力没映射」。
        """
        return _HTML_RENDERER_PROXY

    # -- 内部工具 ---------------------------------------------------------- #

    def _unsupported(self, capability: str, detail: str = "") -> None:
        """登记并抛出"没有映射"的能力调用。"""
        RUNTIME.record_unsupported(self._native_plugin_id, capability)
        raise UnsupportedCapabilityError(capability, detail)

    # -- 消息 -------------------------------------------------------------- #

    async def send_message(
        self,
        session: str | MessageSession,
        message_chain: MessageChain,
    ) -> bool:
        """按 ``unified_msg_origin`` 主动发送消息。

        N.E.K.O. 只有一个对话，没有多平台/多会话路由，因此 ``session`` 只做格式
        校验，实际投递到宿主对话。

        Returns:
            宿主是否接受这条消息。

        Raises:
            ValueError: ``session`` 字符串不合法。
            UnsupportedCapabilityError: 消息链里没有可发送的文本内容。
        """
        if isinstance(session, str):
            try:
                MessageSession.from_str(session)
            except (ValueError, TypeError) as exc:
                raise ValueError("不合法的 session 字符串: " + str(exc)) from exc
        elif not isinstance(session, MessageSession):
            raise ValueError("不合法的 session: " + repr(session))
        chain = list(message_chain.chain or [])
        text = message_chain.get_plain_text()
        dropped = tuple(
            type(component).__name__
            for component in chain
            if not isinstance(component, Plain)
        )
        if not text and not dropped:
            return False
        if not text:
            self._unsupported(
                "context.send_message",
                f"消息链只有非文本消息段（{'/'.join(dropped)}）；N.E.K.O. 的 send_chat 只接受文本",
            )
        accepted = await self._host.send_text(
            self._bridge_plugin_id,
            text,
            umo=str(session),
            source=f"astrbot:{self._plugin_name}",
            dropped=dropped,
        )
        if dropped:
            RUNTIME.log(
                "warning",
                f"send_message dropped non-text components {dropped} from chain",
                plugin_id=self._bridge_plugin_id,
                source="astrbot.context",
            )
        return bool(accepted)

    # -- 模型 -------------------------------------------------------------- #

    async def llm_generate(
        self,
        *,
        chat_provider_id: str = "",
        prompt: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        tools: Any = None,
        system_prompt: str | None = None,
        contexts: list[Any] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """调用当前对话模型（落到 N.E.K.O. 的模型槽位）。"""
        if chat_provider_id and chat_provider_id not in ("neko", "default"):
            RUNTIME.log(
                "debug",
                f"llm_generate(chat_provider_id={chat_provider_id!r}) 被忽略："
                "N.E.K.O. 只有一个对话模型槽位",
                plugin_id=self._bridge_plugin_id,
                source="astrbot.context",
            )
        provider = self._provider_factory(provider_id=chat_provider_id or "neko")
        return await provider.text_chat(
            prompt=prompt,
            image_urls=image_urls,
            audio_urls=audio_urls,
            func_tool=tools,
            contexts=contexts,
            system_prompt=system_prompt,
            **kwargs,
        )

    def get_using_provider(self, umo: str | None = None) -> Provider:
        """取当前使用的对话模型 Provider（N.E.K.O. 只有这一个）。"""
        del umo
        return self._provider_factory(provider_id="neko")

    async def get_using_provider_async(self, umo: str | None = None) -> Provider:
        """异步版本（上游推荐接口）。"""
        return self.get_using_provider(umo)

    def get_current_chat_provider_id(self, umo: str | None = None) -> str:
        del umo
        return self._unsupported(
            "context.get_current_chat_provider_id",
            "N.E.K.O. 不暴露 provider id",
        )

    def get_provider_by_id(self, provider_id: str) -> Provider:
        self._unsupported(
            "context.get_provider_by_id",
            f"N.E.K.O. 只有一个对话模型槽位（请求 {provider_id!r}）",
        )

    def get_all_providers(self) -> list[Provider]:
        self._unsupported(
            "context.get_all_providers",
            "N.E.K.O. 只暴露一个对话模型槽位",
        )

    def get_all_tts_providers(self) -> list[Any]:
        self._unsupported("context.get_all_tts_providers", "N.E.K.O. 没有 TTS provider")

    def get_all_stt_providers(self) -> list[Any]:
        self._unsupported("context.get_all_stt_providers", "N.E.K.O. 没有 STT provider")

    def get_all_embedding_providers(self) -> list[Any]:
        self._unsupported(
            "context.get_all_embedding_providers",
            "N.E.K.O. 没有 Embedding provider 列表",
        )

    def get_using_tts_provider(self, umo: str | None = None) -> Any:
        del umo
        self._unsupported("context.get_using_tts_provider", "N.E.K.O. 没有 TTS provider")

    async def get_using_tts_provider_async(self, umo: str | None = None) -> Any:
        return self.get_using_tts_provider(umo)

    def get_using_stt_provider(self, umo: str | None = None) -> Any:
        del umo
        self._unsupported("context.get_using_stt_provider", "N.E.K.O. 没有 STT provider")

    async def get_using_stt_provider_async(self, umo: str | None = None) -> Any:
        return self.get_using_stt_provider(umo)

    async def tool_loop_agent(self, **kwargs: Any) -> Any:
        del kwargs
        self._unsupported(
            "context.tool_loop_agent",
            "N.E.K.O. 不提供 Agent 循环；工具调用由宿主决定",
        )

    # -- 插件 / 配置 ------------------------------------------------------- #

    def get_registered_star(self, star_name: str) -> StarMetadata | None:
        """按插件名取元数据。"""
        for metadata in star_registry:
            if metadata.name == star_name:
                return metadata
        return None

    def get_all_stars(self) -> list[StarMetadata]:
        """所有已注册插件的元数据。"""
        return list(star_registry)

    def get_config(self, umo: str | None = None) -> AstrBotConfig:
        """取插件配置（``umo`` 在 N.E.K.O. 里没有会话级配置，忽略）。"""
        del umo
        return self._config

    def register_web_api(
        self,
        route: str,
        view_handler: Callable[..., Awaitable[Any]],
        methods: list[str],
        desc: str,
    ) -> None:
        """登记一个 Web API（会导出成 ``ApiSpec``；N.E.K.O. 侧不承载 HTTP 路由）。"""
        for index, api in enumerate(self.registered_web_apis):
            if api[0] == route and methods == api[2]:
                self.registered_web_apis[index] = (route, view_handler, methods, desc)
                return
        self.registered_web_apis.append((route, view_handler, methods, desc))

    # -- LLM 工具（本地注册表） -------------------------------------------- #

    def get_llm_tool_manager(self) -> FunctionToolManager:
        """取本地 LLM 工具注册表。"""
        return llm_tools

    def register_llm_tool(
        self,
        name: str,
        func_args: list[dict[str, Any]],
        desc: str,
        func_obj: Callable[..., Awaitable[Any]],
    ) -> None:
        """[已废弃] 注册一个 LLM 工具（与上游行为一致，会生成 ``ToolSpec``）。"""
        metadata = StarHandlerMetadata(
            event_type=EventType.OnLLMRequestEvent,
            handler_full_name=f"{func_obj.__module__}_{func_obj.__name__}",
            handler_name=func_obj.__name__,
            handler_module_path=func_obj.__module__,
            handler=func_obj,
            event_filters=[],
            desc=desc,
        )
        star_handlers_registry.append(metadata)
        llm_tools.add_func(name, func_args, desc, func_obj)

    def unregister_llm_tool(self, name: str) -> None:
        """[已废弃] 移除一个 LLM 工具。"""
        llm_tools.remove_func(name)

    def activate_llm_tool(self, name: str) -> bool:
        """启用某个 LLM 工具。"""
        tool = llm_tools.get_func(name)
        if tool is None:
            return False
        tool.active = True
        return True

    async def activate_llm_tool_async(self, name: str) -> bool:
        """异步版本。"""
        return self.activate_llm_tool(name)

    def deactivate_llm_tool(self, name: str) -> bool:
        """禁用某个 LLM 工具。"""
        tool = llm_tools.get_func(name)
        if tool is None:
            return False
        tool.active = False
        return True

    async def deactivate_llm_tool_async(self, name: str) -> bool:
        """异步版本。"""
        return self.deactivate_llm_tool(name)

    def add_llm_tools(self, *tools: Any) -> None:
        del tools
        self._unsupported(
            "context.add_llm_tools",
            "AstrBot 的 FunctionTool 对象没有映射；请用 @filter.llm_tool 或 register_llm_tool",
        )

    def register_commands(
        self,
        star_name: str,
        command_name: str,
        desc: str,
        priority: int,
        awaitable: Callable[..., Awaitable[Any]],
        use_regex: bool = False,
        ignore_prefix: bool = False,
    ) -> None:
        """[已废弃] 以旧接口注册一条指令（与上游行为一致）。"""
        del star_name
        if ignore_prefix:
            self._unsupported(
                "context.register_commands.ignore_prefix",
                "ignore_prefix 需要平台适配器支持，N.E.K.O. 侧不适用",
            )
        metadata: StarHandlerMetadata = StarHandlerMetadata(
            event_type=EventType.AdapterMessageEvent,
            handler_full_name=f"{awaitable.__module__}_{awaitable.__name__}",
            handler_name=awaitable.__name__,
            handler_module_path=awaitable.__module__,
            handler=awaitable,
            event_filters=[],
            desc=desc,
            extras_configs={"priority": priority},
        )
        if use_regex:
            metadata.event_filters.append(RegexFilter(command_name))
        else:
            metadata.event_filters.append(CommandFilter(command_name, handler_md=metadata))
        star_handlers_registry.append(metadata)

    # -- 没有映射的宿主设施 ------------------------------------------------ #

    def get_event_queue(self) -> Any:
        self._unsupported("context.get_event_queue", "N.E.K.O. 的消息总线不向插件暴露队列")

    def get_platform(self, platform_type: Any) -> Any:
        self._unsupported(
            "context.get_platform",
            f"N.E.K.O. 没有平台适配器实例（请求 {platform_type!r}）",
        )

    def get_platform_inst(self, platform_id: str) -> Any:
        self._unsupported(
            "context.get_platform_inst",
            f"N.E.K.O. 没有平台适配器实例（请求 {platform_id!r}）",
        )

    def get_db(self) -> Any:
        self._unsupported("context.get_db", "N.E.K.O. 不向插件暴露数据库")

    def register_provider(self, provider: Any) -> None:
        del provider
        self._unsupported("context.register_provider", "N.E.K.O. 不接受插件注册模型 provider")

    def register_task(self, task: Awaitable[Any], desc: str) -> None:
        del desc
        close = getattr(task, "close", None)
        if callable(close):
            close()
        self._unsupported(
            "context.register_task",
            "N.E.K.O. 没有插件后台任务的挂载点；请在插件自己的生命周期里起任务",
        )


__all__ = ["Context"]
