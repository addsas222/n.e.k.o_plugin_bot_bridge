"""OneBot v11 事件模型（上游 ``onebot.v11.event`` 的子集）。

覆盖 ``Event`` / ``MessageEvent`` / ``PrivateMessageEvent`` / ``GroupMessageEvent`` /
``NoticeEvent`` 家族 / ``RequestEvent`` 家族 / ``MetaEvent`` 家族，字段名与语义与上游一致。

偏差：

* 上游是 pydantic 模型（必填字段缺失会抛 ``ValidationError``）；shim 是普通类，缺失字段取默认值。
* ``extra="allow"`` 的行为保留：payload 里多余的键会原样变成事件属性。
* ``user_id`` / ``group_id`` 等 ID 字段在 shim 里保持原始类型（数字字符串转 int，
  非数字 ID 保留 str）——N.E.K.O. 的用户 ID 不一定可转成 int，强转会丢信息。
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any, ClassVar

from nonebot.adapters import Event as BaseEvent
from nonebot.utils import escape_tag

from .exception import NoLogException
from .message import Message, ensure_message

__all__ = [
    "Anonymous",
    "Event",
    "File",
    "FriendAddNoticeEvent",
    "FriendRecallNoticeEvent",
    "FriendRequestEvent",
    "GroupAdminNoticeEvent",
    "GroupBanNoticeEvent",
    "GroupDecreaseNoticeEvent",
    "GroupIncreaseNoticeEvent",
    "GroupMessageEvent",
    "GroupRecallNoticeEvent",
    "GroupRequestEvent",
    "GroupUploadNoticeEvent",
    "HeartbeatMetaEvent",
    "HonorNotifyEvent",
    "LifecycleMetaEvent",
    "LuckyKingNotifyEvent",
    "MessageEvent",
    "MetaEvent",
    "NoticeEvent",
    "NotifyEvent",
    "PokeNotifyEvent",
    "PrivateMessageEvent",
    "Reply",
    "RequestEvent",
    "Sender",
    "Status",
    # shim 扩展：桥接层构建事件后调用，等价于上游 Adapter 的事件预处理管线
    "build_event",
    "check_at_me",
    "check_nickname",
]


def _coerce_id(value: Any, default: Any = 0) -> Any:
    """保留 ID 语义：能转 int 就转 int，否则保留原字符串。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, int):
        return value
    text = str(value)
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce(value: Any, type_: type[Any] | None) -> Any:
    if value is None or type_ is None:
        return value
    if isinstance(value, type_):
        return value
    if isinstance(value, dict):
        try:
            return type_(**value)
        except TypeError:
            return value
    return value


class _Payload:
    """``extra="allow"`` 的轻量替身：声明字段取默认值，其余键直接变成属性。"""

    _fields: ClassVar[dict[str, Any]] = {}

    def __init__(self, **data: Any) -> None:
        for key, default in self._all_fields().items():
            setattr(self, key, data.pop(key, default))
        for key, value in data.items():
            setattr(self, key, value)

    @classmethod
    def _all_fields(cls) -> dict[str, Any]:
        merged: dict[str, Any] = {}
        for klass in reversed(cls.__mro__):
            merged.update(getattr(klass, "_fields", {}) or {})
        return merged

    def model_dump(self) -> dict[str, Any]:
        return {key: value for key, value in vars(self).items() if not key.startswith("_")}

    def dict(self) -> dict[str, Any]:
        return self.model_dump()

    def __repr__(self) -> str:
        inner = ", ".join(f"{key}={value!r}" for key, value in self.model_dump().items())
        return f"{self.__class__.__name__}({inner})"


class Sender(_Payload):
    """消息发送者信息。"""

    _fields = {
        "user_id": None,
        "nickname": None,
        "sex": None,
        "age": None,
        "card": None,
        "area": None,
        "level": None,
        "role": None,
        "title": None,
    }


class Anonymous(_Payload):
    """匿名用户信息。"""

    _fields = {"id": 0, "name": "", "flag": ""}


class File(_Payload):
    """文件信息。"""

    _fields = {"id": "", "name": "", "size": 0, "busid": 0}


class Status(_Payload):
    """运行状态。"""

    _fields = {"online": False, "good": False}


class Reply(_Payload):
    """被回复的消息。"""

    _fields = {"time": 0, "message_type": "", "message_id": 0, "real_id": 0, "sender": None, "message": None}

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.sender = _coerce(self.sender, Sender)
        if self.message is not None:
            self.message = ensure_message(self.message)


