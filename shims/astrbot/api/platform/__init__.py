"""``astrbot.api.platform`` —— 平台适配器侧的聚合入口（上游同名模块）。

上游把消息组件、平台事件模型与 ``register_platform_adapter`` 摊平在这里，供
「写平台适配器」的插件使用。shim 能兑现的部分：消息组件、事件/会话模型、
``register_platform_adapter``（注册动作会被 ``Context`` 记录，但 N.E.K.O. 不加载
第三方平台适配器 —— 真被调用时返回结构化失败，不静默吞掉）。
"""

from __future__ import annotations

from astrbot.core.message.components import *  # noqa: F403
from astrbot.core.platform import (
    AstrBotMessage,
    AstrMessageEvent,
    MessageMember,
    MessageSession,
    MessageType,
    PlatformMetadata,
)
from astrbot.core.platform.register import register_platform_adapter

__all__ = [
    "AstrBotMessage",
    "AstrMessageEvent",
    "MessageMember",
    "MessageSession",
    "MessageType",
    "PlatformMetadata",
    "register_platform_adapter",
]
