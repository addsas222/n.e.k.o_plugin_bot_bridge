"""协议适配基类（上游 ``nonebot.adapters`` 的子集）。

shim 把上游的 ``nonebot.internal.adapter`` 四个模块合并进一个文件，本模块只做透传，
保持 ``nonebot.adapters.Bot`` 这类导入路径不变。
"""

from __future__ import annotations

from nonebot.internal.adapter import Adapter as Adapter
from nonebot.internal.adapter import Bot as Bot
from nonebot.internal.adapter import Event as Event
from nonebot.internal.adapter import Message as Message
from nonebot.internal.adapter import MessageSegment as MessageSegment
from nonebot.internal.adapter import MessageTemplate as MessageTemplate

__all__ = [
    "Adapter",
    "Bot",
    "Event",
    "Message",
    "MessageSegment",
    "MessageTemplate",
]
