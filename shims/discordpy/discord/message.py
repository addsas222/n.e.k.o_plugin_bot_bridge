"""``discord.Message`` / ``Attachment`` / ``Reaction``。

消息对象由兼容层按 ``CommandContext`` 合成：``content`` / ``author`` / ``channel`` /
``guild`` 是真实语义，``attachments`` / ``embeds`` / ``mentions`` 只在 payload 里带了
数据时才有内容（读不到就是空，不伪造）。``Message.reply`` 是插件回话的主要入口，
走 ``Messageable.send`` 的回复收集；编辑 / 删除 / 加表情等动作没有 N.E.K.O. 对应面。
"""

from __future__ import annotations

import datetime
from typing import Any

from . import _runtime
from .abc import Messageable, Snowflake
from .enums import MessageType

__all__ = ["Attachment", "Message", "Reaction"]


class Attachment(Snowflake):
    """一条消息里的附件（只读元数据）。"""

    def __init__(
        self,
        id: int,  # noqa: A002 — 与上游字段名一致
        *,
        filename: str = "",
        url: str = "",
        size: int = 0,
        content_type: str | None = None,
        description: str | None = None,
        width: int | None = None,
        height: int | None = None,
        proxy_url: str = "",
    ) -> None:
        super().__init__(id)
        self.filename = str(filename)
        self.url = str(url)
        self.proxy_url = str(proxy_url)
        self.size = int(size or 0)
        self.content_type = content_type
        self.description = description
        self.width = width
        self.height = height

    @property
    def content_scan_version(self) -> int:
        return 0

    def is_spoiler(self) -> bool:
        return self.filename.startswith("SPOILER_")

    async def read(self, *args: Any, **kwargs: Any) -> bytes:
        raise _runtime.unsupported("Attachment.read", "N.E.K.O. 不代理 Discord CDN 下载")

    async def save(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Attachment.save", "N.E.K.O. 不代理 Discord CDN 下载")

    async def to_file(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Attachment.to_file", "N.E.K.O. 不代理 Discord CDN 下载")

    def __str__(self) -> str:
        return self.url

    def __repr__(self) -> str:
        return f"<Attachment id={self.id} filename={self.filename!r} size={self.size}>"


class Reaction:
    """一条消息上的一个表情反应（只读）。"""

    def __init__(self, *, emoji: Any, count: int = 0, me: bool = False) -> None:
        self.emoji = emoji
        self.count = int(count)
        self.me = bool(me)

    async def users(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Reaction.users", "N.E.K.O. 不提供反应用户清单")

    def __repr__(self) -> str:
        return f"<Reaction emoji={self.emoji!r} count={self.count}>"


class Message(Snowflake):
    """一条消息（兼容层合成的替身 + ``send`` 的返回值）。"""

    def __init__(
        self,
        id: int,  # noqa: A002 — 与上游字段名一致
        *,
        content: str = "",
        channel: Any = None,
        author: Any = None,
        guild: Any = None,
        type: Any = MessageType.default,  # noqa: A002 — 与上游字段名一致
        created_at: datetime.datetime | None = None,
        attachments: Any = (),
        embeds: Any = (),
        mentions: Any = (),
        role_mentions: Any = (),
        channel_mentions: Any = (),
        reactions: Any = (),
        mention_everyone: bool = False,
        reference: Any = None,
        edited_at: datetime.datetime | None = None,
        plugin_id: str = "",
    ) -> None:
        super().__init__(id)
        self.content = str(content or "")
        self.channel = channel
        self.author = author
        self.guild = guild
        self.type = type
        self._created_at = created_at
        self.attachments = list(attachments)
        self.embeds = list(embeds)
        self.mentions = list(mentions)
        self.role_mentions = list(role_mentions)
        self.channel_mentions = list(channel_mentions)
        self.reactions = list(reactions)
        self.mention_everyone = bool(mention_everyone)
        self.reference = reference
        self.edited_at = edited_at
        self.flags = None
        self.pinned = False
        self.tts = False
        self.nonce = None
        self._bridge_plugin_id = plugin_id or getattr(channel, "_bridge_plugin_id", "")
        self._bridge_outgoing = False

    # ---- 视图 ------------------------------------------------------------ #

    @property
    def created_at(self) -> Any:
        if self._created_at is not None:
            return self._created_at
        return super().created_at

    @property
    def jump_url(self) -> str:
        return f"neko://message/{self.id}"

    @property
    def system_content(self) -> str:
        return self.content

    @property
    def clean_content(self) -> str:
        """去掉提及标记的纯文本（与上游 ``clean_content`` 同义）。"""
        text = self.content
        for member in list(self.mentions) + list(self.role_mentions):
            mention = getattr(member, "mention", "")
            if mention:
                text = text.replace(mention, f"@{getattr(member, 'display_name', '')}")
        return text

    # ---- 发言（可用） ---------------------------------------------------- #

    async def reply(self, content: Any = None, **kwargs: Any) -> Any:
        """回复这条消息（N.E.K.O. 里没有引用概念，等价于在当前频道发言）。"""
        target = self.channel
        if target is None or not isinstance(target, Messageable):
            raise _runtime.unsupported("Message.reply", "这条消息没有可回复的频道")
        kwargs.pop("mention_author", None)
        return await target.send(content, **kwargs)

    async def add_reaction(self, emoji: Any) -> None:
        raise _runtime.unsupported("Message.add_reaction", "N.E.K.O. 不承载表情反应")

    async def remove_reaction(self, emoji: Any, member: Any = None) -> None:
        raise _runtime.unsupported("Message.remove_reaction", "N.E.K.O. 不承载表情反应")

    async def clear_reactions(self) -> None:
        raise _runtime.unsupported("Message.clear_reactions", "N.E.K.O. 不承载表情反应")

    async def edit(self, **fields: Any) -> "Message":
        raise _runtime.unsupported("Message.edit", "N.E.K.O. 不允许改写已经进对话的消息")

    async def delete(self, *, delay: float | None = None) -> None:
        raise _runtime.unsupported("Message.delete", "N.E.K.O. 不允许删除已经进对话的消息")

    async def pin(self, *, reason: Any = None) -> None:
        raise _runtime.unsupported("Message.pin", "N.E.K.O. 没有置顶概念")

    async def unpin(self, *, reason: Any = None) -> None:
        raise _runtime.unsupported("Message.unpin", "N.E.K.O. 没有置顶概念")

    async def publish(self) -> None:
        raise _runtime.unsupported("Message.publish", "N.E.K.O. 没有公告频道概念")

    async def create_thread(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Message.create_thread", "N.E.K.O. 没有线程概念")

    async def fetch(self, *args: Any, **kwargs: Any) -> "Message":
        return self

    def to_reference(self, *args: Any, **kwargs: Any) -> Any:
        return None

    def __repr__(self) -> str:
        return f"<Message id={self.id} author={getattr(self.author, 'name', None)!r} content={self.content[:32]!r}>"
