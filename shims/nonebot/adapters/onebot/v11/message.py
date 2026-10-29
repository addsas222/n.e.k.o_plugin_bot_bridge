"""OneBot v11 消息类型（上游 ``onebot.v11.message`` 的子集）。

核心语义与上游一致：``Message("...")`` 解析 CQ 码，``str(Message)`` 还原 CQ 码，
``extract_plain_text()`` 只取纯文本段，``MessageSegment`` 提供 ``text`` / ``at`` / ``image``
等工厂方法。

偏差：上游 ``MessageSegment`` / ``Message`` 走 pydantic 校验（dict → 消息段会做类型校验），
shim 直接把 dict 转成消息段；``to_rich_text`` 的截断长度等展示细节可能与上游略有不同。
"""

from __future__ import annotations

import re
from io import BytesIO
from pathlib import Path
from typing import Any

from nonebot.adapters import Message as BaseMessage
from nonebot.adapters import MessageSegment as BaseMessageSegment

from .utils import b2s, escape, f2s, unescape

__all__ = ["Message", "MessageSegment"]

_TYPE_PATTERN = r"\[CQ:(?P<type>[a-zA-Z0-9-_.]+)(?P<params>(?:,[a-zA-Z0-9-_.]+=[^,\]]*)*),?\]"


class MessageSegment(BaseMessageSegment["Message"]):
    """OneBot v11 协议 MessageSegment 适配。"""

    @classmethod
    def get_message_class(cls) -> type["Message"]:
        return Message

    def __str__(self) -> str:
        if self.is_text():
            return escape(self.data.get("text", ""), escape_comma=False)

        params = ",".join(f"{k}={escape(str(v))}" for k, v in self.data.items() if v is not None)
        return f"[CQ:{self.type}{',' if params else ''}{params}]"

    def to_rich_text(self, truncate: int | None = 70) -> str:
        """人类可读形式（``[image:file=...]``），用于日志展示。"""
        if self.is_text():
            return str(self.data.get("text", ""))
        limit = truncate if truncate else 0

        def _cut(value: Any) -> str:
            text = str(value)
            return text[:limit] + "..." if limit and len(text) > limit else text

        params = ",".join(f"{k}={_cut(v)}" for k, v in self.data.items() if v is not None)
        return f"[{self.type}{':' if params else ''}{params}]"

    def __add__(self, other: str | "MessageSegment" | Any) -> "Message":
        return Message(self) + (MessageSegment.text(other) if isinstance(other, str) else other)

    def __radd__(self, other: str | "MessageSegment" | Any) -> "Message":
        return (MessageSegment.text(other) if isinstance(other, str) else Message(other)) + self

    def is_text(self) -> bool:
        return self.type == "text"

    # ------------------------------------------------------------ 工厂方法
    @classmethod
    def anonymous(cls, ignore_failure: bool | None = None) -> "MessageSegment":
        return cls("anonymous", {"ignore": b2s(ignore_failure)})

    @classmethod
    def at(cls, user_id: int | str) -> "MessageSegment":
        return cls("at", {"qq": str(user_id)})

    @classmethod
    def contact(cls, type_: str, id: int) -> "MessageSegment":
        return cls("contact", {"type": type_, "id": str(id)})

    @classmethod
    def contact_group(cls, group_id: int) -> "MessageSegment":
        return cls("contact", {"type": "group", "id": str(group_id)})

    @classmethod
    def contact_user(cls, user_id: int) -> "MessageSegment":
        return cls("contact", {"type": "qq", "id": str(user_id)})

    @classmethod
    def dice(cls) -> "MessageSegment":
        return cls("dice", {})

    @classmethod
    def face(cls, id_: int) -> "MessageSegment":
        return cls("face", {"id": str(id_)})

    @classmethod
    def forward(cls, id_: str) -> "MessageSegment":
        return cls("forward", {"id": id_})

    @classmethod
    def image(
        cls,
        file: str | bytes | BytesIO | Path,
        type_: str | None = None,
        cache: bool = True,
        proxy: bool = True,
        timeout: int | None = None,
    ) -> "MessageSegment":
        return cls(
            "image",
            {
                "file": f2s(file),
                "type": type_,
                "cache": b2s(cache),
                "proxy": b2s(proxy),
                "timeout": timeout if timeout is None else str(timeout),
            },
        )

    @classmethod
    def json(cls, data: str) -> "MessageSegment":
        return cls("json", {"data": data})

    @classmethod
    def location(
        cls,
        latitude: float,
        longitude: float,
        title: str | None = None,
        content: str | None = None,
    ) -> "MessageSegment":
        return cls(
            "location",
            {"lat": str(latitude), "lon": str(longitude), "title": title, "content": content},
        )

    @classmethod
    def music(cls, type_: str, id_: int) -> "MessageSegment":
        return cls("music", {"type": type_, "id": str(id_)})

    @classmethod
    def music_custom(
        cls,
        url: str,
        audio: str,
        title: str,
        content: str | None = None,
        img_url: str | None = None,
    ) -> "MessageSegment":
        return cls(
            "music",
            {"type": "custom", "url": url, "audio": audio, "title": title, "content": content, "image": img_url},
        )

    @classmethod
    def node(cls, id_: int) -> "MessageSegment":
        return cls("node", {"id": str(id_)})

    @classmethod
    def node_custom(cls, user_id: int, nickname: str, content: str | "Message") -> "MessageSegment":
        return cls("node", {"user_id": str(user_id), "nickname": nickname, "content": content})

    @classmethod
    def poke(cls, type_: str, id_: str) -> "MessageSegment":
        return cls("poke", {"type": type_, "id": id_})

    @classmethod
    def record(
        cls,
        file: str | bytes | BytesIO | Path,
        magic: bool | None = None,
        cache: bool | None = None,
        proxy: bool | None = None,
        timeout: int | None = None,
    ) -> "MessageSegment":
        return cls(
            "record",
            {"file": f2s(file), "magic": b2s(magic), "cache": b2s(cache), "proxy": b2s(proxy), "timeout": timeout},
        )

    @classmethod
    def reply(cls, id_: int) -> "MessageSegment":
        return cls("reply", {"id": str(id_)})

    @classmethod
    def rps(cls) -> "MessageSegment":
        return cls("rps", {})

    @classmethod
    def shake(cls) -> "MessageSegment":
        return cls("shake", {})

    @classmethod
    def share(
        cls,
        url: str = "",
        title: str = "",
        content: str | None = None,
        image: str | None = None,
    ) -> "MessageSegment":
        return cls("share", {"url": url, "title": title, "content": content, "image": image})

    @classmethod
    def text(cls, text: str) -> "MessageSegment":
        return cls("text", {"text": text})

    @classmethod
    def video(
        cls,
        file: str | bytes | BytesIO | Path,
        cache: bool | None = None,
        proxy: bool | None = None,
        timeout: int | None = None,
    ) -> "MessageSegment":
        return cls("video", {"file": f2s(file), "cache": b2s(cache), "proxy": b2s(proxy), "timeout": timeout})

    @classmethod
    def xml(cls, data: str) -> "MessageSegment":
        return cls("xml", {"data": data})


