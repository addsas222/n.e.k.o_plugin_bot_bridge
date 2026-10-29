"""OneBot v11 事件模型（``aiocqhttp.Event`` 的忠实子集）。

Hoshino 插件对事件有三种访问方式，本类**同时**支持（上游的 Event 是 dict 子类，
所以这三种写法在真实 Hoshino 代码里混用）：

* 属性：``ev.user_id`` / ``ev.group_id`` / ``ev.message`` / ``ev.detail_type``；
* 下标：``ev['match']`` / ``ev['message_id']`` / ``event['to_me']``；
* 可变：触发器会写 ``ev['prefix']`` / ``ev['suffix']`` / ``ev['match']``，
  ``_TextNormalizer`` 会写 ``ev.plain_text`` / ``ev.norm_text``。

所以底层是一个普通 dict，属性访问走 ``__getattr__``，写入走 ``__setattr__``。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from .message import Message, normalize_text

__all__ = [
    "Event",
    "GroupMessageEvent",
    "MessageEvent",
    "MetaEvent",
    "NoticeEvent",
    "PrivateMessageEvent",
    "RequestEvent",
    "build_event",
]

#: OneBot v11 的四种事件大类。
POST_TYPES: tuple[str, ...] = ("message", "notice", "request", "meta_event")
#: 消息事件的子类型（``message_type`` / ``detail_type``）。
MESSAGE_DETAIL_TYPES: tuple[str, ...] = ("private", "group", "discuss")


class Event:
    """一条 OneBot 事件。"""

    #: 事件大类（子类覆盖）。
    post_type = "message"

    def __init__(self, data: Mapping[str, Any] | None = None) -> None:
        payload: dict[str, Any] = dict(data or {})
        payload.setdefault("post_type", self.post_type)
        raw_message = payload.get("raw_message")
        message = payload.get("message")
        if message is None:
            message = Message(raw_message if raw_message is not None else "")
        elif not isinstance(message, Message):
            message = Message(message)
        payload["message"] = message
        if raw_message is None:
            payload["raw_message"] = str(message)
        payload.setdefault("plain_text", "")
        payload.setdefault("to_me", False)
        payload.setdefault("self_id", 0)
        payload.setdefault("user_id", 0)
        payload.setdefault("group_id", 0)
        payload.setdefault("discuss_id", 0)
        payload.setdefault("sender", {})
        payload.setdefault("anonymous", None)
        payload.setdefault("font", 0)
        payload.setdefault("time", 0)
        object.__setattr__(self, "_data", payload)

    # ------------------------------------------------------------------ dict

    @property
    def raw_event(self) -> dict[str, Any]:
        """原始上报数据（含 shim 扩展键；``message`` 是 Message 对象）。"""
        return dict(self._data)

    def to_dict(self) -> dict[str, Any]:
        return dict(self._data)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value

    def __delitem__(self, key: str) -> None:
        del self._data[key]

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def keys(self) -> Any:
        return self._data.keys()

    def values(self) -> Any:
        return self._data.values()

    def items(self) -> Any:
        return self._data.items()

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def update(self, other: Mapping[str, Any] | None = None, **kwargs: Any) -> None:
        self._data.update(dict(other or {}), **kwargs)

    # -------------------------------------------------------------- 属性访问

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        try:
            return object.__getattribute__(self, "_data")[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        self._data[name] = value

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._data!r})"

    # ------------------------------------------------------------------ 语义

    @property
    def detail_type(self) -> str:
        """二级类型（``message_type`` / ``notice_type`` / ``request_type``）。"""
        for key in ("message_type", "notice_type", "request_type", "detail_type"):
            value = self._data.get(key)
            if value:
                return str(value)
        return ""

    @property
    def message_type(self) -> str:
        """消息事件的消息类型（非消息事件返回空串）。"""
        return str(self._data.get("message_type") or "") if self.post_type == "message" else ""

    @property
    def notice_type(self) -> str:
        return str(self._data.get("notice_type") or "")

    @property
    def request_type(self) -> str:
        return str(self._data.get("request_type") or "")

    @property
    def sub_type(self) -> str:
        return str(self._data.get("sub_type") or "")

    @property
    def is_group(self) -> bool:
        return self.post_type == "message" and self.message_type in ("group", "discuss")

    @property
    def message(self) -> Message:
        message = self._data.get("message")
        if not isinstance(message, Message):
            message = Message(message if message is not None else self._data.get("raw_message") or "")
            self._data["message"] = message
        return message

    @message.setter
    def message(self, value: Any) -> None:
        self._data["message"] = value if isinstance(value, Message) else Message(value)

    @property
    def plain_text(self) -> str:
        """去掉首尾空白的纯文本（上游由 ``_PlainTextExtractor`` 写入）。"""
        text = self._data.get("plain_text")
        if not text:
            text = self.message.extract_plain_text().strip()
            self._data["plain_text"] = text
        return str(text)

    @plain_text.setter
    def plain_text(self, value: Any) -> None:
        self._data["plain_text"] = str(value)

    @property
    def norm_text(self) -> str:
        """规范化文本（NFKC + 小写），供 ``normalize=True`` 的触发器使用。"""
        text = self._data.get("norm_text")
        if text is None:
            text = normalize_text(self.plain_text)
            self._data["norm_text"] = text
        return str(text)

    @norm_text.setter
    def norm_text(self, value: Any) -> None:
        self._data["norm_text"] = str(value)

    def reset_text_cache(self) -> None:
        """丢掉缓存的 ``plain_text`` / ``norm_text``（改过 ``message`` 之后调用）。

        触发器在运行 handler 期间会摘掉命中的前后缀，摘完/恢复时都要重算这两个值。
        """
        self._data.pop("plain_text", None)
        self._data.pop("norm_text", None)

    @property
    def to_me(self) -> bool:
        """这条消息是不是冲着机器人来的。"""
        return bool(self._data.get("to_me"))

    def is_message_of(self, *detail_types: str) -> bool:
        """``post_type`` / ``detail_type`` 是否命中给定值（触发器与 ``on_message`` 用）。"""
        if not detail_types:
            return True
        return self.detail_type in detail_types or self.post_type in detail_types

    def copy(self) -> "Event":
        """浅拷贝（``message`` 会深一层，避免触发器改写影响原事件）。"""
        payload = dict(self._data)
        message = payload.get("message")
        if isinstance(message, Message):
            payload["message"] = message.copy()
        return type(self)(payload)


class MessageEvent(Event):
    """消息事件。"""

    post_type = "message"


class NoticeEvent(Event):
    """通知事件（群成员变动、戳一戳、撤回…）。"""

    post_type = "notice"


class RequestEvent(Event):
    """请求事件（加好友、加群）。"""

    post_type = "request"


class MetaEvent(Event):
    """元事件（心跳、生命周期）。"""

    post_type = "meta_event"


#: 上游 aiocqhttp 只有 ``Event``；这两个别名只是让「群消息 / 私聊消息」的心智模型自洽。
GroupMessageEvent = MessageEvent
PrivateMessageEvent = MessageEvent

_EVENT_CLASSES: dict[str, type[Event]] = {
    "message": MessageEvent,
    "notice": NoticeEvent,
    "request": RequestEvent,
    "meta_event": MetaEvent,
}


def build_event(body: Mapping[str, Any]) -> Event:
    """按上报数据造事件；``post_type`` 不明时按里面的类型字段推断。

    推断不出来就抛 ``ValueError``（调用方 —— 兼容层 —— 会把它翻成
    ``UnsupportedCapabilityError``，与 NoneBot 兼容层的做法一致），**不猜**。
    """
    payload = dict(body)
    post_type = str(payload.get("post_type") or "").split(".", 1)[0].strip()
    if post_type not in POST_TYPES:
        if payload.get("notice_type"):
            post_type = "notice"
        elif payload.get("request_type"):
            post_type = "request"
        elif payload.get("message_type"):
            post_type = "message"
        else:
            raise ValueError(
                "cannot tell the event type: provide 'post_type' (message/notice/request/meta_event) "
                "or one of 'message_type' / 'notice_type' / 'request_type'",
            )
    if post_type == "message":
        detail = str(payload.get("message_type") or payload.get("detail_type") or "")
        if detail not in MESSAGE_DETAIL_TYPES:
            raise ValueError(f"unsupported message_type: {detail or '<missing>'}")
        payload.setdefault("message_type", detail)
    payload["post_type"] = post_type
    return _EVENT_CLASSES[post_type](payload)