class Event(_Payload, BaseEvent):
    """OneBot v11 协议事件，字段与 OneBot 一致。"""

    _fields: ClassVar[dict[str, Any]] = {"time": 0, "self_id": 0, "post_type": ""}

    def __init__(self, **data: Any) -> None:
        for key, default in self._all_fields().items():
            setattr(self, key, data.pop(key, default))
        self.time = _coerce_int(self.time)
        self.self_id = _coerce_id(self.self_id, 0)
        self.post_type = str(self.post_type or "")
        for key, value in data.items():
            setattr(self, key, value)

    def model_dump(self) -> dict[str, Any]:
        """字段导出（上游由 pydantic 提供）。"""
        result: dict[str, Any] = {}
        for key, value in vars(self).items():
            if key.startswith("_"):
                continue
            result[key] = value.model_dump() if isinstance(value, _Payload) else value
        return result

    def get_type(self) -> str:
        return self.post_type

    def get_event_name(self) -> str:
        return self.post_type

    def get_event_description(self) -> str:
        return escape_tag(str(self.model_dump()))

    def get_message(self) -> Message:
        raise ValueError("Event has no message!")

    def get_user_id(self) -> str:
        raise ValueError("Event has no context!")

    def get_session_id(self) -> str:
        raise ValueError("Event has no context!")

    def is_tome(self) -> bool:
        return False


class MessageEvent(Event):
    """消息事件。"""

    _fields = {
        "post_type": "message",
        "sub_type": "",
        "user_id": 0,
        "message_type": "",
        "message_id": 0,
        "raw_message": "",
        "font": 0,
        "sender": None,
        "to_me": False,
        "reply": None,
    }

    def __init__(self, **data: Any) -> None:
        message = data.pop("message", "")
        original = data.pop("original_message", None)
        raw_message = data.pop("raw_message", None)
        super().__init__(**data)
        self.user_id = _coerce_id(self.user_id, 0)
        self.sender = _coerce(self.sender, Sender)
        self.message = ensure_message(message)
        self.original_message = ensure_message(original) if original is not None else deepcopy(self.message)
        self.raw_message = str(raw_message) if raw_message is not None else str(self.message)
        self.reply = _coerce(self.reply, Reply)

    def get_event_name(self) -> str:
        sub_type = getattr(self, "sub_type", None)
        name = f"{self.post_type}.{self.message_type}"
        return f"{name}.{sub_type}" if sub_type else name

    def get_message(self) -> Message:
        return self.message

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_session_id(self) -> str:
        return str(self.user_id)

    def is_tome(self) -> bool:
        return bool(self.to_me)


class PrivateMessageEvent(MessageEvent):
    """私聊消息。"""

    _fields = {"message_type": "private", "sub_type": "friend"}

    def get_event_description(self) -> str:
        return (
            f"Message {self.message_id} from {self.user_id} "
            f"{self.original_message.to_rich_text()!r}"
        )


class GroupMessageEvent(MessageEvent):
    """群消息。"""

    _fields = {"message_type": "group", "sub_type": "normal", "group_id": 0, "anonymous": None}

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.group_id = _coerce_id(self.group_id, 0)
        self.anonymous = _coerce(self.anonymous, Anonymous)

    def get_event_description(self) -> str:
        return (
            f"Message {self.message_id} from {self.user_id}@[群:{self.group_id}] "
            f"{self.original_message.to_rich_text()!r}"
        )

    def get_session_id(self) -> str:
        return f"group_{self.group_id}_{self.user_id}"


class NoticeEvent(Event):
    """通知事件。"""

    _fields = {"post_type": "notice", "notice_type": ""}

    def get_event_name(self) -> str:
        sub_type = getattr(self, "sub_type", None)
        name = f"{self.post_type}.{self.notice_type}"
        return f"{name}.{sub_type}" if sub_type else name


class GroupUploadNoticeEvent(NoticeEvent):
    """群文件上传事件。"""

    _fields = {"notice_type": "group_upload", "user_id": 0, "group_id": 0, "file": None}

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.user_id = _coerce_id(self.user_id, 0)
        self.group_id = _coerce_id(self.group_id, 0)
        self.file = _coerce(self.file, File)

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_session_id(self) -> str:
        return f"group_{self.group_id}_{self.user_id}"


