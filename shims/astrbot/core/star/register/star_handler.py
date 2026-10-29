"""AstrBot 装饰器实现（N.E.K.O. shim 子集）。

与上游 ``astrbot/core/star/register/star_handler.py`` 一一对应，差异只有两处：

* ``filter.llm_tool`` 的 docstring 解析不再依赖 ``docstring_parser``，改为标准库的
  Google / NumPy 风格轻量解析（``Args:`` 段落里的 ``name(type): desc``）；
  解析不到的参数行会被跳过而不是让插件导入失败；
* ``filter.agent`` / ``RegisteringAgent`` 没有提供（N.E.K.O. 没有 Agent 概念），
  传入 ``registering_agent`` 会抛 :class:`astrbot.UnsupportedCapabilityError`。
"""

from __future__ import annotations

import inspect
import logging
import re
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from ...._runtime import UnsupportedCapabilityError
from ..filter.command import CommandFilter
from ..filter.command_group import CommandGroupFilter
from ..filter.custom_filter import CustomFilterAnd, CustomFilterOr
from ..filter.event_message_type import EventMessageType, EventMessageTypeFilter
from ..filter.permission import PermissionType, PermissionTypeFilter
from ..filter.platform_adapter_type import (
    PlatformAdapterType,
    PlatformAdapterTypeFilter,
)
from ..filter.regex import RegexFilter
from ..llm_tools import llm_tools
from ..star_handler import EventType, StarHandlerMetadata, star_handlers_registry

logger = logging.getLogger("astrbot")

PY_TO_JSON_TYPE = {
    "int": "number",
    "float": "number",
    "bool": "boolean",
    "str": "string",
    "dict": "object",
    "list": "array",
    "tuple": "array",
    "set": "array",
}
"""Python 类型名 → JSON schema 类型（与上游 ``func_tool_manager`` 一致）。"""

SUPPORTED_TYPES = ["string", "number", "object", "array", "boolean"]
"""JSON schema 支持的类型。"""

_ARG_SECTION_NAMES = ("args:", "arguments:", "parameters:", "参数:", "参数：")
_SECTION_END_NAMES = (
    "returns:",
    "return:",
    "raises:",
    "yields:",
    "examples:",
    "example:",
    "notes:",
    "note:",
    "attributes:",
    "返回:",
    "返回：",
    "异常:",
    "示例:",
)
_ARG_LINE = re.compile(
    r"^(?P<name>\*{0,2}[A-Za-z_][\w]*)\s*"
    r"(?:[:(]\s*(?P<type>[^:)]*?)\s*\)?\s*)?"
    r":\s*(?P<desc>.*)$",
)


def get_handler_full_name(
    awaitable: Callable[..., Awaitable[Any] | AsyncGenerator[Any, None]],
) -> str:
    """取 Handler 的全名：``module_name + "_" + func_name``。"""
    return f"{awaitable.__module__}_{awaitable.__name__}"


def get_handler_or_create(
    handler: Callable[..., Any],
    event_type: EventType,
    dont_add: bool = False,
    **kwargs: Any,
) -> StarHandlerMetadata:
    """获取或创建一个 Handler 元数据（同名 Handler 复用同一条记录）。"""
    handler_full_name = get_handler_full_name(handler)
    metadata = star_handlers_registry.get_handler_by_full_name(handler_full_name)
    if metadata:
        return metadata
    metadata = StarHandlerMetadata(
        event_type=event_type,
        handler_full_name=handler_full_name,
        handler_name=handler.__name__,
        handler_module_path=handler.__module__,
        handler=handler,
        event_filters=[],
    )
    if handler.__doc__:
        metadata.desc = handler.__doc__.strip()
    if "desc" in kwargs:
        metadata.desc = kwargs["desc"]
        del kwargs["desc"]
    metadata.extras_configs = kwargs
    if not dont_add:
        star_handlers_registry.append(metadata)
    return metadata


def register_command(
    command_name: str | RegisteringCommandable | None = None,
    sub_command: str | None = None,
    alias: set[str] | None = None,
    **kwargs: Any,
) -> Callable[[Any], Any]:
    """注册一个 Command。"""
    new_command: CommandFilter | None = None
    add_to_event_filters = False
    if isinstance(command_name, RegisteringCommandable):
        if sub_command is not None:
            parent_command_names = command_name.parent_group.get_complete_command_names()
            new_command = CommandFilter(
                sub_command,
                alias,
                None,
                parent_command_names=parent_command_names,
            )
            command_name.parent_group.add_sub_command_filter(new_command)
        else:
            logger.warning(
                "No sub_command argument was provided while registering a subcommand.",
            )
    elif command_name is None:
        logger.warning("No command_name argument was provided while registering a bare command.")
    else:
        new_command = CommandFilter(command_name, alias, None)
        add_to_event_filters = True

    def decorator(awaitable: Any) -> Any:
        if not add_to_event_filters:
            # 标记为子指令：唤醒阶段会跳过它，由父指令组接管
            kwargs["sub_command"] = True
        handler_md = get_handler_or_create(
            awaitable,
            EventType.AdapterMessageEvent,
            **kwargs,
        )
        if new_command:
            new_command.init_handler_md(handler_md)
            handler_md.event_filters.append(new_command)
        return awaitable

    return decorator


