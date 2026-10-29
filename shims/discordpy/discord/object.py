"""``discord.Object``：只有 id 的对象（上游 ``discord.object``）。

插件用它做「已经知道 id 但拿不到完整对象」的引用；N.E.K.O. 没有 Discord 目录，
因此 ``Object`` 不会去补全任何字段（上游会按需 fetch，这里 fetch 显式不支持）。
"""

from __future__ import annotations

from typing import Any

from . import _runtime
from .abc import Snowflake

__all__ = ["Object"]


class Object(Snowflake):
    """一个只有 id 的对象。"""

    def __init__(self, id: int, *, type: Any = None) -> None:  # noqa: A002 — 与上游字段名一致
        super().__init__(id)
        self.type = type

    def __repr__(self) -> str:
        return f"<Object id={self.id} type={self.type!r}>"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Object):
            return self.id == other.id and self.type == other.type
        if isinstance(other, Snowflake):
            return self.id == other.id
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self) -> int:
        return hash(self.id)

    async def fetch(self) -> Any:
        raise _runtime.unsupported("Object.fetch", "N.E.K.O. 不提供按 id 查询 Discord 对象的接口")
