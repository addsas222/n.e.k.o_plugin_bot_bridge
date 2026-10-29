"""``discord.abc``（抽象基类的最小子集）。

提供 ``Snowflake``（id 基类）、``Messageable``（能 ``send`` 的载体）、``Connectable``
（语音连接）、``GuildChannel`` / ``PrivateChannel`` 这些**类型判断**会用到的基类。
``Messageable.send`` 是本 shim 的回复收集入口：文本进当前 ``RunReport.replies``
（分发之外则交给 ``HostServices.send_chat``），附件 / 交互组件没有 N.E.K.O. 载体，
只做结构化记录 + 警告日志（见 ``discord._runtime``）。
"""

from __future__ import annotations

import datetime
from typing import Any

from . import _runtime
from .utils import MISSING, snowflake_time

__all__ = ["Connectable", "GuildChannel", "Messageable", "PrivateChannel", "Snowflake"]


class Snowflake:
    """带 snowflake id 的对象（``created_at`` 由 id 推导，与上游一致）。

    刻意**不用** ``__slots__``：不少上游插件会给消息 / 频道对象挂自定义属性
    （``channel.session = ...``），``__slots__`` 会把这种写法变成 AttributeError。
    """

    def __init__(self, id: int) -> None:  # noqa: A002 — 与上游字段名一致
        self.id = int(id)

    @property
    def created_at(self) -> datetime.datetime:
        return snowflake_time(self.id)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Snowflake):
            return self.id == other.id
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self) -> int:
        return self.id >> 22

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id}>"


class Messageable:
    """能接收消息的载体（频道 / 线程 / 私聊）共用的发送实现。"""

    #: 归属的外来插件（由兼容层填；用于分发之外的来源标注）。
    _bridge_plugin_id: str = ""
    #: 分发之外发言的回落投递函数（由兼容层填；缺省时按 plugin_id 走全局回落）。
    _bridge_sender: Any = None

    # ---- 子类需要提供的信息 ---------------------------------------------- #

    def _bridge_channel_label(self) -> str:
        """日志用的载体描述。"""
        name = getattr(self, "name", "")
        return f"{type(self).__name__}({name or self.id})" if hasattr(self, "id") else type(self).__name__

    # ---- 发送 ------------------------------------------------------------ #

    async def send(
        self,
        content: Any = None,
        *,
        tts: bool = False,
        embed: Any = None,
        embeds: Any = None,
        file: Any = None,
        files: Any = None,
        stickers: Any = None,
        delete_after: float | None = None,
        nonce: Any = None,
        allowed_mentions: Any = None,
        reference: Any = None,
        mention_author: Any = None,
        view: Any = None,
        suppress_embeds: Any = MISSING,
        silent: bool = False,
        poll: Any = None,
    ) -> Any:
        """把一条消息投给 N.E.K.O.（上游 ``Messageable.send``）。

        只投递文本（``content`` + ``embed`` / ``embeds`` 文本化）；附件与交互组件
        没有 N.E.K.O. 载体，按结构化记录 + 警告日志处理，不假装送到。
        """
        del tts, delete_after, nonce, allowed_mentions, reference, mention_author, suppress_embeds, silent, poll
        plugin_id = self._bridge_plugin_id or _runtime.current_plugin_id()
        parts: list[str] = []
        if isinstance(content, str) and content:
            parts.append(content)
        elif content is not None and content is not MISSING:
            parts.append(str(content))
        for item in _as_list(embeds) + _as_list(embed):
            text = _embed_text(item)
            if text:
                parts.append(text)
        for item in _as_list(files) + _as_list(file):
            _note_attachment(item)
        for item in _as_list(stickers):
            _runtime.note_component("Sticker", str(item))
        if view is not None:
            _runtime.note_component("View", type(view).__name__)
        text = "\n".join(part for part in parts if part)
        if text:
            await _runtime.deliver(
                text,
                source=plugin_id or "discord",
                plugin_id=plugin_id,
                sender=getattr(self, "_bridge_sender", None),
            )
        return _outgoing_message(self, text, plugin_id)

    async def reply(self, content: Any = None, **kwargs: Any) -> Any:
        """回复一条消息（N.E.K.O. 里没有「引用」概念，因此就是普通发言）。"""
        return await self.send(content, **kwargs)

    # ---- 显式不支持 ------------------------------------------------------ #

    def typing(self) -> Any:
        """``async with channel.typing()``：N.E.K.O. 没有「正在输入」展示位。

        上游语义是「提示对方机器人正在输入」，这里只写一条 debug 日志（与 AstrBot
        兼容层对 ``send_typing`` 的处理一致：不是能力缺失，只是没有展示位）。
        """
        return _Typing(self)

    def history(self, **kwargs: Any) -> Any:
        raise _runtime.unsupported("abc.Messageable.history", "N.E.K.O. 不保存可翻页的频道历史")

    async def fetch_message(self, id: int) -> Any:  # noqa: A002 — 与上游签名一致
        raise _runtime.unsupported("abc.Messageable.fetch_message", f"message id={id} 不在 N.E.K.O. 的可见面里")

    async def pins(self) -> Any:
        raise _runtime.unsupported("abc.Messageable.pins", "N.E.K.O. 没有置顶概念")

    def permissions_for(self, obj: Any = None) -> Any:
        raise _runtime.unsupported("abc.Messageable.permissions_for", "N.E.K.O. 没有 Discord 权限模型")