class GroupAdminNoticeEvent(NoticeEvent):
    """群管理员变动。"""

    _fields = {"notice_type": "group_admin", "sub_type": "", "user_id": 0, "group_id": 0}

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.user_id = _coerce_id(self.user_id, 0)
        self.group_id = _coerce_id(self.group_id, 0)

    def is_tome(self) -> bool:
        return self.user_id == self.self_id

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_session_id(self) -> str:
        return f"group_{self.group_id}_{self.user_id}"


class GroupDecreaseNoticeEvent(NoticeEvent):
    """群成员减少事件。"""

    _fields = {
        "notice_type": "group_decrease",
        "sub_type": "",
        "user_id": 0,
        "group_id": 0,
        "operator_id": 0,
    }

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.user_id = _coerce_id(self.user_id, 0)
        self.group_id = _coerce_id(self.group_id, 0)
        self.operator_id = _coerce_id(self.operator_id, 0)

    def is_tome(self) -> bool:
        return self.user_id == self.self_id

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_session_id(self) -> str:
        return f"group_{self.group_id}_{self.user_id}"


class GroupIncreaseNoticeEvent(NoticeEvent):
    """群成员增加事件。"""

    _fields = {
        "notice_type": "group_increase",
        "sub_type": "",
        "user_id": 0,
        "group_id": 0,
        "operator_id": 0,
    }

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.user_id = _coerce_id(self.user_id, 0)
        self.group_id = _coerce_id(self.group_id, 0)
        self.operator_id = _coerce_id(self.operator_id, 0)

    def is_tome(self) -> bool:
        return self.user_id == self.self_id

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_session_id(self) -> str:
        return f"group_{self.group_id}_{self.user_id}"


class GroupBanNoticeEvent(NoticeEvent):
    """群禁言事件。"""

    _fields = {
        "notice_type": "group_ban",
        "sub_type": "",
        "user_id": 0,
        "group_id": 0,
        "operator_id": 0,
        "duration": 0,
    }

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.user_id = _coerce_id(self.user_id, 0)
        self.group_id = _coerce_id(self.group_id, 0)
        self.operator_id = _coerce_id(self.operator_id, 0)
        self.duration = _coerce_int(self.duration)

    def is_tome(self) -> bool:
        return self.user_id == self.self_id

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_session_id(self) -> str:
        return f"group_{self.group_id}_{self.user_id}"


class FriendAddNoticeEvent(NoticeEvent):
    """好友添加事件。"""

    _fields = {"notice_type": "friend_add", "user_id": 0}

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.user_id = _coerce_id(self.user_id, 0)

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_session_id(self) -> str:
        return str(self.user_id)


class GroupRecallNoticeEvent(NoticeEvent):
    """群消息撤回事件。"""

    _fields = {
        "notice_type": "group_recall",
        "user_id": 0,
        "group_id": 0,
        "operator_id": 0,
        "message_id": 0,
    }

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.user_id = _coerce_id(self.user_id, 0)
        self.group_id = _coerce_id(self.group_id, 0)
        self.operator_id = _coerce_id(self.operator_id, 0)
        self.message_id = _coerce_int(self.message_id)

    def is_tome(self) -> bool:
        return self.user_id == self.self_id

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_session_id(self) -> str:
        return f"group_{self.group_id}_{self.user_id}"


class FriendRecallNoticeEvent(NoticeEvent):
    """好友消息撤回事件。"""

    _fields = {"notice_type": "friend_recall", "user_id": 0, "message_id": 0}

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.user_id = _coerce_id(self.user_id, 0)
        self.message_id = _coerce_int(self.message_id)

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_session_id(self) -> str:
        return str(self.user_id)


class NotifyEvent(NoticeEvent):
    """提醒事件。"""

    _fields = {"notice_type": "notify", "sub_type": "", "user_id": 0, "group_id": 0}

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.user_id = _coerce_id(self.user_id, 0)
        self.group_id = _coerce_id(self.group_id, 0)

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_session_id(self) -> str:
        return f"group_{self.group_id}_{self.user_id}"


class PokeNotifyEvent(NotifyEvent):
    """戳一戳提醒事件。"""

    _fields = {"sub_type": "poke", "target_id": 0, "group_id": None}

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.target_id = _coerce_id(self.target_id, 0)
        self.group_id = None if self.group_id is None else _coerce_id(self.group_id, 0)

    def is_tome(self) -> bool:
        return self.target_id == self.self_id

    def get_session_id(self) -> str:
        if not self.group_id:
            return str(self.user_id)
        return super().get_session_id()


