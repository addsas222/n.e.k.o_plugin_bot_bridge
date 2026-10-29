"""OneBot v12 协议适配（上游 ``nonebot.adapters.onebot.v12`` 的子集）。

与 v11 **并存**：桥接层在加载插件时判断它要哪个适配器（看
``__plugin_meta__.supported_adapters`` 与模块里实际 import 的类），然后合成对应
形状的事件与 Bot —— v11 插件不受影响，v12 插件拿到的是 v12 事件。

与上游的差异（如实）：

* 没有协议端：除发消息外的 API 一律 :class:`ApiNotAvailable`；
* 不支持消息段：``markdown`` / ``node`` / ``forward`` 等只做数据承载，渲染成
  ``[type]``；桥接层投递给 N.E.K.O. 的是纯文本；
* ``detail_type == "channel"`` 只是数据结构，N.E.K.O. 侧没有频道概念，
  ``get_session_id()`` 仍按 v12 规则拼。
"""

from __future__ import annotations

from .adapter import Adapter as Adapter
from .bot import Bot as Bot
from .event import *  # noqa: F403
from .exception import ActionFailed as ActionFailed
from .exception import ApiNotAvailable as ApiNotAvailable
from .exception import NetworkError as NetworkError
from .exception import NoLogException as NoLogException
from .exception import OneBotV12AdapterException as OneBotV12AdapterException
from .message import Message as Message
from .message import MessageSegment as MessageSegment