class _Typing:
    """``channel.typing()`` 的异步上下文管理器（只写 debug 日志）。"""

    __slots__ = ("_target",)

    def __init__(self, target: Any) -> None:
        self._target = target

    async def __aenter__(self) -> "_Typing":
        _runtime.emit_log(
            "debug",
            f"typing() 被调用（N.E.K.O. 没有正在输入展示位）：{getattr(self._target, 'name', '')}",
            source="discord",
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class Connectable:
    """能连语音的载体（N.E.K.O. 没有语音面，一律显式不支持）。"""

    @property
    def voice_client(self) -> Any:
        return None

    async def connect(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("abc.Connectable.connect", "N.E.K.O. 不承载语音连接")

    async def create_voice_clients(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("abc.Connectable.create_voice_clients", "N.E.K.O. 不承载语音连接")


class GuildChannel(Snowflake):
    """服务器频道（``discord.abc.GuildChannel`` 的类型替身）。"""

    def __init__(
        self,
        id: int,  # noqa: A002
        *,
        name: str = "",
        guild: Any = None,
        position: int = 0,
        nsfw: bool = False,
        category_id: int | None = None,
    ) -> None:
        super().__init__(id)
        self.name = str(name)
        self.guild = guild
        self.position = int(position)
        self.nsfw = bool(nsfw)
        self.category_id = category_id
        self._bridge_plugin_id = getattr(guild, "_bridge_plugin_id", "")

    @property
    def mention(self) -> str:
        return f"<#{self.id}>"

    @property
    def category(self) -> Any:
        guild = self.guild
        if guild is None or self.category_id is None:
            return None
        return guild.get_channel(self.category_id)

    def is_nsfw(self) -> bool:
        return bool(self.nsfw)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id} name={self.name!r}>"


class PrivateChannel(Snowflake):
    """私聊频道（``discord.abc.PrivateChannel`` 的类型替身）。"""

    def __init__(self, id: int, *, recipient: Any = None) -> None:  # noqa: A002
        super().__init__(id)
        self.recipient = recipient
        self._bridge_plugin_id = getattr(recipient, "_bridge_plugin_id", "")


def _as_list(value: Any) -> list[Any]:
    if value is None or value is MISSING:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _note_attachment(item: Any) -> None:
    name = getattr(item, "filename", None) or getattr(item, "name", None) or str(item)
    url = ""
    fp = getattr(item, "url", None)
    if isinstance(fp, str):
        url = fp
    size = getattr(item, "size", 0) or 0
    _runtime.note_attachment(str(name), url, int(size or 0))


def _embed_text(embed: Any) -> str:
    """把 ``Embed`` 文本化（``Embed.to_dict`` 优先，兼容任何带该方法的对象）。"""
    to_dict = getattr(embed, "to_dict", None)
    data = to_dict() if callable(to_dict) else embed
    if not isinstance(data, dict):
        return str(embed)
    lines: list[str] = []
    title = data.get("title")
    if title:
        lines.append(str(title))
    description = data.get("description")
    if description:
        lines.append(str(description))
    for field in data.get("fields") or ():
        if isinstance(field, dict):
            lines.append(f"{field.get('name', '')}：{field.get('value', '')}".strip("："))
        else:
            lines.append(str(field))
    footer = data.get("footer")
    if isinstance(footer, dict) and footer.get("text"):
        lines.append(str(footer["text"]))
    elif isinstance(footer, str) and footer:
        lines.append(footer)
    author = data.get("author")
    if isinstance(author, dict) and author.get("name"):
        lines.insert(0, str(author["name"]))
    return "\n".join(line for line in lines if line)


def _outgoing_message(channel: Any, text: str, plugin_id: str) -> Any:
    """合成「刚发出去的那条消息」（上游 ``send`` 返回 ``Message``）。

    懒导入避免 ``abc`` ↔ ``message`` 的循环依赖。
    """
    from .message import Message

    message = Message(
        id=_next_message_id(),
        content=text,
        channel=channel,
        author=getattr(getattr(channel, "guild", None), "me", None),
        guild=getattr(channel, "guild", None),
    )
    message._bridge_plugin_id = plugin_id
    message._bridge_outgoing = True
    return message


_message_counter = 0


def _next_message_id() -> int:
    """本地合成的消息 id（N.E.K.O. 侧消息没有 snowflake，用一个进程内递增的假 id）。"""
    global _message_counter
    _message_counter += 1
    return _message_counter