class LuckyKingNotifyEvent(NotifyEvent):
    """群红包运气王提醒事件。"""

    _fields = {"sub_type": "lucky_king", "target_id": 0}

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.target_id = _coerce_id(self.target_id, 0)

    def is_tome(self) -> bool:
        return self.target_id == self.self_id

    def get_user_id(self) -> str:
        return str(self.target_id)

    def get_session_id(self) -> str:
        return f"group_{self.group_id}_{self.target_id}"


class HonorNotifyEvent(NotifyEvent):
    """群荣誉变更提醒事件。"""

    _fields = {"sub_type": "honor", "honor_type": ""}

    def is_tome(self) -> bool:
        return self.user_id == self.self_id


class RequestEvent(Event):
    """请求事件。"""

    _fields = {"post_type": "request", "request_type": ""}

    def get_event_name(self) -> str:
        sub_type = getattr(self, "sub_type", None)
        name = f"{self.post_type}.{self.request_type}"
        return f"{name}.{sub_type}" if sub_type else name


class FriendRequestEvent(RequestEvent):
    """加好友请求事件。"""

    _fields = {"request_type": "friend", "user_id": 0, "flag": "", "comment": None}

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.user_id = _coerce_id(self.user_id, 0)

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_session_id(self) -> str:
        return str(self.user_id)

    async def approve(self, bot: Any, remark: str = "") -> Any:
        return await bot.set_friend_add_request(flag=self.flag, approve=True, remark=remark)

    async def reject(self, bot: Any) -> Any:
        return await bot.set_friend_add_request(flag=self.flag, approve=False)


class GroupRequestEvent(RequestEvent):
    """加群请求/邀请事件。"""

    _fields = {
        "request_type": "group",
        "sub_type": "",
        "group_id": 0,
        "user_id": 0,
        "flag": "",
        "comment": None,
    }

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.group_id = _coerce_id(self.group_id, 0)
        self.user_id = _coerce_id(self.user_id, 0)

    def get_user_id(self) -> str:
        return str(self.user_id)

    def get_session_id(self) -> str:
        return f"group_{self.group_id}_{self.user_id}"

    async def approve(self, bot: Any) -> Any:
        return await bot.set_group_add_request(flag=self.flag, sub_type=self.sub_type, approve=True)

    async def reject(self, bot: Any, reason: str = "") -> Any:
        return await bot.set_group_add_request(
            flag=self.flag, sub_type=self.sub_type, approve=False, reason=reason
        )


class MetaEvent(Event):
    """元事件。"""

    _fields = {"post_type": "meta_event", "meta_event_type": ""}

    def get_event_name(self) -> str:
        sub_type = getattr(self, "sub_type", None)
        name = f"{self.post_type}.{self.meta_event_type}"
        return f"{name}.{sub_type}" if sub_type else name

    def get_log_string(self) -> str:
        raise NoLogException


class LifecycleMetaEvent(MetaEvent):
    """生命周期元事件。"""

    _fields = {"meta_event_type": "lifecycle", "sub_type": ""}


class HeartbeatMetaEvent(MetaEvent):
    """心跳元事件。"""

    _fields = {"meta_event_type": "heartbeat", "status": None, "interval": 0}

    def __init__(self, **data: Any) -> None:
        super().__init__(**data)
        self.status = _coerce(self.status, Status)
        self.interval = _coerce_int(self.interval)


_EVENT_CLASSES: dict[str, type[Event]] = {
    "message": MessageEvent,
    "message.private": PrivateMessageEvent,
    "message.group": GroupMessageEvent,
    "notice": NoticeEvent,
    "notice.group_upload": GroupUploadNoticeEvent,
    "notice.group_admin": GroupAdminNoticeEvent,
    "notice.group_decrease": GroupDecreaseNoticeEvent,
    "notice.group_increase": GroupIncreaseNoticeEvent,
    "notice.group_ban": GroupBanNoticeEvent,
    "notice.friend_add": FriendAddNoticeEvent,
    "notice.group_recall": GroupRecallNoticeEvent,
    "notice.friend_recall": FriendRecallNoticeEvent,
    "notice.notify": NotifyEvent,
    "notice.notify.poke": PokeNotifyEvent,
    "notice.notify.lucky_king": LuckyKingNotifyEvent,
    "notice.notify.honor": HonorNotifyEvent,
    "request": RequestEvent,
    "request.friend": FriendRequestEvent,
    "request.group": GroupRequestEvent,
    "meta_event": MetaEvent,
    "meta_event.lifecycle": LifecycleMetaEvent,
    "meta_event.heartbeat": HeartbeatMetaEvent,
}


