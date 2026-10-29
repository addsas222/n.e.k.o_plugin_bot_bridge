"""AstrBot 消息事件（N.E.K.O. shim 子集）。

复刻上游 ``astrbot.core.platform.astr_message_event.AstrMessageEvent`` 中插件实际
会用到的那部分：消息读取、结果构造、事件传播控制、会话标识。

与上游的差异：

* 上游是抽象类，``send()`` 由各平台适配器实现；shim 直接把它接到 N.E.K.O. 的
  ``send_chat`` 上（文本以外的消息段会记入 ``dropped`` 并写日志，不静默丢失）。
* ``send_typing`` / ``stop_typing`` 只是平台输入状态提示，N.E.K.O. 没有对应概念，
  这里记录一条宿主日志后返回 ``None``（有意的、已记录的 no-op）。
* 其余需要平台能力的方法（``react`` / ``send_streaming`` / ``get_group``）抛
  :class:`astrbot.UnsupportedCapabilityError`。
"""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any

from ..._runtime import RUNTIME, UnsupportedCapabilityError
from ..message.components import BaseMessageComponent, Plain
from ..message.message_event_result import MessageChain, MessageEventResult
from .astrbot_message import AstrBotMessage
from .message_session import MessageSession
from .message_type import MessageType
from .platform_metadata import PlatformMetadata

NekoSend = Callable[[str, str, tuple[str, ...]], Awaitable[bool]]
"""桥接层注入的立即发送回调：``(text, unified_msg_origin, dropped_components) -> bool``。"""


