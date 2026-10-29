"""CQ 码消息模型（``aiocqhttp.message`` 的忠实子集，只依赖标准库）。

Hoshino 插件对消息的用法集中在三处，这里都覆盖：

* ``ev.message.extract_plain_text()`` —— 取纯文本；
* ``ev.message[0].type == 'text'`` / ``seg.data['text']`` —— 逐段检查（触发器就是这么干的）；
* ``MessageSegment.at(user_id)`` / ``MessageSegment.image('file:///…')`` —— 拼回复。

与上游的差异：
``Message('<str>')`` 会解析字符串里的 ``[CQ:…]`` 段（上游由协议层在构造事件时解析），
这样从 N.E.K.O. 传进来的文本（可能带 CQ 码）能双向还原；纯文本字符串仍是单个 text 段。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any

__all__ = [
    "Message",
    "MessageSegment",
    "escape",
    "normalize_text",
    "unescape",
]

_CQ_ESCAPE = (("&", "&amp;"), ("[", "&#91;"), ("]", "&#93;"), (",", "&#44;"))
_CQ_UNESCAPE = (("&#44;", ","), ("&#93;", "]"), ("&#91;", "["), ("&amp;", "&"))
_CQ_PATTERN = re.compile(r"\[CQ:([A-Za-z0-9_.-]+)((?:,[^\[\]]*)?)\]")
_CQ_ARG_PATTERN = re.compile(r"([A-Za-z0-9_.-]+)=([^,]*)")
#: CQ 码里不长这样子的键值对（上游会原样保留）。
_TEXT_SEGMENT_TYPES = frozenset({"text"})


def escape(text: str, escape_comma: bool = True) -> str:
    """转义 CQ 码里的值。"""
    result = str(text)
    for raw, encoded in _CQ_ESCAPE:
        if raw == "," and not escape_comma:
            continue
        result = result.replace(raw, encoded)
    return result


def unescape(text: str) -> str:
    """还原 CQ 码里的值。"""
    result = str(text)
    for encoded, raw in _CQ_UNESCAPE:
        result = result.replace(encoded, raw)
    return result


def normalize_text(text: str) -> str:
    """规范化文本：NFKC + 小写（对应上游 ``util.normalize_str``）。

    上游还会做繁→简转换（zhconv），shim 只用标准库因而没有这一步 —— 这是已知缺口，
    记在 ``bridge/backends/hoshino.py`` 的模块 docstring 里。
    """
    return unicodedata.normalize("NFKC", str(text)).lower()


class MessageSegment:
    """一个消息段（``[CQ:type,key=value]``）。"""

    __slots__ = ("data", "type")

    def __init__(self, type_: str, data: dict[str, Any] | None = None) -> None:
        self.type = str(type_)
        self.data: dict[str, Any] = dict(data or {})

    # ---- 构造快捷方式（上游同名静态方法） -------------------------------- #

    @staticmethod
    def text(text: Any) -> "MessageSegment":
        return MessageSegment("text", {"text": str(text)})

    @staticmethod
    def image(file: Any) -> "MessageSegment":
        return MessageSegment("image", {"file": str(file)})

    @staticmethod
    def at(user_id: Any) -> "MessageSegment":
        return MessageSegment("at", {"qq": str(user_id)})

    @staticmethod
    def face(id_: Any) -> "MessageSegment":
        return MessageSegment("face", {"id": str(id_)})

    @staticmethod
    def record(file: Any) -> "MessageSegment":
        return MessageSegment("record", {"file": str(file)})

    @staticmethod
    def video(file: Any) -> "MessageSegment":
        return MessageSegment("video", {"file": str(file)})

    @staticmethod
    def rps() -> "MessageSegment":
        return MessageSegment("rps", {})

    @staticmethod
    def dice() -> "MessageSegment":
        return MessageSegment("dice", {})

    @staticmethod
    def shake() -> "MessageSegment":
        return MessageSegment("shake", {})

    @staticmethod
    def poke(user_id: Any) -> "MessageSegment":
        return MessageSegment("poke", {"qq": str(user_id)})

    @staticmethod
    def reply(id_: Any) -> "MessageSegment":
        return MessageSegment("reply", {"id": str(id_)})

    @staticmethod
    def share(url: str, title: str, content: str = "", image: str = "") -> "MessageSegment":
        return MessageSegment("share", {"url": url, "title": title, "content": content, "image": image})

    @staticmethod
    def music(type_: str, id_: Any) -> "MessageSegment":
        return MessageSegment("music", {"type": type_, "id": str(id_)})

    @staticmethod
    def location(lat: Any, lon: Any, title: str = "", content: str = "") -> "MessageSegment":
        return MessageSegment("location", {"lat": str(lat), "lon": str(lon), "title": title, "content": content})

    @staticmethod
    def json(data: Any) -> "MessageSegment":
        return MessageSegment("json", {"data": str(data)})

    @staticmethod
    def xml(data: Any) -> "MessageSegment":
        return MessageSegment("xml", {"data": str(data)})

    # ---- 协议 ------------------------------------------------------------ #

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "data": dict(self.data)}

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MessageSegment":
        return cls(str(payload.get("type") or "text"), dict(payload.get("data") or {}))

    def __str__(self) -> str:
        if self.type in _TEXT_SEGMENT_TYPES:
            return escape(str(self.data.get("text") or ""))
        args = "".join(f",{key}={escape(str(value))}" for key, value in self.data.items())
        return f"[CQ:{self.type}{args}]"

    def __repr__(self) -> str:
        return f"MessageSegment({self.type!r}, {self.data!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, MessageSegment):
            return self.type == other.type and self.data == other.data
        if isinstance(other, str):
            return self.type in _TEXT_SEGMENT_TYPES and str(self.data.get("text") or "") == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.type, tuple(sorted((str(k), str(v)) for k, v in self.data.items()))))

    def __add__(self, other: Any) -> "Message":
        return Message([self]) + other

    def __radd__(self, other: Any) -> "Message":
        return Message(other) + self


class Message(list):
    """一串 :class:`MessageSegment`（``isinstance(msg, list)`` 为真，与上游一致）。"""

    def __init__(self, msg: Any = None) -> None:
        super().__init__()
        self.extend(cls_to_segments(msg))

    # ---- 构造快捷方式 ---------------------------------------------------- #

    @classmethod
    def text(cls, text: Any) -> "Message":
        return cls(MessageSegment.text(text))

    @classmethod
    def image(cls, file: Any) -> "Message":
        return cls(MessageSegment.image(file))

    @classmethod
    def at(cls, user_id: Any) -> "Message":
        return cls(MessageSegment.at(user_id))

    # ---- 列表语义 -------------------------------------------------------- #

    def append(self, segment: Any) -> None:  # type: ignore[override] — 上游同样接受 str
        super().append(_coerce_segment(segment))

    def insert(self, index: int, segment: Any) -> None:
        super().insert(index, _coerce_segment(segment))

    def extend(self, segments: Any) -> None:  # type: ignore[override]
        super().extend(cls_to_segments(segments))

    def __setitem__(self, index: Any, value: Any) -> None:  # type: ignore[override]
        if isinstance(index, slice):
            super().__setitem__(index, cls_to_segments(value))
            return
        super().__setitem__(index, _coerce_segment(value))

    def copy(self) -> "Message":
        copied = Message()
        for segment in self:
            copied.append(MessageSegment(segment.type, dict(segment.data)))
        return copied

    def __add__(self, other: Any) -> "Message":  # type: ignore[override]
        result = self.copy()
        result.extend(other)
        return result

    def __radd__(self, other: Any) -> "Message":
        result = Message(other)
        result.extend(self)
        return result

    def __iadd__(self, other: Any) -> "Message":  # type: ignore[override]
        self.extend(other)
        return self

    # ---- 文本 ------------------------------------------------------------ #

    def extract_plain_text(self) -> str:
        """取出所有 text 段的文本（其它段忽略）。"""
        return "".join(str(segment.data.get("text") or "") for segment in self if segment.type == "text")

    extract_plaintext = extract_plain_text
    """上游别名（少数插件用 ``extract_plaintext``）。"""

    def is_text(self) -> bool:
        """是否只由 text 段组成（空消息也算）。"""
        return all(segment.type == "text" for segment in self)

    def to_dict(self) -> list[dict[str, Any]]:
        return [segment.to_dict() for segment in self]

    def __str__(self) -> str:
        return "".join(str(segment) for segment in self)

    def __repr__(self) -> str:
        return f"Message({list(self)!r})"


def cls_to_segments(value: Any) -> list[MessageSegment]:
    """把任意输入（str / Message / MessageSegment / 迭代器 / dict）规范成消息段列表。"""
    if value is None:
        return []
    if isinstance(value, MessageSegment):
        return [value]
    if isinstance(value, Message):
        return [MessageSegment(segment.type, dict(segment.data)) for segment in value]
    if isinstance(value, str):
        return _parse_cq(value)
    if isinstance(value, dict):
        return [MessageSegment.from_dict(value)]
    if isinstance(value, Iterable):
        result: list[MessageSegment] = []
        for item in value:
            result.extend(cls_to_segments(item))
        return result
    return [MessageSegment.text(value)]


def _coerce_segment(value: Any) -> MessageSegment:
    segments = cls_to_segments(value)
    if len(segments) != 1:
        raise TypeError(f"expect a single message segment, got {value!r}")
    return segments[0]


def _parse_cq(text: str) -> list[MessageSegment]:
    """把 ``text[CQ:at,qq=1]more`` 解析成消息段；没有 CQ 码时就是一个 text 段。"""
    if "[CQ:" not in text:
        return [MessageSegment.text(text)]
    segments: list[MessageSegment] = []
    position = 0
    for match in _CQ_PATTERN.finditer(text):
        if match.start() > position:
            segments.append(MessageSegment.text(unescape(text[position : match.start()])))
        data: dict[str, Any] = {}
        for key, value in _CQ_ARG_PATTERN.findall(match.group(2) or ""):
            data[key] = unescape(value)
        segments.append(MessageSegment(match.group(1), data))
        position = match.end()
    if position < len(text):
        segments.append(MessageSegment.text(unescape(text[position:])))
    return segments