def check_at_me(event: "MessageEvent") -> None:
    """按 OneBot v11 适配器的做法检查开头的 @机器人 / 结尾的 at 段（shim 扩展）。

    上游由 ``Adapter`` 在收到上报事件时调用；桥接层构建事件后调用同一个函数，
    这样 ``event.to_me`` 与消息里 at 段的去除行为与真实协议端一致。
    """
    if not isinstance(event, MessageEvent):
        return
    if not event.message:
        event.message.append(MessageSegment.text(""))

    def _is_at_me_seg(segment: MessageSegment) -> bool:
        return segment.type == "at" and str(segment.data.get("qq", "")) == str(event.self_id)

    if event.message_type == "private":
        event.to_me = True
    else:
        if _is_at_me_seg(event.message[0]):
            event.to_me = True
            event.message.pop(0)
            if event.message and event.message[0].type == "text":
                event.message[0].data["text"] = str(event.message[0].data["text"]).lstrip()
                if not event.message[0].data["text"]:
                    del event.message[0]
            if event.message and _is_at_me_seg(event.message[0]):
                event.message.pop(0)
                if event.message and event.message[0].type == "text":
                    event.message[0].data["text"] = str(event.message[0].data["text"]).lstrip()
                    if not event.message[0].data["text"]:
                        del event.message[0]

        if not event.to_me and event.message:
            index = -1
            last_seg = event.message[index]
            if last_seg.type == "text" and not str(last_seg.data.get("text", "")).strip() and len(event.message) >= 2:
                index -= 1
                last_seg = event.message[index]
            if _is_at_me_seg(last_seg):
                event.to_me = True
                del event.message[index:]

    if not event.message:
        event.message.append(MessageSegment.text(""))


def check_nickname(event: "MessageEvent", nicknames: Any) -> None:
    """检查消息开头是否在叫机器人昵称（shim 扩展，与上游 ``_check_nickname`` 等价）。

    ``nicknames`` 为空时不做任何处理（桥接层默认不配置昵称，避免误吞用户文本）。
    """
    import re

    if not isinstance(event, MessageEvent) or not event.message:
        return
    first_seg = event.message[0]
    if first_seg.type != "text":
        return
    escaped = {re.escape(str(name)) for name in (nicknames or ()) if str(name)}
    if not escaped:
        return
    pattern = "|".join(sorted(escaped))
    first_text = str(first_seg.data.get("text", ""))
    match = re.search(rf"^({pattern})([\s,，]*|$)", first_text, re.IGNORECASE)
    if match:
        event.to_me = True
        first_seg.data["text"] = first_text[match.end() :]


def build_event(payload: dict[str, Any], event_type: str = "") -> Event:
    """从 OneBot v11 payload 构造事件（shim 扩展：桥接层 ``dispatch_event`` 用）。

    选择最具体的类：先按 ``post_type`` 找到基类，再用 ``sub_type`` / ``notice_type`` /
    ``request_type`` / ``meta_event_type`` 细化。
    """
    data = dict(payload)
    post_type = str(data.get("post_type") or event_type or "").split("@", 1)[0]
    if not post_type:
        raise ValueError("payload has no post_type and no event_type was given")
    data["post_type"] = post_type

    key = post_type
    sub = data.get("sub_type")
    if post_type == "message":
        key = f"message.{data.get('message_type') or ''}".rstrip(".")
    elif post_type == "notice":
        key = f"notice.{data.get('notice_type') or ''}".rstrip(".")
        if sub and f"{key}.{sub}" in _EVENT_CLASSES:
            key = f"{key}.{sub}"
    elif post_type == "request":
        key = f"request.{data.get('request_type') or ''}".rstrip(".")
    elif post_type == "meta_event":
        key = f"meta_event.{data.get('meta_event_type') or ''}".rstrip(".")

    event_cls = _EVENT_CLASSES.get(key) or _EVENT_CLASSES.get(post_type)
    if event_cls is None:
        raise ValueError(f"unsupported onebot v11 event type: {post_type!r}")
    return event_cls(**data)
