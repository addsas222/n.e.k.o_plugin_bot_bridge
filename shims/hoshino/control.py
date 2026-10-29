"""``hoshino.control`` 的兼容实现：分发控制流异常（上游来自 nonebot v1）。

这里只是把 ``aiocqhttp.control`` 里那批异常重新导出（``hoshino`` / ``hoshino.typing``
也从同一处导出），保证插件无论从哪个模块 import 拿到的都是同一个类。
"""

from __future__ import annotations

from aiocqhttp.control import (
    CanceledException,
    FinishedException,
    PauseException,
    SwitchException,
    get_cancelled_message,
)

#: 上游私有名（插件里写过 ``except _FinishException`` / ``except _PauseException``）。
_FinishException = FinishedException
_PauseException = PauseException

__all__ = [
    "CanceledException",
    "FinishedException",
    "PauseException",
    "SwitchException",
    "_FinishException",
    "_PauseException",
    "get_cancelled_message",
]
