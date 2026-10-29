"""``discord.ext.commands.converter``：参数转换器。

N.E.K.O. 里没有 Discord 的成员 / 频道 / 角色目录，因此**转换器只认「这次消息上下文里
已知的对象」**：当前作者、当前频道、当前服务器，以及 payload 带来的
``mentions`` / ``channels`` / ``roles`` / ``members`` 数据。查不到就抛上游同名的
``MemberNotFound`` / ``ChannelNotFound`` 之类异常（结构化失败），绝不编造对象。
"""

from __future__ import annotations

import re
from typing import Any

from ...errors import (
    BadColourArgument,
    ChannelNotFound,
    EmojiNotFound,
    GuildNotFound,
    MemberNotFound,
    MessageNotFound,
    ObjectNotFound,
    PartialEmojiConversionFailure,
    RoleNotFound,
    UserNotFound,
)

__all__ = [
    "ChannelConverter",
    "ColorConverter",
    "ColourConverter",
    "Converter",
    "EmojiConverter",
    "GuildConverter",
    "MemberConverter",
    "MessageConverter",
    "PartialEmojiConverter",
    "RoleConverter",
    "UserConverter",
]

_MEMBER_MENTION = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION = re.compile(r"<#(\d+)>")
_ID = re.compile(r"^(\d+)$")

#: 上游 ``Greedy`` 标记（``Greedy[T]`` 一次性吃掉剩余参数）。
_GREEDY_FLAG = "__commands_greedy__"


class Converter:
    """转换器基类（上游 ``discord.ext.commands.Converter``）。"""

    async def convert(self, ctx: Any, argument: str) -> Any:
        raise NotImplementedError("Derived classes need to implement this.")


class _IDConverter(Converter):
    _id_regex = _ID

    @staticmethod
    def _get_id_match(argument: str) -> re.Match[str] | None:
        return _ID.match(argument)


def _raw_lookup(ctx: Any, key: str) -> list[Any]:
    """载荷里带来的对象清单。

    优先取「已经合成成 discord 对象」的那些（``ctx.guild.members`` / ``ctx.bot.users``），
    再退回 ``raw[key]``（只有当载荷里本来就是对象时才用得上）—— 原始字典由兼容层的
    对象工厂负责转成对象，转换器不去猜字典的字段名。
    """
    for source in (getattr(ctx, "raw", None), getattr(getattr(ctx, "message", None), "raw", None)):
        if not isinstance(source, dict):
            continue
        value = source.get(key)
        if isinstance(value, (list, tuple)):
            return [item for item in value if not isinstance(item, dict)]
    return []


def _bot_registry(ctx: Any, name: str) -> list[Any]:
    bot = getattr(ctx, "bot", None)
    if bot is None:
        return []
    value = getattr(bot, name, None)
    if value is None:
        return []
    if callable(value):
        try:
            return list(value())
        except Exception:  # noqa: BLE001 — 注册表取不到就当空
            return []
    try:
        return list(value)
    except TypeError:
        return []


def _guild_members(ctx: Any) -> list[Any]:
    """这次上下文里可见的成员：当前服务器成员 + 桥接层登记过的用户 + 当前作者。"""
    members: list[Any] = []
    guild = getattr(ctx, "guild", None)
    if guild is not None:
        members.extend(guild.members)
    for item in _bot_registry(ctx, "users"):
        if item not in members:
            members.append(item)
    for item in _raw_lookup(ctx, "members"):
        if item not in members:
            members.append(item)
    author = getattr(ctx, "author", None)
    if author is not None and author not in members:
        members.append(author)
    return members


def _known_channels(ctx: Any) -> list[Any]:
    channels: list[Any] = []
    guild = getattr(ctx, "guild", None)
    if guild is not None:
        channels.extend(guild.channels)
    for item in _bot_registry(ctx, "get_all_channels"):
        if item not in channels:
            channels.append(item)
    for item in _raw_lookup(ctx, "channels"):
        if item not in channels:
            channels.append(item)
    current = getattr(ctx, "channel", None)
    if current is not None and current not in channels:
        channels.append(current)
    return channels


def _known_roles(ctx: Any) -> list[Any]:
    roles: list[Any] = []
    guild = getattr(ctx, "guild", None)
    if guild is not None:
        roles.extend(guild.roles)
    for item in _raw_lookup(ctx, "roles"):
        if item not in roles:
            roles.append(item)
    return roles


def _known_guilds(ctx: Any) -> list[Any]:
    guilds: list[Any] = []
    for item in _bot_registry(ctx, "guilds"):
        guilds.append(item)
    guild = getattr(ctx, "guild", None)
    if guild is not None and guild not in guilds:
        guilds.append(guild)
    return guilds


def _finder(items: Any, predicate: Any) -> Any:
    for item in items:
        if predicate(item):
            return item
    return None


class MemberConverter(_IDConverter):
    """把 ``<@id>`` / id / 用户名解析成 :class:`discord.Member`。"""

    async def convert(self, ctx: Any, argument: str) -> Any:
        match = _MEMBER_MENTION.fullmatch(str(argument)) or _ID.match(str(argument))
        if match:
            member = _finder(_guild_members(ctx), lambda item: item.id == int(match.group(1)))
            if member is not None:
                return member
            raise MemberNotFound(str(argument))
        guild = getattr(ctx, "guild", None)
        if guild is not None:
            member = guild.get_member_named(str(argument))
            if member is not None:
                return member
        raise MemberNotFound(str(argument))