def register_command_group(
    command_group_name: str | RegisteringCommandable | None = None,
    sub_command: str | None = None,
    alias: set[str] | None = None,
    **kwargs: Any,
) -> Callable[[Any], Any]:
    """注册一个 CommandGroup。"""
    new_group: CommandGroupFilter | None = None
    if isinstance(command_group_name, RegisteringCommandable):
        if sub_command is None:
            logger.warning(
                "No sub_command was specified for a subgroup of a command group.",
            )
        else:
            new_group = CommandGroupFilter(
                sub_command,
                alias,
                parent_group=command_group_name.parent_group,
            )
            command_group_name.parent_group.add_sub_command_filter(new_group)
    elif command_group_name is None:
        logger.warning("No name was specified for the root command group.")
    else:
        new_group = CommandGroupFilter(command_group_name, alias)

    def decorator(obj: Any) -> Any:
        if new_group:
            handler_md = get_handler_or_create(
                obj,
                EventType.AdapterMessageEvent,
                **kwargs,
            )
            handler_md.event_filters.append(new_group)
            return RegisteringCommandable(new_group)
        raise ValueError("注册指令组失败。")

    return decorator


def register_custom_filter(
    custom_type_filter: Any,
    *args: Any,
    **kwargs: Any,
) -> Callable[[Any], Any]:
    """注册一个自定义过滤器。"""
    add_to_event_filters = False
    raise_error = True
    parent_register_commandable: RegisteringCommandable | None = None
    if isinstance(custom_type_filter, RegisteringCommandable):
        parent_register_commandable = custom_type_filter
        custom_filter = args[0]
        if len(args) > 1:
            raise_error = args[1]
    else:
        add_to_event_filters = True
        custom_filter = custom_type_filter
        if args:
            raise_error = args[0]

    if not isinstance(custom_filter, (CustomFilterAnd, CustomFilterOr)):
        custom_filter = custom_filter(raise_error)

    def decorator(awaitable: Any) -> Any:
        if isinstance(awaitable, RegisteringCommandable):
            # 指令组：自定义过滤器挂到指令组上，和子指令一起判断
            awaitable.parent_group.add_custom_filter(custom_filter)
            return awaitable
        handler_md = get_handler_or_create(
            awaitable,
            EventType.AdapterMessageEvent,
            **kwargs,
        )
        if (
            not add_to_event_filters
            and parent_register_commandable is not None
        ):
            # 指令组下的子指令：把过滤器加到同名的子指令过滤器上
            handle_full_name = get_handler_full_name(awaitable)
            for sub_handler in parent_register_commandable.parent_group.sub_command_filters:
                if isinstance(sub_handler, CommandGroupFilter):
                    continue
                sub_handler_md = sub_handler.get_handler_md()
                if sub_handler_md and sub_handler_md.handler_full_name == handle_full_name:
                    sub_handler.add_custom_filter(custom_filter)
        else:
            handler_md.event_filters.append(custom_filter)
        return awaitable

    return decorator


class RegisteringCommandable:
    """用于指令组级联注册。

    ``group`` / ``command`` / ``custom_filter`` 是模块级注册函数，按上游的做法直接
    作为类属性挂在这里：``@math.command("add")`` 会以实例作为第一个位置参数调用，
    正好对上 ``register_command(command_name=<实例>, sub_command="add")``。
    """

    group: Callable[..., Any] = register_command_group
    command: Callable[..., Any] = register_command
    custom_filter: Callable[..., Any] = register_custom_filter

    def __init__(self, parent_group: CommandGroupFilter) -> None:
        self.parent_group = parent_group


def register_event_message_type(
    event_message_type: EventMessageType,
    **kwargs: Any,
) -> Callable[[Any], Any]:
    """注册一个 EventMessageType 过滤器。"""

    def decorator(awaitable: Any) -> Any:
        handler_md = get_handler_or_create(
            awaitable,
            EventType.AdapterMessageEvent,
            **kwargs,
        )
        handler_md.event_filters.append(EventMessageTypeFilter(event_message_type))
        return awaitable

    return decorator


