"""上游 ``hoshino/msghandler.py`` 的对应物。

上游在这里把触发器链挂到 ``nonebot.message_preprocessor`` 上（``handle_message(bot, event, _)``）；
shim 里分发由兼容层直接驱动（见 :mod:`hoshino.msgdispatch`），所以这个模块只是把那套入口
按上游名字暴露出来，方便插件或调试脚本按老名字调用。
"""

from __future__ import annotations

from hoshino.msgdispatch import dispatch as dispatch
from hoshino.msgdispatch import handle_event as handle_event
from hoshino.msgdispatch import handle_message as handle_message

__all__ = ["dispatch", "handle_event", "handle_message"]
