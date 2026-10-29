"""``discord.utils``（上游同名工具函数的最小子集）。

只实现**纯函数**：不联网、不读磁盘。``get`` / ``find`` 与上游一致（``get`` 支持
``a__b`` 形式的嵌套属性），``MISSING`` / ``utcnow`` / ``format_dt`` / ``escape_mentions``
按上游语义实现。
"""

from __future__ import annotations

import datetime
from collections.abc import Callable, Iterable, Sequence
from typing import Any, TypeVar

__all__ = [
    "MISSING",
    "escape_mentions",
    "find",
    "format_dt",
    "get",
    "snowflake_time",
    "utcnow",
]

T = TypeVar("T")

#: Discord 纪元（2015-01-01T00:00:00Z），``snowflake_time`` 用。
DISCORD_EPOCH = 1420070400000


class _MissingSentinel:
    """``MISSING``：区分「没传」与「传了 None」（与上游同名同义）。"""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return False

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "..."


MISSING: Any = _MissingSentinel()


def utcnow() -> datetime.datetime:
    """带 UTC 时区的当前时间（上游从 3.12 起返回 aware datetime）。"""
    return datetime.datetime.now(datetime.timezone.utc)


def snowflake_time(snowflake_id: int) -> datetime.datetime:
    """snowflake id → 创建时间（UTC）。"""
    milliseconds = (int(snowflake_id) >> 22) + DISCORD_EPOCH
    return datetime.datetime.fromtimestamp(milliseconds / 1000.0, tz=datetime.timezone.utc)


def format_dt(dt: datetime.datetime, /, style: Any = None) -> str:
    """把时间格式化成 Discord 的时间戳标记 ``<t:unix[:style]>``。

    N.E.K.O. 不渲染这个标记，但插件常把它拼进消息文本；保持原样输出比报错有用。
    """
    if style is None:
        return f"<t:{int(dt.timestamp())}>"
    return f"<t:{int(dt.timestamp())}:{str(style)}>"


def escape_mentions(text: str) -> str:
    """把 ``@everyone`` / ``@here`` / ``<@id>`` 转义（上游同名函数）。"""
    text = text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")
    return text.replace("<@", "<@\u200b").replace("<#", "<#\u200b")


def _attr_getter(path: str) -> Callable[[Any], Any]:
    parts = path.split(".")

    def _get(item: Any) -> Any:
        for part in parts:
            item = getattr(item, part, None)
            if item is None:
                return None
        return item

    return _get


def get(iterable: Iterable[T], **attrs: Any) -> T | None:
    """返回第一个所有 ``attrs`` 都匹配的元素（``a__b`` 表示嵌套属性，与上游一致）。"""
    if not attrs:
        for item in iterable:
            return item
        return None
    getters = {key.replace("__", "."): value for key, value in attrs.items()}
    for item in iterable:
        if all(_attr_getter(path)(item) == value for path, value in getters.items()):
            return item
    return None


def find(predicate: Callable[[T], bool], seq: Sequence[T]) -> T | None:
    """返回第一个满足 ``predicate`` 的元素（上游同名函数）。"""
    for item in seq:
        if predicate(item):
            return item
    return None


def as_chunks(iterator: Iterable[T], max_size: int) -> Any:
    """把可迭代对象切成 ``max_size`` 大小的块（上游同名工具，生成器版）。"""
    if max_size < 1:
        raise ValueError("max_size must be greater than 0")
    chunk: list[T] = []
    for item in iterator:
        chunk.append(item)
        if len(chunk) == max_size:
            yield chunk
            chunk = []
    if chunk:
        yield chunk