class AstrMessageEvent:
    """一次入站消息事件。"""

    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
    ) -> None:
        self.message_str = message_str
        """纯文本的消息内容（唤醒前缀已由桥接层剥离）。"""
        self.message_obj = message_obj
        """消息对象（带完整消息结构）。"""
        self.platform_meta = platform_meta
        """消息平台元数据。"""
        self.role = "member"
        """发送者身份：``member`` 或 ``admin``。"""
        self.is_wake = False
        """是否被唤醒。"""
        self.is_at_or_wake_command = False
        """是否是 At 机器人 / 带唤醒词 / 私聊（桥接层转发进来的消息一律为 True）。"""
        self._extras: dict[str, Any] = {}
        self._force_stopped = False
        message_type = getattr(message_obj, "type", None)
        if not isinstance(message_type, MessageType):
            try:
                message_type = MessageType(str(message_type))
            except (ValueError, TypeError, AttributeError):
                message_type = MessageType.FRIEND_MESSAGE
        self.session = MessageSession(
            platform_name=platform_meta.id,
            message_type=message_type,
            session_id=session_id,
        )
        self._result: MessageEventResult | None = None
        self.created_at = time.time()
        self._has_send_oper = False
        self.call_llm = False
        """是否禁止本次事件走默认 LLM 请求（桥接层不使用该链路）。"""
        self._temporary_local_files: list[str] = []
        self.plugins_name: list[str] | None = None
        self._neko_send: NekoSend | None = None
        # 兼容旧字段
        self.platform = platform_meta

    # -- 桥接层内部接口 ---------------------------------------------------- #

    def _neko_attach_sender(self, sender: NekoSend) -> None:
        """由兼容层注入立即发送通道。"""
        self._neko_send = sender

    # -- 会话标识 ---------------------------------------------------------- #

    @property
    def unified_msg_origin(self) -> str:
        """统一消息来源：``platform_id:message_type:session_id``。"""
        return str(self.session)

    @unified_msg_origin.setter
    def unified_msg_origin(self, value: str) -> None:
        self.session = MessageSession.from_str(value)

    @property
    def session_id(self) -> str:
        """当前会话 id。"""
        return self.session.session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self.session.session_id = value

    # -- 消息读取 ---------------------------------------------------------- #

    def get_platform_name(self) -> str:
        """平台类型名。"""
        return self.platform_meta.name

    def get_platform_id(self) -> str:
        """平台实例 id。"""
        return self.platform_meta.id

    def get_message_str(self) -> str:
        """纯文本消息。"""
        return self.message_str

    def _outline_chain(self, chain: list[BaseMessageComponent] | None) -> str:
        if not chain:
            return ""
        parts: list[str] = []
        for comp in chain:
            if isinstance(comp, Plain):
                parts.append(comp.text)
            elif comp.type.value == "at":
                parts.append(f"@{comp.qq}")
            elif comp.type.value == "image":
                parts.append("[图片]")
            else:
                parts.append(f"[{comp.type.value}]")
        return "".join(parts)

    def get_message_outline(self) -> str:
        """消息摘要（文本 + 类型占位）。"""
        return self._outline_chain(self.get_messages())

    def get_messages(self) -> list[BaseMessageComponent]:
        """消息链。"""
        return self.message_obj.message

    def get_message_type(self) -> MessageType:
        """消息类型。"""
        message_type = getattr(self.message_obj, "type", None)
        if isinstance(message_type, MessageType):
            return message_type
        return self.session.message_type

    def get_session_id(self) -> str:
        """会话 id。"""
        return self.session_id

    def get_group_id(self) -> str:
        """群号；私聊为空字符串。"""
        return getattr(self.message_obj, "group_id", "") or ""

    def get_self_id(self) -> str:
        """机器人自身 id。"""
        return getattr(self.message_obj, "self_id", "")

    def get_sender_id(self) -> str:
        """发送者 id。"""
        sender = getattr(self.message_obj, "sender", None)
        if sender and isinstance(getattr(sender, "user_id", None), str):
            return sender.user_id
        return ""

    def get_sender_name(self) -> str:
        """发送者昵称（可能为空字符串）。"""
        sender = getattr(self.message_obj, "sender", None)
        if not sender:
            return ""
        nickname = getattr(sender, "nickname", None)
        if nickname is None:
            return ""
        return nickname if isinstance(nickname, str) else str(nickname)

    # -- 附加信息 ---------------------------------------------------------- #

    def set_extra(self, key: str, value: Any) -> None:
        """写入事件级附加信息。"""
        self._extras[key] = value

    def get_extra(self, key: str | None = None, default: Any = None) -> Any:
        """读取事件级附加信息。"""
        if key is None:
            return self._extras
        return self._extras.get(key, default)

    def clear_extra(self) -> None:
        """清空事件级附加信息。"""
        self._extras.clear()

    def track_temporary_local_file(self, path: str) -> None:
        """登记一个随事件销毁的临时文件。"""
        if path not in self._temporary_local_files:
            self._temporary_local_files.append(path)

    def untrack_temporary_local_file(self, path: str) -> None:
        """取消登记临时文件。"""
        if path in self._temporary_local_files:
            self._temporary_local_files.remove(path)

    def cleanup_temporary_local_files(self) -> None:
        """删除所有登记的临时文件。"""
        import os

        for path in list(self._temporary_local_files):
            try:
                os.remove(path)
            except OSError as exc:
                RUNTIME.log(
                    "debug",
                    f"failed to remove temporary local file {path}: {exc}",
                    source="astrbot.event.cleanup",
                )
            finally:
                self.untrack_temporary_local_file(path)

    # -- 身份 / 状态 ------------------------------------------------------- #

    def is_private_chat(self) -> bool:
        """是否私聊。"""
        return self.get_message_type() == MessageType.FRIEND_MESSAGE

    def is_wake_up(self) -> bool:
        """是否被唤醒。"""
        return self.is_wake

    def is_admin(self) -> bool:
        """发送者是否管理员。"""
        return self.role == "admin"

    def should_call_llm(self, call_llm: bool) -> None:
        """设置是否允许默认 LLM 请求链路。"""
        self.call_llm = call_llm

    # -- 结果与传播 -------------------------------------------------------- #

    def set_result(self, result: MessageEventResult | str) -> None:
        """设置事件结果。"""
        if isinstance(result, str):
            result = MessageEventResult().message(result)
        if isinstance(result, MessageEventResult) and result.chain is None:
            result.chain = []
        self._result = result

    def stop_event(self) -> None:
        """终止事件传播。"""
        self._force_stopped = True
        if self._result is None:
            self.set_result(MessageEventResult().stop_event())
        else:
            self._result.stop_event()

    def continue_event(self) -> None:
        """继续事件传播。"""
        self._force_stopped = False
        if self._result is None:
            self.set_result(MessageEventResult().continue_event())
        else:
            self._result.continue_event()

    def is_stopped(self) -> bool:
        """是否终止事件传播。"""
        if self._force_stopped:
            return True
        if self._result is None:
            return False
        return self._result.is_stopped()

    def get_result(self) -> MessageEventResult | None:
        """当前事件结果。"""
        return self._result

    def clear_result(self) -> None:
        """清除事件结果。"""
        self._result = None

    def make_result(self) -> MessageEventResult:
        """创建一个空的消息事件结果。"""
        return MessageEventResult()

    def plain_result(self, text: str) -> MessageEventResult:
        """创建只含一段文本的消息事件结果。"""
        return MessageEventResult().message(text)

    def image_result(self, url_or_path: str) -> MessageEventResult:
        """创建只含一张图片的消息事件结果。"""
        if url_or_path.startswith("http"):
            return MessageEventResult().url_image(url_or_path)
        return MessageEventResult().file_image(url_or_path)

    def chain_result(self, chain: list[BaseMessageComponent]) -> MessageEventResult:
        """创建含指定消息链的消息事件结果。"""
        result = MessageEventResult()
        result.chain = list(chain)
        return result

    # -- LLM 请求对象 ------------------------------------------------------ #

    def request_llm(
        self,
        prompt: str = "",
        func_tool_manager: Any = None,
        tool_set: Any = None,
        session_id: str = "",
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        contexts: list[Any] | None = None,
        system_prompt: str = "",
        conversation: Any = None,
    ) -> Any:
        """构造一个 ``ProviderRequest``（真正发起请求用 ``context.llm_generate``）。"""
        from ..provider import ProviderRequest

        request = ProviderRequest(
            prompt=prompt or None,
            session_id=session_id or self.session_id,
            image_urls=list(image_urls or []),
            audio_urls=list(audio_urls or []),
            contexts=list(contexts or []),
            system_prompt=system_prompt or "",
            conversation=conversation,
        )
        request.func_tool = tool_set if tool_set is not None else func_tool_manager
        return request

    # -- 发送 -------------------------------------------------------------- #

    async def _pre_send(self) -> None:
        """发送前钩子（上游可覆写）。"""

    async def _post_send(self) -> None:
        """发送后钩子（上游可覆写）。"""

    async def send(self, message: MessageChain) -> None:
        """立即把消息发进 N.E.K.O. 对话。

        Raises:
            UnsupportedCapabilityError: 事件未绑定发送通道时（不应该发生）。
            RuntimeError: 宿主拒绝接收这条消息时。
        """
        if self._neko_send is None:
            raise UnsupportedCapabilityError(
                "event.send",
                "该事件由兼容层构造时未绑定 N.E.K.O. 发送通道",
            )
        await self._pre_send()
        chain = list(message.chain or [])
        text = message.get_plain_text()
        dropped = tuple(type(comp).__name__ for comp in chain if not isinstance(comp, Plain))
        if not text and not dropped:
            return
        accepted = await self._neko_send(text, self.unified_msg_origin, dropped)
        self._has_send_oper = True
        await self._post_send()
        if not accepted:
            raise RuntimeError(
                f"N.E.K.O. declined the message sent by AstrBot event {self.unified_msg_origin}",
            )

    async def send_streaming(
        self,
        generator: AsyncGenerator[MessageChain, None],
        use_fallback: bool = False,
    ) -> None:
        raise UnsupportedCapabilityError(
            "event.send_streaming",
            "N.E.K.O. 的 send_chat 是整段文本，没有流式输出通道",
        )

    async def send_typing(self) -> None:
        """发出"正在输入"状态。

        N.E.K.O. 没有这个展示位，这里按设计只记一条宿主日志（不假装向平台发送成功）。
        """
        RUNTIME.log(
            "debug",
            f"send_typing() is a documented no-op for N.E.K.O. (event {self.unified_msg_origin})",
            source="astrbot.event",
        )

    async def stop_typing(self) -> None:
        """撤销"正在输入"状态（同上，no-op）。"""
        RUNTIME.log(
            "debug",
            f"stop_typing() is a documented no-op for N.E.K.O. (event {self.unified_msg_origin})",
            source="astrbot.event",
        )

    async def process_buffer(self, buffer: str, pattern: re.Pattern[str]) -> str:
        """按正则切分缓冲区并逐段发送（不支持流式平台的降级路径）。"""
        while True:
            match = re.search(pattern, buffer)
            if not match:
                break
            matched_text = match.group().strip()
            if matched_text:
                await self.send(MessageChain([Plain(matched_text)]))
                await asyncio.sleep(1.5)
            buffer = buffer[match.end():]
        return buffer

    async def react(self, emoji: str) -> None:
        raise UnsupportedCapabilityError(
            "event.react",
            f"N.E.K.O. 没有表情回应接口（emoji={emoji!r}）",
        )

    async def get_group(self, group_id: str | None = None, **kwargs: Any) -> None:
        raise UnsupportedCapabilityError(
            "event.get_group",
            "N.E.K.O. 不提供群资料查询",
        )


__all__ = ["AstrMessageEvent", "NekoSend"]
