"""Handler 元数据与全局注册表（N.E.K.O. shim 子集）。

与上游同名同语义；另外提供三个 shim 扩展方法给兼容层使用（上游没有，但语义是
上游 ``star_manager`` 在加载/卸载插件时做的事）：

* :meth:`StarHandlerRegistry.bind_module` —— 把装饰器注册的裸函数绑定到插件实例
  （等价于上游用 ``functools.partial`` 绑定 ``metadata.star_cls``）；
* :meth:`StarHandlerRegistry.unload_module` —— 卸载某插件的全部 Handler；
* :meth:`StarHandlerRegistry.get_handlers_by_module_name` —— 上游已有，兼容层用。
"""

from __future__ import annotations

import enum
import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from .filter import HandlerFilter
from .star import star_map

T = TypeVar("T", bound="StarHandlerMetadata")


class EventType(enum.Enum):
    """AstrBot 内部事件类型（成员名与上游完全一致）。"""

    OnAstrBotLoadedEvent = enum.auto()  # AstrBot 加载完成
    OnPlatformLoadedEvent = enum.auto()  # 平台加载完成

    AdapterMessageEvent = enum.auto()  # 收到适配器发来的消息
    OnWaitingLLMRequestEvent = enum.auto()  # 等待调用 LLM（仅通知）
    OnLLMRequestEvent = enum.auto()  # 收到 LLM 请求
    OnLLMResponseEvent = enum.auto()  # LLM 响应后
    OnAgentBeginEvent = enum.auto()  # Agent 开始运行
    OnAgentDoneEvent = enum.auto()  # Agent 运行完成
    OnDecoratingResultEvent = enum.auto()  # 发送消息前
    OnCallingFuncToolEvent = enum.auto()  # 调用函数工具
    OnUsingLLMToolEvent = enum.auto()  # 使用 LLM 工具
    OnLLMToolRespondEvent = enum.auto()  # 调用函数工具后
    OnAfterMessageSentEvent = enum.auto()  # 消息发送后
    OnPluginErrorEvent = enum.auto()  # 插件处理消息异常时
    OnPluginLoadedEvent = enum.auto()  # 插件加载完成
    OnPluginUnloadedEvent = enum.auto()  # 插件卸载完成


H = TypeVar("H", bound=Callable[..., Any])


@dataclass
class StarHandlerMetadata(Generic[H]):
    """描述一个 Star 注册的某个 Handler。"""

    event_type: EventType
    handler_full_name: str
    handler_name: str
    handler_module_path: str
    handler: H
    event_filters: list[HandlerFilter]
    desc: str = ""
    extras_configs: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True

    def __lt__(self, other: StarHandlerMetadata) -> bool:
        return self.extras_configs.get("priority", 0) < other.extras_configs.get("priority", 0)


class StarHandlerRegistry(Generic[T]):
    """Handler 注册表：按 priority 从高到低保存。"""

    def __init__(self) -> None:
        self.star_handlers_map: dict[str, StarHandlerMetadata] = {}
        self._handlers: list[StarHandlerMetadata] = []

    def append(self, handler: StarHandlerMetadata) -> None:
        """添加一个 Handler，并保持按优先级有序。"""
        if "priority" not in handler.extras_configs:
            handler.extras_configs["priority"] = 0
        self.star_handlers_map[handler.handler_full_name] = handler
        self._handlers.append(handler)
        self._handlers.sort(key=lambda item: -item.extras_configs["priority"])

    def get_handler_by_full_name(self, full_name: str) -> StarHandlerMetadata | None:
        """按全名取 Handler。"""
        return self.star_handlers_map.get(full_name, None)

    def get_handlers_by_module_name(self, module_name: str) -> list[StarHandlerMetadata]:
        """取某个插件模块注册的全部 Handler。"""
        return [
            handler
            for handler in self._handlers
            if handler.handler_module_path == module_name
        ]

    def get_handlers_by_event_type(
        self,
        event_type: EventType,
        only_activated: bool = True,
        plugins_name: list[str] | None = None,
    ) -> list[StarHandlerMetadata]:
        """按事件类型取 Handler（含启用状态与插件白名单过滤）。"""
        handlers: list[StarHandlerMetadata] = []
        for handler in self._handlers:
            if handler.event_type != event_type:
                continue
            if not handler.enabled:
                continue
            if only_activated:
                plugin = star_map.get(handler.handler_module_path)
                if not (plugin and plugin.activated):
                    continue
            if plugins_name is not None and plugins_name != ["*"]:
                plugin = star_map.get(handler.handler_module_path)
                if not plugin:
                    continue
                if (
                    plugin.name not in plugins_name
                    and event_type
                    not in (
                        EventType.OnAstrBotLoadedEvent,
                        EventType.OnPlatformLoadedEvent,
                        EventType.OnPluginLoadedEvent,
                        EventType.OnPluginUnloadedEvent,
                    )
                    and not plugin.reserved
                ):
                    continue
            handlers.append(handler)
        return handlers

    def remove(self, handler: StarHandlerMetadata) -> None:
        """移除一个 Handler。"""
        self.star_handlers_map.pop(handler.handler_full_name, None)
        self._handlers = [item for item in self._handlers if item != handler]

    def clear(self) -> None:
        """清空注册表。"""
        self.star_handlers_map.clear()
        self._handlers.clear()

    def __iter__(self):
        return iter(self._handlers)

    def __len__(self) -> int:
        return len(self._handlers)

    # -- shim 扩展：加载 / 卸载时使用 -------------------------------------- #

    def bind_module(
        self,
        module_name: str,
        instance: object,
    ) -> list[StarHandlerMetadata]:
        """把该模块的 Handler 绑定到插件实例，返回绑定后的 Handler 列表。

        与上游 ``PluginManager`` 的做法一致：先把可能存在的旧 ``partial`` 还原成裸
        函数，再重新 ``functools.partial(raw, instance)``，因此重复加载不会叠加。
        """
        related = self.get_handlers_by_module_name(module_name)
        for handler in related:
            raw_handler = (
                handler.handler.func
                if isinstance(handler.handler, functools.partial)
                else handler.handler
            )
            handler.handler = raw_handler
            if instance is not None and getattr(raw_handler, "__self__", None) is not instance:
                # 装饰器注册的是裸函数，需要绑到实例上；而运行时（例如 __init__ 里
                # context.register_commands）注册的已经是绑定方法，绑第二次会把实例
                # 当成 event 传进去——上游没有区分，这里区分。
                handler.handler = functools.partial(raw_handler, instance)
        return related

    def unload_module(self, module_name: str) -> None:
        """移除某插件模块注册的全部 Handler（卸载时调用）。"""
        for handler in self.get_handlers_by_module_name(module_name):
            self.remove(handler)


star_handlers_registry: StarHandlerRegistry = StarHandlerRegistry()

__all__ = [
    "EventType",
    "StarHandlerMetadata",
    "StarHandlerRegistry",
    "star_handlers_registry",
]
