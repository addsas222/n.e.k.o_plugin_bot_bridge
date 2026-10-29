"""OneBot v12 事件模型（``nonebot.adapters.onebot.v12.event`` 的子集）。

v12 的事件结构与 v11 不同，是**扁平 + detail_type**：

    {"type": "message", "detail_type": "group", "group_id": "...", ...}

字段名、``get_session_id()`` 的拼法、``is_tome()`` 的来源都按 v12 语义来：
``to_me`` 由平台在 ``self``/事件里给出，**不靠 @ 与昵称推断**（那是 v11 的规则）。
"""

from __future__ import annotations

from typing import Any

from nonebot.internal.adapter import Event as BaseEvent

from .message import Message

__all__ = [
    "ChannelMessageEvent",
    "Event",
    "GroupMessageEvent",
    "MessageEvent",
    "MetaEvent",
    "NoticeEvent",
    "PrivateMessageEvent",
    "RequestEvent",
    "build_event",
]


def _as_message(value: Any) -> Message:
    if isinstance(value, Message):
        return value
    return Message(value)


class _Payload:
    """宽容的属性容器：缺字段给默认值，多字段原样保留（与 v11 shim 的 ``_Payload`` 同构）。"""

    _defaults: dict[str, Any] = {}

    def __init__(self, **data: Any) -> None:
        for key, default in type(self)._defaults.items():
            setattr(self, key, data.pop(key, default))
        for key, value in data.items():
            setattr(self, key, value)

    def dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    def __repr__(self) -> str:
        fields = ", ".join(f"{k}={v!r}" for k, v in self.__dict__.items())
        return f"{type(self).__name__}({fields})"


class Event(_Payload, BaseEvent):
    """所有 v12 事件的基类。"""

    _defaults = {"type": "", "time": 0.0, "self_id": ""}

    def get_type(self) -> str:
        return str(getattr(self, "type", "") or "")

    def get_event_name(self) -> str:
        return type(self).__name__

    def get_event_description(self) -> str:
        return f"{self.get_event_name()} from {getattr(self, 'self_id', '')}"

    def get_user_id(self) -> str:
        return str(getattr(self, "user_id", "") or "")

    def get_session_id(self) -> str:
        return f"{self.get_type()}_{self.get_user_id()}"

    def get_message(self) -> Message:
        return _as_message(getattr(self, "message", None))

    def is_tome(self) -> bool:
        return bool(getattr(self, "to_me", False))


class MessageEvent(Event):
    """消息事件（``type == "message"``）。"""

    _defaults = {
        "type": "message",
        "time": 0.0,
        "self_id": "",
        "message_id": "",
        "message": None,
        "original_message": None,
        "alt_message": "",
        "user_id": "",
        "detail_type": "private",
        "sub_type": "",
        "to_me": False,
    }

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        if not isinstance(self.message, Message):
            self.message = _as_message(self.message)
        if self.original_message is None:
            self.original_message = self.message
        elif not isinstance(self.original_message, Message):
            self.original_message = _as_message(self.original_message)
        if not self.alt_message:
            self.alt_message = self.message.extract_plain_text()

    def get_type(self) -> str:
        return "message"

    def get_session_id(self) -> str:
        detail = str(getattr(self, "detail_type", "") or "private")
        if detail == "group":
            return f"group_{getattr(self, 'group_id', '')}_{self.get_user_id()}"
        if detail == "channel":
            return f"channel_{getattr(self, 'channel_id', '')}_{self.get_user_id()}"
        return f"private_{self.get_user_id()}"

    def get_event_description(self) -> str:
        return f"Message {self.message_id!r} from {self.get_user_id()}"[:200]


class PrivateMessageEvent(MessageEvent):
    """私聊消息。"""

    _defaults = {**MessageEvent._defaults, "detail_type": "private"}


class GroupMessageEvent(MessageEvent):
    """群消息。"""

    _defaults = {**MessageEvent._defaults, "detail_type": "group", "group_id": ""}


class ChannelMessageEvent(MessageEvent):
    """频道消息。"""

    _defaults = {
        **MessageEvent._defaults,
        "detail_type": "channel",
        "guild_id": "",
        "channel_id": "",
    }


class NoticeEvent(Event):
    """通知事件。"""

    _defaults = {**Event._defaults, "type": "notice", "detail_type": "", "user_id": "", "group_id": ""}

    def get_type(self) -> str:
        return "notice"


class RequestEvent(Event):
    """请求事件。"""

    _defaults = {**Event._defaults, "type": "request", "detail_type": "", "user_id": "", "group_id": ""}

    def get_type(self) -> str:
        return "request"


class MetaEvent(Event):
    """元事件。"""

    _defaults = {**Event._defaults, "type": "meta_event", "detail_type": ""}

    def get_type(self) -> str:
        return "meta_event"


_EVENT_CLASSES: dict[str, type[Event]] = {
    "message": MessageEvent,
    "message.private": PrivateMessageEvent,
    "message.group": GroupMessageEvent,
    "message.channel": ChannelMessageEvent,
    "notice": NoticeEvent,
    "request": RequestEvent,
    "meta_event": MetaEvent,
}


def build_event(payload: dict[str, Any], event_type: str = "") -> Event:
    """从 v12 payload 构造事件（shim 扩展：桥接层 ``dispatch_event`` 用）。

    选择最具体的类：先看 ``type``，再看 ``detail_type``。
    """
    data = dict(payload)
    kind = str(data.get("type") or event_type or "").split("@", 1)[0]
    if not kind:
        raise ValueError("payload has no type and no event_type was given")
    data["type"] = kind

    key = kind
    if kind == "message":
        key = f"message.{data.get('detail_type') or 'private'}"
    event_cls = _EVENT_CLASSES.get(key) or _EVENT_CLASSES.get(kind)
    if event_cls is None:
        raise ValueError(f"unsupported onebot v12 event type: {kind!r}")
    return event_cls(**data)
