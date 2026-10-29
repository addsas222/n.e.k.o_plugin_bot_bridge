"""N.E.K.O. bot_bridge 的 ``aiocqhttp`` 兼容 shim（只依赖标准库）。

HoshinoBot（Ice9Coffee/HoshinoBot）跑在 **nonebot v1 + aiocqhttp** 上，插件里到处是
``from aiocqhttp import CQHttp, Event`` / ``from aiocqhttp.message import Message`` /
``from aiocqhttp.exceptions import CQHttpError``。这个包提供同一批名字的忠实子集，
让这些插件在 N.E.K.O. 里能 import 得动、跑得起来。

它**不是**上游 aiocqhttp：没有 websocket、没有 OneBot 连接。事件由 N.E.K.O. 的对话
提供，发消息落到 N.E.K.O. 对话（分发期间进 ``CommandResult.replies``），没有等价实现的
动作抛 :class:`~aiocqhttp.errors.UnsupportedCapabilityError`。

覆盖范围：``CQHttp``（``send`` / ``send_msg`` / ``send_group_msg`` / ``send_private_msg`` /
``finish`` / ``get_self_ids``）、``Event`` 家族 + ``build_event``、``Message`` /
``MessageSegment``（CQ 码、``extract_plain_text``、``at`` / ``image`` / ``text``…）、
``escape`` / ``unescape``、``CQHttpError`` / ``ActionFailed`` / ``ApiNotAvailable``、
分发控制流异常（``CanceledException`` 等）。

被兼容层放进 ``sys.modules`` 之前会确认没有别人占用 ``aiocqhttp`` 这个名字
（看模块上的 ``__neko_bridge_shim__`` 标记）。
"""

from __future__ import annotations

from . import errors as errors
from . import exceptions as exceptions
from . import message as message
from ._runtime import RUNTIME as RUNTIME
from .bot import CQHttp as CQHttp
from .control import CanceledException as CanceledException
from .control import FinishedException as FinishedException
from .control import PauseException as PauseException
from .control import SwitchException as SwitchException
from .errors import UnsupportedCapabilityError as UnsupportedCapabilityError
from .event import Event as Event
from .event import GroupMessageEvent as GroupMessageEvent
from .event import MessageEvent as MessageEvent
from .event import MetaEvent as MetaEvent
from .event import NoticeEvent as NoticeEvent
from .event import PrivateMessageEvent as PrivateMessageEvent
from .event import RequestEvent as RequestEvent
from .event import build_event as build_event
from .exceptions import ActionFailed as ActionFailed
from .exceptions import ApiError as ApiError
from .exceptions import ApiNotAvailable as ApiNotAvailable
from .exceptions import CQHttpError as CQHttpError
from .message import Message as Message
from .message import MessageSegment as MessageSegment
from .message import escape as escape
from .message import unescape as unescape

__neko_bridge_shim__ = True
"""标记：这是 N.E.K.O. bot_bridge 的 aiocqhttp 兼容 shim，不是上游 aiocqhttp。"""

_NEKO_BRIDGE_SHIM = True
"""与其它兼容层（astrbot）一致的旧标记名。"""

__version__ = "0.0.0+neko-bridge"
"""上游版本号占位（shim 不打上游版本号，避免插件误判版本）。"""

_NEKO_SHIM_UPSTREAM = "https://github.com/nonebot/aiocqhttp"

Bot = CQHttp
"""上游 NoneBot 生态里 ``Bot`` 就是 aiocqhttp 的 ``CQHttp``。"""

__all__ = [
    "ActionFailed",
    "ApiError",
    "ApiNotAvailable",
    "Bot",
    "CQHttp",
    "CQHttpError",
    "CanceledException",
    "Event",
    "FinishedException",
    "GroupMessageEvent",
    "Message",
    "MessageEvent",
    "MessageSegment",
    "MetaEvent",
    "NoticeEvent",
    "PauseException",
    "PrivateMessageEvent",
    "RUNTIME",
    "RequestEvent",
    "SwitchException",
    "UnsupportedCapabilityError",
    "build_event",
    "errors",
    "escape",
    "exceptions",
    "message",
    "unescape",
]
