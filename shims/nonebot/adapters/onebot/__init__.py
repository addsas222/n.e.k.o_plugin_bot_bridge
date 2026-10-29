"""OneBot 协议适配器（上游 ``nonebot.adapters.onebot`` 的子集）。

shim 只实现 v11（N.E.K.O. 桥接的场景是 OneBot v11 协议插件），因此这里只导出 v11 的
别名；上游的 ``V12*`` 别名与 ``collator`` / ``store`` 不提供。
"""

from __future__ import annotations

from .v11 import Adapter as V11Adapter
from .v11 import Bot as V11Bot
from .v11 import Message as V11Message
from .v11 import MessageSegment as V11MessageSegment
from .v11 import event as V11Event

__all__ = [
    "V11Adapter",
    "V11Bot",
    "V11Event",
    "V11Message",
    "V11MessageSegment",
]