class UserConverter(_IDConverter):
    """把 ``<@id>`` / id / 用户名解析成 :class:`discord.User`。

    只在「上下文里已知的用户」中查找（成员清单 / 提及 / 已有用户对象）；N.E.K.O. 没有
    Discord 用户目录，查不到就如实失败。
    """

    async def convert(self, ctx: Any, argument: str) -> Any:
        text = str(argument)
        for member in _guild_members(ctx):
            if text in (member.mention, member.name, member.display_name, str(member.id)):
                return member
        match = _MEMBER_MENTION.fullmatch(text) or _ID.match(text)
        if match:
            user_id = int(match.group(1))
            known = getattr(ctx.bot, "get_user", lambda _id: None)(user_id)
            if known is not None:
                return known
            author = getattr(ctx, "author", None)
            if author is not None and int(getattr(author, "id", 0)) == user_id:
                return author
            raise UserNotFound(text)
        raise UserNotFound(text)


class MessageConverter(_IDConverter):
    """按 id 或 ``频道-消息`` 链接取消息。

    N.E.K.O. 只有「当前这条消息」可见，因此只有 id 等于当前消息 id 时才成功。
    """

    async def convert(self, ctx: Any, argument: str) -> Any:
        match = _ID.match(str(argument).rstrip("/").rsplit("/", 1)[-1])
        message = getattr(ctx, "message", None)
        if match and message is not None and int(match.group(1)) == int(message.id):
            return message
        raise MessageNotFound(str(argument))


class _ChannelFinder(_IDConverter):
    def _find(self, ctx: Any, argument: str) -> Any:
        text = str(argument)
        match = _CHANNEL_MENTION.fullmatch(text) or _ID.match(text)
        channels = _known_channels(ctx)
        if match:
            channel = _finder(channels, lambda item: int(getattr(item, "id", 0)) == int(match.group(1)))
        else:
            name = text.lstrip("#")
            channel = _finder(channels, lambda item: getattr(item, "name", None) == name)
        if channel is None:
            raise ChannelNotFound(text)
        return channel


class ChannelConverter(_ChannelFinder):
    """``#频道`` / id → 频道对象。"""

    async def convert(self, ctx: Any, argument: str) -> Any:
        return self._find(ctx, argument)


class TextChannelConverter(_ChannelFinder):
    """``#文本频道`` / id → 文本频道（非文本频道如实失败）。"""

    async def convert(self, ctx: Any, argument: str) -> Any:
        from ...channel import TextChannel

        channel = self._find(ctx, argument)
        if not isinstance(channel, TextChannel):
            raise ChannelNotFound(str(argument))
        return channel


class GuildConverter(_IDConverter):
    """服务器名 / id → :class:`discord.Guild`（只有当前可见的服务器）。"""

    async def convert(self, ctx: Any, argument: str) -> Any:
        text = str(argument)
        for guild in _known_guilds(ctx):
            if text in (guild.name, str(guild.id)):
                return guild
        raise GuildNotFound(text)


class RoleConverter(_IDConverter):
    """``@角色`` / 角色名 / id → :class:`discord.Role`。"""

    async def convert(self, ctx: Any, argument: str) -> Any:
        text = str(argument)
        roles = _known_roles(ctx)
        match = _ROLE_MENTION.fullmatch(text) or _ID.match(text)
        if match:
            role = _finder(roles, lambda item: int(getattr(item, "id", 0)) == int(match.group(1)))
        else:
            name = text.lstrip("@")
            role = _finder(
                roles, lambda item: getattr(item, "name", None) in (text, name) and not getattr(item, "is_default", False)
            )
        if role is None:
            raise RoleNotFound(text)
        return role


class PartialEmojiConverter(Converter):
    """``<:name:id>`` / 名字 → :class:`discord.PartialEmoji`。"""

    async def convert(self, ctx: Any, argument: str) -> Any:
        from ...emoji import PartialEmoji

        text = str(argument)
        if text.startswith("<") and text.endswith(">"):
            emoji = PartialEmoji.from_str(text)
            if emoji.id is None and not emoji.name:
                raise PartialEmojiConversionFailure(text)
            return emoji
        for item in _bot_registry(ctx, "emojis"):
            if getattr(item, "name", None) == text:
                return item
        raise PartialEmojiConversionFailure(text)


class EmojiConverter(Converter):
    """服务器自定义表情（桥接层登记的可见表情；没有数据时如实失败）。"""

    async def convert(self, ctx: Any, argument: str) -> Any:
        text = str(argument)
        for item in _bot_registry(ctx, "emojis"):
            if text in (getattr(item, "name", None), str(item)):
                return item
        raise EmojiNotFound(text)


class ColourConverter(Converter):
    """颜色（``#ff0000`` / ``0xff0000`` / 名字）。"""

    #: 上游用 ``Colour`` 的类属性做名字查找，这里取同一份调色板。
    async def convert(self, ctx: Any, argument: str) -> Any:
        from ...colour import Colour

        text = str(argument)
        if text.lower() in ("default", "random"):
            return Colour.default() if text.lower() == "default" else Colour.random()
        named = getattr(Colour, text.lower(), None)
        if isinstance(named, Colour):
            return named
        try:
            return Colour.from_str(text)
        except ValueError as exc:
            raise BadColourArgument(text) from exc


class ObjectConverter(_IDConverter):
    """任意 id → :class:`discord.Object`。"""

    async def convert(self, ctx: Any, argument: str) -> Any:
        from ...object import Object

        match = _ID.match(str(argument))
        if not match:
            raise ObjectNotFound(str(argument))
        return Object(int(match.group(1)))


#: 上游美式拼写别名。
ColorConverter = ColourConverter
