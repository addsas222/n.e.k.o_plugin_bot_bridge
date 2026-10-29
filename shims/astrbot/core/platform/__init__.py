"""AstrBot 平台抽象（N.E.K.O. shim 子集）。

只保留插件侧拿得到的类型；上游的 ``Platform`` / ``register_platform_adapter`` /
webhook server 属于适配器开发 API，本 shim 不提供（见后端模块 docstring 的差异表）。
"""

from __future__ import annotations

from .astr_message_event import AstrMessageEvent
from .astrbot_message import AstrBotMessage, Group, MessageMember
from .message_session import MessageSesion, MessageSession
from .message_type import MessageType
from .platform_metadata import PlatformMetadata

__all__ = [
    "AstrBotMessage",
    "AstrMessageEvent",
    "Group",
    "MessageMember",
    "MessageSesion",
    "MessageSession",
    "MessageType",
    "PlatformMetadata",
]