class Message(BaseMessage[MessageSegment]):
    """OneBot v11 协议 Message 适配。"""

    @classmethod
    def get_segment_class(cls) -> type[MessageSegment]:
        return MessageSegment

    def __add__(self, other: Any) -> "Message":
        return super().__add__(MessageSegment.text(other) if isinstance(other, str) else other)

    def __radd__(self, other: Any) -> "Message":
        return super().__radd__(MessageSegment.text(other) if isinstance(other, str) else other)

    def __iadd__(self, other: Any) -> "Message":
        return super().__iadd__(MessageSegment.text(other) if isinstance(other, str) else other)

    def to_rich_text(self, truncate: int | None = 70) -> str:
        return "".join(seg.to_rich_text(truncate=truncate) for seg in self)

    @staticmethod
    def _construct(msg: str) -> Any:
        def _iter_message(text: str) -> Any:
            text_begin = 0
            for cqcode in re.finditer(_TYPE_PATTERN, text):
                yield "text", text[text_begin : cqcode.pos + cqcode.start()]
                text_begin = cqcode.pos + cqcode.end()
                yield cqcode.group("type"), cqcode.group("params").lstrip(",")
            yield "text", text[text_begin:]

        for type_, data in _iter_message(msg):
            if type_ == "text":
                if data:
                    # 只产出非空文本段（与上游一致）
                    yield MessageSegment(type_, {"text": unescape(data)})
            else:
                parsed = {
                    key: unescape(value)
                    for key, value in (
                        item.split("=", maxsplit=1)
                        for item in (part.strip() for part in data.split(","))
                        if item
                    )
                }
                yield MessageSegment(type_, parsed)

    def extract_plain_text(self) -> str:
        return "".join(str(seg.data.get("text", "")) for seg in self if seg.is_text())

    def reduce(self) -> None:
        """合并消息内连续的纯文本段。"""
        index = 1
        while index < len(self):
            if self[index - 1].type == "text" and self[index].type == "text":
                self[index - 1].data["text"] += self[index].data["text"]
                del self[index]
            else:
                index += 1


def ensure_message(value: Any) -> Message:
    """把 str / list / dict / Message 统一转成 :class:`Message`（上游由 pydantic 完成）。"""
    if isinstance(value, Message):
        return value
    if isinstance(value, str):
        return Message(value)
    if isinstance(value, dict):
        return Message([_coerce_segment(value)])
    if isinstance(value, (list, tuple)):
        return Message([_coerce_segment(item) for item in value])
    raise TypeError(f"Cannot convert {type(value)!r} to Message")


def _coerce_segment(value: Any) -> MessageSegment:
    if isinstance(value, MessageSegment):
        return value
    if isinstance(value, dict):
        return MessageSegment(str(value.get("type", "")), dict(value.get("data") or {}))
    raise TypeError(f"Cannot convert {type(value)!r} to MessageSegment")
