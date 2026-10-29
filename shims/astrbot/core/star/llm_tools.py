"""LLM 工具（function-calling）注册表（N.E.K.O. shim 子集）。

上游把 ``@filter.llm_tool`` 装饰的函数登记进 ``FunctionToolManager``，由 Agent 在
对话中调用，调用形式是 ``handler(event, **kwargs)``。本 shim 保留同名注册表与
``add_func`` / ``remove_func`` / ``get_func``，桥接层再把它们映射成 N.E.K.O. 的
LLM 工具（``ToolSpec`` + ``call_tool``）。
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class LLMToolRegistration:
    """一个已注册的 LLM 工具（shim 内部结构）。"""

    name: str
    description: str
    parameters: dict[str, Any]
    """JSON schema：``{"type": "object", "properties": {...}}``"""
    arguments: list[dict[str, Any]]
    """原始参数列表：``[{"type", "name", "description"}, ...]``（与上游一致）"""
    handler: Callable[..., Any]
    handler_module_path: str
    active: bool = True


class FunctionToolManager:
    """LLM 工具注册表。"""

    def __init__(self) -> None:
        self.func_list: list[LLMToolRegistration] = []

    def add_func(
        self,
        name: str,
        func_args: list[dict[str, Any]],
        desc: str,
        handler: Callable[..., Any],
    ) -> None:
        """添加（或替换）一个函数工具。

        Args:
            name: 工具名。
            func_args: 参数列表，元素形如
                ``{"type": "string", "name": "location", "description": "地点"}``。
            desc: 工具描述。
            handler: 处理函数，调用形式 ``handler(event, **kwargs)``。
        """
        self.remove_func(name)
        module_path = _handler_module_path(handler)
        self.func_list.append(
            LLMToolRegistration(
                name=name,
                description=desc,
                parameters=_build_parameters(func_args),
                arguments=list(func_args),
                handler=handler,
                handler_module_path=module_path,
            ),
        )

    def remove_func(self, name: str) -> None:
        """按名字移除工具。"""
        for index, item in enumerate(self.func_list):
            if item.name == name:
                self.func_list.pop(index)
                break

    def get_func(self, name: str) -> LLMToolRegistration | None:
        """按名字取工具（激活的优先，与上游一致）。"""
        for item in reversed(self.func_list):
            if item.name == name and getattr(item, "active", True):
                return item
        for item in reversed(self.func_list):
            if item.name == name:
                return item
        return None

    # -- shim 扩展：按插件模块管理 ------------------------------------------ #

    def get_by_module(self, module_path: str) -> list[LLMToolRegistration]:
        """取某个插件模块注册的全部工具。"""
        return [
            item for item in self.func_list if item.handler_module_path == module_path
        ]

    def bind_module(self, module_path: str, instance: object) -> list[LLMToolRegistration]:
        """把该模块的工具处理函数绑定到插件实例，返回绑定后的列表。"""
        related = self.get_by_module(module_path)
        for item in related:
            raw_handler = (
                item.handler.func
                if isinstance(item.handler, functools.partial)
                else item.handler
            )
            item.handler = raw_handler
            if instance is not None and getattr(raw_handler, "__self__", None) is not instance:
                # 同 StarHandlerRegistry.bind_module：只绑裸函数，已绑定的方法不动。
                item.handler = functools.partial(raw_handler, instance)
        return related

    def unload_module(self, module_path: str) -> None:
        """移除某插件模块注册的全部工具。"""
        self.func_list = [
            item for item in self.func_list if item.handler_module_path != module_path
        ]


def _handler_module_path(handler: Callable[..., Any]) -> str:
    """取处理函数所属模块路径（``functools.partial`` 会向下取一层）。"""
    if isinstance(handler, functools.partial):
        handler = handler.func
    return str(getattr(handler, "__module__", "") or "")


def _build_parameters(func_args: list[dict[str, Any]]) -> dict[str, Any]:
    """把上游的参数列表转成 JSON schema。"""
    properties: dict[str, Any] = {}
    for item in func_args:
        prop = {key: value for key, value in item.items() if key != "name"}
        properties[str(item["name"])] = prop
    return {"type": "object", "properties": properties}


llm_tools = FunctionToolManager()
"""全局工具注册表（与上游同名）。"""

__all__ = ["FunctionToolManager", "LLMToolRegistration", "llm_tools"]