def register_platform_adapter_type(
    platform_adapter_type: PlatformAdapterType,
    **kwargs: Any,
) -> Callable[[Any], Any]:
    """注册一个 PlatformAdapterType 过滤器。"""

    def decorator(awaitable: Any) -> Any:
        handler_md = get_handler_or_create(
            awaitable,
            EventType.AdapterMessageEvent,
            **kwargs,
        )
        handler_md.event_filters.append(
            PlatformAdapterTypeFilter(platform_adapter_type),
        )
        return awaitable

    return decorator


def register_regex(regex: str | re.Pattern[str], **kwargs: Any) -> Callable[[Any], Any]:
    """注册一个正则过滤器。"""

    def decorator(awaitable: Any) -> Any:
        handler_md = get_handler_or_create(
            awaitable,
            EventType.AdapterMessageEvent,
            **kwargs,
        )
        handler_md.event_filters.append(RegexFilter(regex))
        return awaitable

    return decorator


def register_permission_type(
    permission_type: PermissionType,
    raise_error: bool = True,
    **kwargs: Any,
) -> Callable[[Any], Any]:
    """注册一个 PermissionType 过滤器。"""

    def decorator(awaitable: Any) -> Any:
        handler_md = get_handler_or_create(
            awaitable,
            EventType.AdapterMessageEvent,
            **kwargs,
        )
        handler_md.event_filters.append(
            PermissionTypeFilter(permission_type, raise_error),
        )
        return awaitable

    return decorator


def _register_hook(event_type: EventType, **kwargs: Any) -> Callable[[Any], Any]:
    """注册一个事件钩子（0 参数 handler）。"""

    def decorator(awaitable: Any) -> Any:
        get_handler_or_create(awaitable, event_type, **kwargs)
        return awaitable

    return decorator


def register_on_astrbot_loaded(**kwargs: Any) -> Callable[[Any], Any]:
    """当 AstrBot 加载完成时。"""
    return _register_hook(EventType.OnAstrBotLoadedEvent, **kwargs)


def register_on_platform_loaded(**kwargs: Any) -> Callable[[Any], Any]:
    """当平台加载完成时。"""
    return _register_hook(EventType.OnPlatformLoadedEvent, **kwargs)


def register_on_plugin_error(**kwargs: Any) -> Callable[[Any], Any]:
    """当插件处理消息异常时（``event, plugin_name, handler_name, error, traceback_text``）。"""
    return _register_hook(EventType.OnPluginErrorEvent, **kwargs)


def register_on_plugin_loaded(**kwargs: Any) -> Callable[[Any], Any]:
    """当有插件加载完成时（``metadata``）。"""
    return _register_hook(EventType.OnPluginLoadedEvent, **kwargs)


def register_on_plugin_unloaded(**kwargs: Any) -> Callable[[Any], Any]:
    """当有插件卸载完成时（``metadata``）。"""
    return _register_hook(EventType.OnPluginUnloadedEvent, **kwargs)


def register_on_waiting_llm_request(**kwargs: Any) -> Callable[[Any], Any]:
    """等待调用 LLM 时的通知事件（``event``）。"""
    return _register_hook(EventType.OnWaitingLLMRequestEvent, **kwargs)


def register_on_llm_request(**kwargs: Any) -> Callable[[Any], Any]:
    """LLM 请求时（``event, req``）。"""
    return _register_hook(EventType.OnLLMRequestEvent, **kwargs)


def register_on_llm_response(**kwargs: Any) -> Callable[[Any], Any]:
    """LLM 响应后（``event, response``）。"""
    return _register_hook(EventType.OnLLMResponseEvent, **kwargs)


def register_on_agent_begin(**kwargs: Any) -> Callable[[Any], Any]:
    """Agent 开始运行时（``event, run_context``）。"""
    return _register_hook(EventType.OnAgentBeginEvent, **kwargs)


def register_on_agent_done(**kwargs: Any) -> Callable[[Any], Any]:
    """Agent 运行完成时（``event, run_context, response``）。"""
    return _register_hook(EventType.OnAgentDoneEvent, **kwargs)


def register_on_using_llm_tool(**kwargs: Any) -> Callable[[Any], Any]:
    """调用函数工具前（``event, tool, tool_args``）。"""
    return _register_hook(EventType.OnUsingLLMToolEvent, **kwargs)


def register_on_llm_tool_respond(**kwargs: Any) -> Callable[[Any], Any]:
    """调用函数工具后（``event, tool, tool_args, tool_result``）。"""
    return _register_hook(EventType.OnLLMToolRespondEvent, **kwargs)


def register_on_decorating_result(**kwargs: Any) -> Callable[[Any], Any]:
    """发送消息前（``event``）。"""
    return _register_hook(EventType.OnDecoratingResultEvent, **kwargs)


def register_after_message_sent(**kwargs: Any) -> Callable[[Any], Any]:
    """消息发送后（``event``）。"""
    return _register_hook(EventType.OnAfterMessageSentEvent, **kwargs)


