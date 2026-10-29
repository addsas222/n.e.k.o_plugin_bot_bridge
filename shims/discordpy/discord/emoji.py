"""``discord.abc.Emoji`` 家族：``PartialEmoji`` / ``Emoji``。

只做数据表示与提及字符串；表情的收发 / 管理在 N.E.K.O. 里没有对应面。
"""

from __future__ import annotations

from typing import Any

from .utils import MISSING

__all__ = ["Emoji", "PartialEmoji"]


class PartialEmoji:
    """一个表情（可能是自定义的，也可能是 unicode 的）。"""

    def __init__(
        self,
        *,
        name: str | None = None,
        id: Any = MISSING,  # noqa: A002 — 与上游字段名一致
        animated: bool = False,
    ) -> None:
        self.name: str | None = name
        self.id: int | None = None if id is MISSING or id is None else int(id)
        self.animated: bool = bool(animated)

    @classmethod
    def from_str(cls, value: str) -> "PartialEmoji":
        """解析 ``<:name:id>`` / ``<a:name:id>``（与上游一致的宽松解析）。"""
        text = str(value)
        animated = text.startswith("<a:")
        raw = text[3 if animated else 2 :]
        try:
            name, _, emoji_id = raw.rpartition(":")
            return cls(name=name or None, id=int(emoji_id), animated=animated)
        except ValueError:
            return cls(name=text, id=None, animated=False)

    @property
    def url(self) -> str:
        """表情图片地址（N.E.K.O. 不加载它，仅按上游语义拼出来）。"""
        if self.id is None:
            return ""
        suffix = "gif" if self.animated else "png"
        return f"https://cdn.discordapp.com/emojis/{self.id}.{suffix}"

    @property
    def is_custom_emoji(self) -> bool:
        return self.id is not None

    @property
    def is_unicode_emoji(self) -> bool:
        return self.id is None

    def __str__(self) -> str:
        if self.id is None:
            return self.name or ""
        prefix = "a" if self.animated else ""
        return f"<{prefix}:{self.name}:{self.id}>"

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r} id={self.id} animated={self.animated}>"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, PartialEmoji):
            return (self.id, self.name) == (other.id, other.name)
        if isinstance(other, str):
            return str(self) == other or self.name == other
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.id, self.name))


class Emoji(PartialEmoji):
    """服务器自定义表情。"""

    def __init__(self, *, name: str | None = None, id: Any = MISSING, animated: bool = False, guild: Any = None,
                 roles: Any = None, require_colons: bool = True, managed: bool = False, available: bool = True) -> None:
        super().__init__(name=name, id=id, animated=animated)
        self.guild = guild
        self.roles = list(roles or ())
        self.require_colons = bool(require_colons)
        self.managed = bool(managed)
        self.available = bool(available)

    async def delete(self, *args: Any, **kwargs: Any) -> Any:
        from . import _runtime

        raise _runtime.unsupported("Emoji.delete", "N.E.K.O. 不承载 Discord 表情管理")
