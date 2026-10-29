"""``hoshino.typing`` 的兼容实现。

上游这个模块就是"把 nonebot v1 与 aiocqhttp 的类型重新导出 + ``from typing import *``"，
插件因此能写 ``from hoshino.typing import CQEvent, Message, MessageSegment, List, Optional``。
shim 复刻这个形状：类型来自 :mod:`typing` 与 :mod:`aiocqhttp`，会话类型来自
:mod:`hoshino.session`，``HoshinoBot`` 来自 :mod:`hoshino.bot`（同一个类对象）。
"""

from __future__ import annotations

from typing import *  # noqa: F401,F403 — 上游就是 `from typing import *`，插件依赖这一点
from typing import TYPE_CHECKING  # noqa: F401 — 插件里常见的 `if TYPE_CHECKING`

from aiocqhttp import Bot as Bot
from aiocqhttp import CQHttp as CQHttp
from aiocqhttp import CQHttpError as CQHttpError
from aiocqhttp import Message as Message
from aiocqhttp import MessageSegment as MessageSegment
from aiocqhttp import UnsupportedCapabilityError as UnsupportedCapabilityError
from aiocqhttp import build_event as build_event
from aiocqhttp.bot import CQHttp as _CQHttpClass
from aiocqhttp.control import CanceledException as CanceledException
from aiocqhttp.control import FinishedException as FinishedException
from aiocqhttp.control import PauseException as PauseException
from aiocqhttp.control import SwitchException as SwitchException
from aiocqhttp.event import Event as Event
from aiocqhttp.event import MessageEvent as MessageEvent
from aiocqhttp.event import MetaEvent as MetaEvent
from aiocqhttp.event import NoticeEvent as NoticeEvent
from aiocqhttp.event import RequestEvent as RequestEvent

from hoshino.bot import HoshinoBot as HoshinoBot
from hoshino.session import CommandSession as CommandSession
from hoshino.session import NoticeSession as NoticeSession
from hoshino.session import RequestSession as RequestSession
from hoshino.session import T_State as T_State

#: 上游最常用的名字：事件就是 aiocqhttp 的 ``Event``。
CQEvent = Event
#: 群消息 / 私聊消息（shim 不细分事件类，统一用 ``MessageEvent``；``message_type`` 区分）。
GroupMessageEvent = MessageEvent
PrivateMessageEvent = MessageEvent
#: 通知 / 请求事件的别名（同样按 ``detail_type`` 区分）。
GroupNoticeEvent = NoticeEvent
GroupRequestEvent = RequestEvent
FriendRequestEvent = RequestEvent
#: 上游 ``Bot`` 就是 aiocqhttp 的 ``CQHttp``；显式绑定一下，保证类型检查器看得见。
Bot = _CQHttpClass

__all__ = sorted(
    {
        # ``from typing import *`` 带进来的名字（List / Optional / Union …）也是上游公开面的一部分。
        *(name for name in dir() if not name.startswith("_")),
        "Bot",
        "CQEvent",
        "CQHttp",
        "CQHttpError",
        "CanceledException",
        "CommandSession",
        "Event",
        "FinishedException",
        "FriendRequestEvent",
        "GroupMessageEvent",
        "GroupNoticeEvent",
        "GroupRequestEvent",
        "HoshinoBot",
        "Message",
        "MessageEvent",
        "MessageSegment",
        "MetaEvent",
        "NoticeEvent",
        "NoticeSession",
        "PauseException",
        "PrivateMessageEvent",
        "RequestEvent",
        "RequestSession",
        "SwitchException",
        "T_State",
        "UnsupportedCapabilityError",
        "build_event",
    },
)
