"""OneBot v12 消息模型（``Message`` / ``MessageSegment``）。

与 v11 最大的区别：**v12 没有 CQ 码**。消息是一段 JSON 段数组：

    [{"type": "text", "data": {"text": "hi "}},
     {"type": "mention", "data": {"user_id": "123"}}]

所以这里的 ``_construct`` 只做两件事：字符串 → 单个 ``text`` 段；段数组 / 段对象 →
原样构造。``str(message)`` 会把段渲染成「人类可读 / 可用于命令匹配」的文本
（``text`` 原文、``mention`` → ``@<user_id> ``、``reply`` → 空串）。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from nonebot.internal.adapter import Message as BaseMessage
from nonebot.internal.adapter import MessageSegment as BaseMessageSegment

__all__ = ["Message", "MessageSegment"]


class MessageSegment(BaseMessageSegment["Message"]):
    """OneBot v12 消息段。"""

    @classmethod
    def get_message_class(cls) -> type["Message"]:
        return Message

    def __str__(self) -> str:
        if self.type == "text":
            return str(self.data.get("text", ""))
        if self.type == "mention":
            return f"@{self.data.get('user_id', '')} "
        if self.type == "mention_all":
            return "@全体成员 "
        if self.type == "reply":
            return ""
        if self.type in ("image", "voice", "audio", "video", "file"):
            return f"[{self.type}]"
        if self.type == "face":
            return f"[face:{self.data.get('id', '')}]"
        return f"[{self.type}]"

    def is_text(self) -> bool:
        return self.type == "text"

    # -- 构造器（上游同名） ------------------------------------------------- #
    @classmethod
    def text(cls, text: str) -> "MessageSegment":
        return cls("text", {"text": str(text)})

    @classmethod
    def mention(cls, user_id: str | int) -> "MessageSegment":
        return cls("mention", {"user_id": str(user_id)})

    @classmethod
    def mention_all(cls) -> "MessageSegment":
        return cls("mention_all", {})

    @classmethod
    def image(cls, file_id: str, **kwargs: Any) -> "MessageSegment":
        return cls("image", {"file_id": str(file_id), **kwargs})

    @classmethod
    def voice(cls, file_id: str, **kwargs: Any) -> "MessageSegment":
        return cls("voice", {"file_id": str(file_id), **kwargs})

    @classmethod
    def audio(cls, file_id: str, **kwargs: Any) -> "MessageSegment":
        return cls("audio", {"file_id": str(file_id), **kwargs})

    @classmethod
    def video(cls, file_id: str, **kwargs: Any) -> "MessageSegment":
        return cls("video", {"file_id": str(file_id), **kwargs})

    @classmethod
    def file(cls, file_id: str, **kwargs: Any) -> "MessageSegment":
        return cls("file", {"file_id": str(file_id), **kwargs})

    @classmethod
    def location(cls, latitude: float, longitude: float, title: str = "", content: str = "") -> "MessageSegment":
        return cls(
            "location",
            {"latitude": latitude, "longitude": longitude, "title": title, "content": content},
        )

    @classmethod
    def reply(cls, message_id: str | int) -> "MessageSegment":
        return cls("reply", {"message_id": str(message_id)})

    @classmethod
    def face(cls, id_: int) -> "MessageSegment":
        return cls("face", {"id": str(id_)})

    @classmethod
    def mface(cls, emoji_id: str, **kwargs: Any) -> "MessageSegment":
        return cls("mface", {"emoji_id": str(emoji_id), **kwargs})

    @classmethod
    def dice(cls) -> "MessageSegment":
        return cls("dice", {})

    @classmethod
    def rps(cls) -> "MessageSegment":
        return cls("rps", {})

    @classmethod
    def poke(cls, type_: str = "poke", id_: str = "") -> "MessageSegment":
        return cls("poke", {"type": type_, "id": id_})

    @classmethod
    def json(cls, data: str) -> "MessageSegment":
        return cls("json", {"data": data})

    @classmethod
    def xml(cls, data: str) -> "MessageSegment":
        return cls("xml", {"data": data})

    @classmethod
    def markdown(cls, content: str) -> "MessageSegment":
        return cls("markdown", {"content": content})

    @classmethod
    def node(cls, id_: str) -> "MessageSegment":
        return cls("node", {"id": id_})

    @classmethod
    def forward(cls, id_: str) -> "MessageSegment":
        return cls("forward", {"id": id_})


class Message(BaseMessage[MessageSegment]):
    """OneBot v12 消息数组。"""

    def __init__(self, message: Any = None) -> None:
        """接受 str / 段对象 / 段数组（含 v12 协议那种 ``[{"type":..., "data":...}]``）。

        基类的 ``__init__`` 对「可迭代对象」是直接 ``extend``，不会再走
        ``_construct`` —— 那样传段数组进来时，dict 会原样留在列表里，后面
        ``extract_plain_text()`` 调 ``.is_text()`` 就炸（``'dict' object has no
        attribute 'is_text'``）。v12 的 payload 恰恰就是段数组，所以这里必须先归一化。
        """
        normalized: Any = message
        if message is not None and not isinstance(message, (str, MessageSegment)):
            try:
                items = list(message)
            except TypeError:
                items = None
            if items is not None:
                if any(not isinstance(item, MessageSegment) for item in items):
                    normalized = list(type(self)._construct(items))
                else:
                    normalized = items
        super().__init__(normalized)

    @classmethod
    def get_segment_class(cls) -> type[MessageSegment]:
        return MessageSegment

    @staticmethod
    def _construct(msg: str | Iterable[Any] | dict[str, Any]) -> Iterable[MessageSegment]:
        if isinstance(msg, str):
            if msg:
                yield MessageSegment.text(msg)
            return
        if isinstance(msg, dict):
            yield MessageSegment(str(msg.get("type") or "text"), dict(msg.get("data") or {}))
            return
        for item in msg:
            if isinstance(item, MessageSegment):
                yield item
            elif isinstance(item, dict):
                yield MessageSegment(str(item.get("type") or "text"), dict(item.get("data") or {}))
            else:
                yield MessageSegment.text(str(item))

    def extract_plain_text(self) -> str:
        return "".join(str(seg) for seg in self if seg.is_text())

    def to_payload(self) -> list[dict[str, Any]]:
        """转成 v12 协议里的段数组（发消息 / 上报都用这个形状）。"""
        return [{"type": seg.type, "data": dict(seg.data)} for seg in self]
