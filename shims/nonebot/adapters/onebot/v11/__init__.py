"""OneBot v11 协议适配（上游 ``nonebot.adapters.onebot.v11`` 的子集）。

与上游同名同义的导出：``Bot`` / ``Adapter`` / ``Event`` 家族 / ``Message`` /
``MessageSegment`` / ``ActionFailed`` 等异常 / 权限常量 / ``escape`` / ``unescape`` / ``log``。
"""

from __future__ import annotations

from .adapter import Adapter as Adapter
from .bot import Bot as Bot
from .event import *  # noqa: F403
from .exception import ActionFailed as ActionFailed
from .exception import ApiNotAvailable as ApiNotAvailable
from .exception import NetworkError as NetworkError
from .exception import NoLogException as NoLogException
from .exception import OneBotV11AdapterException as OneBotV11AdapterException
from .message import Message as Message
from .message import MessageSegment as MessageSegment
from .permission import *  # noqa: F403
from .utils import escape as escape
from .utils import log as log
from .utils import unescape as unescape
