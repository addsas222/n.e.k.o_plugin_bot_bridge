"""插件便捷工具 ``StarTools``（N.E.K.O. shim 子集）。

上游（``astrbot.core.star.star_tools``）里它是宿主注入的便捷入口；本 shim 保留同名
的类方法与调用形式：

* ``initialize`` / ``send_message`` / ``create_message`` / ``get_data_dir`` /
  ``register_llm_tool`` / ``unregister_llm_tool`` / ``activate_llm_tool(_async)`` /
  ``deactivate_llm_tool(_async)`` 都是真实实现（发送落到 ``send_chat``，数据目录落到
  宿主的数据目录，工具开关落到本地工具注册表）；
* ``create_event`` 需要平台适配器实例，N.E.K.O. 侧没有，抛
  :class:`astrbot.UnsupportedCapabilityError`。

差异（已记录）：``get_data_dir`` 返回 N.E.K.O. 给该插件的数据目录（不是
``data/plugin_data/<name>``）；``_context`` 与上游一样是类级单例，桥接层在加载每个
插件时写入该插件的 ``Context``，因此它记录的是最后一次初始化——各插件的 ``Context``
都指向同一个 N.E.K.O. 宿主，投递行为不受影响，只有日志归属可能落在最后加载的插件上。
"""

from __future__ import annotations

import inspect
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, ClassVar

from ..._runtime import RUNTIME, UnsupportedCapabilityError
from ..message.components import BaseMessageComponent
from ..message.message_event_result import MessageChain
from ..platform.astrbot_message import AstrBotMessage, MessageMember
from ..platform.message_session import MessageSession
from ..platform.message_type import MessageType


class StarTools:
    """插件便捷工具。"""

    _context: ClassVar[Any] = None

    @classmethod
    def initialize(cls, context: Any) -> None:
        """注入插件上下文（由桥接层在加载插件时调用）。"""
        cls._context = context

    @classmethod
    def _require_context(cls) -> Any:
        if cls._context is None:
            raise ValueError("StarTools not initialized")
        return cls._context

    @classmethod
    async def send_message(
        cls,
        session: str | MessageSession,
        message_chain: MessageChain,
    ) -> bool:
        """按 ``unified_msg_origin`` 主动发送消息（落到 N.E.K.O. 的 ``send_chat``）。"""
        return await cls._require_context().send_message(session, message_chain)

    @classmethod
    async def create_message(
        cls,
        type: str,
        self_id: str,
        session_id: str,
        sender: MessageMember,
        message: list[BaseMessageComponent],
        message_str: str,
        message_id: str = "",
        raw_message: object = None,
        group_id: str = "",
    ) -> AstrBotMessage:
        """构造一个 ``AstrBotMessage``（与上游同签名）。"""
        abm = AstrBotMessage()
        abm.type = MessageType(type)
        abm.self_id = self_id
        abm.session_id = session_id
        abm.message_id = message_id or uuid.uuid4().hex
        abm.sender = sender
        abm.message = message
        abm.message_str = message_str
        abm.raw_message = raw_message
        abm.group_id = group_id
        return abm

    @classmethod
    async def create_event(
        cls,
        abm: AstrBotMessage,
        platform: str = "aiocqhttp",
        is_wake: bool = True,
    ) -> None:
        """把消息对象提交给消息平台（未映射）。"""
        del abm, is_wake
        cls._require_context()
        raise UnsupportedCapabilityError(
            "star_tools.create_event",
            f"N.E.K.O. 没有平台适配器实例，无法把事件提交到 {platform!r}",
        )

    @classmethod
    def get_data_dir(cls, plugin_name: str | None = None) -> Path:
        """返回插件的数据目录（已保证存在）。

        Args:
            plugin_name: 插件名；为空时从调用栈里识别调用方插件。

        Raises:
            RuntimeError: 无法从调用栈识别插件时。
            ValueError: 插件名为空时。
        """
        native_id = _native_plugin_id(plugin_name) if plugin_name else None
        if native_id is None and not plugin_name:
            native_id = _caller_native_plugin_id()
        if native_id is not None:
            bound = RUNTIME.plugin_data_dir(native_id)
            if bound is not None:
                return bound.resolve()
        if not plugin_name:
            raise ValueError("Unable to resolve plugin name")
        # 显式给了名字但插件没加载：与上游一样，按名字建一个数据目录
        return RUNTIME.host().data_dir(plugin_name).resolve()

    @classmethod
    def register_llm_tool(
        cls,
        name: str,
        func_args: list[dict[str, Any]],
        desc: str,
        func_obj: Callable[..., Awaitable[Any]],
    ) -> None:
        """[已废弃] 注册一个 LLM 工具。"""
        cls._require_context().register_llm_tool(name, func_args, desc, func_obj)

    @classmethod
    def unregister_llm_tool(cls, name: str) -> None:
        """[已废弃] 移除一个 LLM 工具。"""
        cls._require_context().unregister_llm_tool(name)

    @classmethod
    def activate_llm_tool(cls, name: str) -> bool:
        """[已废弃] 启用一个 LLM 工具。"""
        return bool(cls._require_context().activate_llm_tool(name))

    @classmethod
    async def activate_llm_tool_async(cls, name: str) -> bool:
        """启用一个 LLM 工具。"""
        return bool(await cls._require_context().activate_llm_tool_async(name))

    @classmethod
    def deactivate_llm_tool(cls, name: str) -> bool:
        """[已废弃] 禁用一个 LLM 工具。"""
        return bool(cls._require_context().deactivate_llm_tool(name))

    @classmethod
    async def deactivate_llm_tool_async(cls, name: str) -> bool:
        """禁用一个 LLM 工具。"""
        return bool(await cls._require_context().deactivate_llm_tool_async(name))


def _caller_native_plugin_id() -> str | None:
    """从调用栈里找出调用方模块所属插件的原生 id。"""
    from ..star.star import star_map

    frame = inspect.currentframe()
    if frame is None or frame.f_back is None or frame.f_back.f_back is None:
        return None
    module = inspect.getmodule(frame.f_back.f_back)
    if module is None:
        return None
    module_name = getattr(module, "__name__", "")
    metadata = star_map.get(module_name)
    if metadata is None:
        for module_path, candidate in star_map.items():
            package = module_path.rpartition(".")[0]
            if module_name == module_path or (
                package and module_name.startswith(package + ".")
            ):
                metadata = candidate
                break
    if metadata is None:
        raise RuntimeError(f"Unable to resolve metadata for module {module_name}")
    return metadata.plugin_id


def _native_plugin_id(plugin_name: str) -> str | None:
    """把插件名（原生 ``author/name`` 或 star 名）解析成原生 id。"""
    from ..star.star import star_map

    for module_path, metadata in star_map.items():
        if plugin_name in (metadata.plugin_id, metadata.name, module_path):
            return metadata.plugin_id
    return None


__all__ = ["StarTools"]
