"""分发控制流异常（上游来自 nonebot v1 的 ``nonebot.message`` / ``nonebot.command``）。

Hoshino 插件与框架胶水靠这几类异常表达「这条消息别再往下走了」：

* ``CanceledException`` —— ``bot.finish()``、``hoshino.msghandler`` 用它表示"已经处理完了"；
* ``SwitchException`` —— 这个 handler 不接，让下一个继续（Hoshino 的 fullmatch 触发器用它）；
* ``FinishedException`` / ``PauseException`` —— 上游 ``_FinishException`` / ``_PauseException``，
  命令会话里表达"结束" / "等待下一条消息"。

它们定义在 aiocqhttp shim 里（而不是 ``hoshino`` 包）是因为 ``bot.finish`` 与应用层的
胶水都要抛它们，放在最底层可以避免两个 shim 互相 import。``hoshino.control`` /
``hoshino.typing`` / ``hoshino`` 都重新导出同一批对象，插件怎么写都能拿到同一个类。
"""

from __future__ import annotations

__all__ = [
    "CanceledException",
    "FinishedException",
    "PauseException",
    "SwitchException",
    "get_cancelled_message",
]


class CanceledException(Exception):
    """当前事件处理被取消（分发应该停下，消息算已处理）。"""

    def __init__(self, message: str = "event handling canceled") -> None:
        super().__init__(message)
        self.message = message


class SwitchException(Exception):
    """这个 handler 不接这条消息，交给下一个候选项。"""

    def __init__(self, message: str = "", new_message: object | None = None) -> None:
        super().__init__(message or "switch to another handler")
        self.message = message
        self.new_message = new_message


class FinishedException(Exception):
    """命令会话正常结束（上游 ``nonebot.command._FinishException``）。"""


class PauseException(Exception):
    """命令会话暂停、等待下一条消息（上游 ``nonebot.command._PauseException``）。

    shim 不支持多轮会话：``CommandSession.pause()`` 与 ``get()`` 拿不到参数时都抛
    ``UnsupportedCapabilityError``，这个类只用于让插件的 ``except _PauseException`` 有意义。
    """

    def __init__(self, message: str = "") -> None:
        super().__init__(message or "session paused")
        self.message = message


_FinishException = FinishedException
"""上游私有名（插件里出现过 ``except _FinishException``）。"""

_PauseException = PauseException
"""上游私有名。"""


def get_cancelled_message(exc: CanceledException) -> str:
    """取 ``CanceledException`` 里携带的文本（上游 ``nonebot.message.get_cancelled_message``）。"""
    return getattr(exc, "message", "") or ""