def register_llm_tool(name: str | None = None, **kwargs: Any) -> Callable[[Any], Any]:
    """把一个函数注册成 LLM 工具（function-calling）。

    文档字符串会被解析成工具的 JSON schema：

    ```python
    @filter.llm_tool(name="get_weather")
    async def get_weather(event: AstrMessageEvent, location: str):
        \'\'\'获取天气信息。

        Args:
            location(string): 地点
        \'\'\'
    ```

    Raises:
        ValueError: 参数缺少类型注释，或类型不在 JSON schema 支持范围内。
        UnsupportedCapabilityError: 传入了 ``registering_agent``（N.E.K.O. 没有 Agent）。
    """
    if kwargs.get("registering_agent") is not None:
        raise UnsupportedCapabilityError(
            "filter.llm_tool.registering_agent",
            "N.E.K.O. 没有 AstrBot 的 Agent 概念",
        )
    name_ = name

    def decorator(awaitable: Callable[..., Any]) -> Callable[..., Any]:
        tool_name = name_ if name_ else awaitable.__name__
        doc_desc, func_args = parse_llm_tool_docstring(awaitable, tool_name)
        metadata = get_handler_or_create(awaitable, EventType.OnCallingFuncToolEvent)
        llm_tools.add_func(tool_name, func_args, doc_desc, metadata.handler)
        return awaitable

    return decorator


def parse_llm_tool_docstring(
    awaitable: Callable[..., Any],
    tool_name: str,
) -> tuple[str, list[dict[str, Any]]]:
    """解析函数的 docstring，返回 ``(描述, 参数列表)``。

    参数列表元素形如 ``{"type": "string", "name": "location", "description": "地点"}``，
    与上游 ``docstring_parser`` 的产出保持一致。
    """
    doc = inspect.getdoc(awaitable) or ""
    lines = doc.splitlines()
    section_index = None
    for index, line in enumerate(lines):
        if line.strip().lower() in _ARG_SECTION_NAMES:
            section_index = index
            break
    description = "\n".join(lines if section_index is None else lines[:section_index]).strip()
    func_args: list[dict[str, Any]] = []
    if section_index is None:
        return description, func_args
    current: dict[str, Any] | None = None
    for raw_line in lines[section_index + 1:]:
        stripped = raw_line.strip()
        if not stripped:
            continue
        if stripped.lower() in _SECTION_END_NAMES:
            break
        match = _ARG_LINE.match(stripped.lstrip("-*").strip())
        if match is None:
            if current is not None:
                extra = stripped
                current["description"] = (
                    f"{current['description']} {extra}".strip()
                    if current["description"]
                    else extra
                )
            continue
        arg_name = match.group("name")
        type_name = (match.group("type") or "").strip()
        if not type_name:
            raise ValueError(
                f"LLM 函数工具 {awaitable.__module__}_{tool_name} 的参数 {arg_name} 缺少类型注释。",
            )
        sub_type_name: str | None = None
        type_match = re.match(r"(\w+)\[(\w+)\]", type_name)
        if type_match:
            type_name = type_match.group(1)
            sub_type_name = type_match.group(2)
        json_type = PY_TO_JSON_TYPE.get(type_name, type_name)
        if sub_type_name:
            sub_type_name = PY_TO_JSON_TYPE.get(sub_type_name, sub_type_name)
        if json_type not in SUPPORTED_TYPES or (
            sub_type_name and sub_type_name not in SUPPORTED_TYPES
        ):
            raise ValueError(
                f"LLM 函数工具 {awaitable.__module__}_{tool_name} 不支持的参数类型：{type_name}",
            )
        arg_schema: dict[str, Any] = {
            "type": json_type,
            "name": arg_name,
            "description": match.group("desc").strip(),
        }
        if sub_type_name and json_type == "array":
            arg_schema["items"] = {"type": sub_type_name}
        func_args.append(arg_schema)
        current = arg_schema
    return description, func_args


__all__ = [
    "PY_TO_JSON_TYPE",
    "SUPPORTED_TYPES",
    "RegisteringCommandable",
    "get_handler_full_name",
    "get_handler_or_create",
    "parse_llm_tool_docstring",
    "register_after_message_sent",
    "register_command",
    "register_command_group",
    "register_custom_filter",
    "register_event_message_type",
    "register_llm_tool",
    "register_on_agent_begin",
    "register_on_agent_done",
    "register_on_astrbot_loaded",
    "register_on_decorating_result",
    "register_on_llm_request",
    "register_on_llm_response",
    "register_on_llm_tool_respond",
    "register_on_plugin_error",
    "register_on_plugin_loaded",
    "register_on_plugin_unloaded",
    "register_on_platform_loaded",
    "register_on_using_llm_tool",
    "register_on_waiting_llm_request",
    "register_permission_type",
    "register_platform_adapter_type",
    "register_regex",
]
