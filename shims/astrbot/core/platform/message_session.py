"""AstrBot 会话标识（N.E.K.O. shim 子集）。

``unified_msg_origin`` 的格式与上游一致：``platform_id:message_type:session_id``。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .message_type import MessageType


@dataclass
class MessageSession:
    """描述一条消息在 AstrBot 中对应的会话的唯一标识。

    实例化时不要给 ``platform_id`` 赋值，``__post_init__`` 会把它设为 ``platform_name``。
    """

    platform_name: str
    message_type: MessageType
    session_id: str
    platform_id: str = field(init=False)

    def __post_init__(self) -> None:
        self.platform_id = self.platform_name

    def __str__(self) -> str:
        return f"{self.platform_id}:{self.message_type.value}:{self.session_id}"

    @staticmethod
    def from_str(session_str: str) -> MessageSession:
        """从 ``platform_id:message_type:session_id`` 解析会话。"""
        platform_id, message_type, session_id = session_str.split(":", 2)
        return MessageSession(platform_id, MessageType(message_type), session_id)


# 上游为兼容历史拼写错误保留的别名
MessageSesion = MessageSession

__all__ = ["MessageSesion", "MessageSession"]
