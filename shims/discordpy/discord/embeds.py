"""``discord.Embed``（上游 ``discord.embeds`` 的核心部分）。

只保留构造与导出：``to_dict()`` 与上游字段名一致，因此兼容层可以把 Embed 文本化后
交给 ``HostServices.send_chat``；``Image`` / ``Thumbnail`` / ``File`` 这些媒体部分
在 N.E.K.O. 里没有投放面，只作为元数据保留。
"""

from __future__ import annotations

import datetime
from typing import Any

from .colour import Colour
from .utils import MISSING

__all__ = ["Embed"]


class Embed:
    """一条富文本消息（上游 ``discord.Embed`` 的子集）。"""

    def __init__(
        self,
        *,
        title: Any = None,
        type: str = "rich",  # noqa: A002 — 与上游字段名一致
        description: Any = None,
        url: Any = None,
        timestamp: datetime.datetime | None = None,
        colour: Any = MISSING,
        color: Any = MISSING,
    ) -> None:
        self.title: str | None = None if title is MISSING else title
        self.type: str = "rich" if type is MISSING else str(type)
        self.description: str | None = None if description is MISSING else description
        self.url: str | None = None if url is MISSING else url
        if timestamp is not None and not isinstance(timestamp, datetime.datetime):
            raise TypeError("timestamp must be a datetime.datetime, not " + type(timestamp).__name__)
        self.timestamp: datetime.datetime | None = timestamp
        self.colour: Colour = self._resolve_colour(colour if colour is not MISSING else color)
        self.footer: dict[str, str] = {}
        self.image: dict[str, str] = {}
        self.thumbnail: dict[str, str] = {}
        self.video: dict[str, str] = {}
        self.provider: dict[str, str] = {}
        self.author: dict[str, str] = {}
        self.fields: list[dict[str, Any]] = []

    # ---- 颜色 ------------------------------------------------------------ #

    @staticmethod
    def _resolve_colour(value: Any) -> Colour:
        if value is MISSING or value is None:
            return Colour.default()
        if isinstance(value, Colour):
            return value
        if isinstance(value, int):
            return Colour(value)
        raise TypeError(f"Expected Colour, int or None, received {type(value).__name__} instead.")

    @property
    def color(self) -> Colour:
        """``colour`` 的上游美式别名。"""
        return self.colour

    @color.setter
    def color(self, value: Any) -> None:
        self.colour = self._resolve_colour(value)

    # ---- 内容 ------------------------------------------------------------ #

    def add_field(self, *, name: str, value: str, inline: bool = True) -> "Embed":
        self.fields.append({"inline": bool(inline), "name": str(name), "value": str(value)})
        return self

    def add_fields(self, *fields: Any) -> "Embed":
        for field in fields:
            if isinstance(field, dict):
                self.add_field(**field)
            else:
                self.add_field(name=field[0], value=field[1], inline=field[2] if len(field) > 2 else True)
        return self

    def clear_fields(self) -> None:
        self.fields.clear()

    def remove_field(self, index: int) -> None:
        del self.fields[index]

    def insert_field_at(self, index: int, *, name: str, value: str, inline: bool = True) -> "Embed":
        self.fields.insert(index, {"inline": bool(inline), "name": str(name), "value": str(value)})
        return self

    def set_field_at(self, index: int, *, name: str, value: str, inline: bool = True) -> "Embed":
        self.fields[index] = {"inline": bool(inline), "name": str(name), "value": str(value)}
        return self

    def set_footer(self, *, text: Any = None, icon_url: Any = None) -> "Embed":
        self.footer = {"text": str(text or ""), "icon_url": str(icon_url or "")}
        return self

    def set_image(self, *, url: Any) -> "Embed":
        self.image = {"url": str(url)}
        return self

    def set_thumbnail(self, *, url: Any) -> "Embed":
        self.thumbnail = {"url": str(url)}
        return self

    def set_author(self, *, name: Any, url: Any = None, icon_url: Any = None) -> "Embed":
        self.author = {"name": str(name)}
        if url:
            self.author["url"] = str(url)
        if icon_url:
            self.author["icon_url"] = str(icon_url)
        return self

    def set_video(self, *, url: Any) -> "Embed":
        self.video = {"url": str(url)}
        return self

    def set_provider(self, *, name: Any, url: Any = None) -> "Embed":
        self.provider = {"name": str(name)}
        if url:
            self.provider["url"] = str(url)
        return self

    # ---- 导出 ------------------------------------------------------------ #

    def to_dict(self) -> dict[str, Any]:
        """与上游字段名一致的字典（兼容层用它把 Embed 文本化）。"""
        data: dict[str, Any] = {}
        if self.title is not None:
            data["title"] = self.title
        if self.description is not None:
            data["description"] = self.description
        if self.url is not None:
            data["url"] = self.url
        if self.timestamp is not None:
            data["timestamp"] = self.timestamp.isoformat()
        if self.colour:
            data["color"] = self.colour.value
        if self.footer:
            data["footer"] = dict(self.footer)
        if self.image:
            data["image"] = dict(self.image)
        if self.thumbnail:
            data["thumbnail"] = dict(self.thumbnail)
        if self.video:
            data["video"] = dict(self.video)
        if self.provider:
            data["provider"] = dict(self.provider)
        if self.author:
            data["author"] = dict(self.author)
        if self.fields:
            data["fields"] = [dict(field) for field in self.fields]
        data["type"] = self.type
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Embed":
        """从 ``to_dict()`` 的产物重建（``color`` 字段按上游语义读）。"""
        embed = cls(
            title=data.get("title"),
            type=data.get("type", "rich"),
            description=data.get("description"),
            url=data.get("url"),
            colour=data.get("color", MISSING),
        )
        if data.get("timestamp"):
            embed.timestamp = datetime.datetime.fromisoformat(str(data["timestamp"]))
        for key in ("footer", "image", "thumbnail", "video", "provider", "author"):
            if isinstance(data.get(key), dict):
                setattr(embed, key, dict(data[key]))
        for field in data.get("fields") or ():
            if isinstance(field, dict):
                embed.add_field(
                    name=field.get("name", ""), value=field.get("value", ""), inline=bool(field.get("inline", True))
                )
        return embed

    def copy(self) -> "Embed":
        return self.from_dict(self.to_dict())

    def __len__(self) -> int:
        """字符总数（上游用来判断 6000 上限）。"""
        total = len(str(self.title or "")) + len(str(self.description or ""))
        total += len(str(self.footer.get("text") or "")) + len(str(self.author.get("name") or ""))
        return total + sum(len(str(field["name"])) + len(str(field["value"])) for field in self.fields)

    def __repr__(self) -> str:
        return f"<Embed title={self.title!r} type={self.type!r} fields={len(self.fields)}>"
