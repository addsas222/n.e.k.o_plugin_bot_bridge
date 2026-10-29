"""``discord.TextChannel`` / ``Thread`` / ``DMChannel`` / 其它频道类型。

只有「能发消息」的载体（文本频道 / 线程 / 私聊）实现了 :class:`discord.abc.Messageable`：
它们的 ``send`` 会把文本收进当前回复。语音 / 分类 / 论坛频道不具备 N.E.K.O. 等价面，
``send`` 显式抛能力缺失，不做无声失败。
"""

from __future__ import annotations

from typing import Any

from . import _runtime
from .abc import GuildChannel, Messageable, PrivateChannel
from .enums import ChannelType

__all__ = [
    "CategoryChannel",
    "DMChannel",
    "ForumChannel",
    "StageChannel",
    "TextChannel",
    "Thread",
    "VoiceChannel",
]


class TextChannel(GuildChannel, Messageable):
    """文本频道。"""

    def __init__(
        self,
        id: int,  # noqa: A002 — 与上游字段名一致
        *,
        name: str = "",
        guild: Any = None,
        position: int = 0,
        topic: str | None = None,
        nsfw: bool = False,
        category_id: int | None = None,
        slowmode_delay: int = 0,
    ) -> None:
        super().__init__(id, name=name, guild=guild, position=position, nsfw=nsfw, category_id=category_id)
        self.topic = topic
        self.slowmode_delay = int(slowmode_delay or 0)
        self.type = ChannelType.text

    @property
    def jump_url(self) -> str:
        """上游返回可点击链接；N.E.K.O. 里没有 Discord 服务器，因此只给标识串。"""
        return f"neko://channel/{self.id}"

    def is_news(self) -> bool:
        return False

    async def create_thread(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("TextChannel.create_thread", "N.E.K.O. 没有线程概念")

    async def archive(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("TextChannel.archive", "N.E.K.O. 没有线程概念")

    async def edit(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("TextChannel.edit", "N.E.K.O. 不承载频道管理动作")

    async def delete(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("TextChannel.delete", "N.E.K.O. 不承载频道管理动作")

    async def set_permissions(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("TextChannel.set_permissions", "N.E.K.O. 没有 Discord 权限模型")


class Thread(GuildChannel, Messageable):
    """线程（N.E.K.O. 里按普通频道处理：发消息可用，线程管理不可用）。"""

    def __init__(
        self,
        id: int,  # noqa: A002 — 与上游字段名一致
        *,
        name: str = "",
        guild: Any = None,
        parent: Any = None,
        owner_id: int | None = None,
        archived: bool = False,
        locked: bool = False,
        type: ChannelType = ChannelType.public_thread,  # noqa: A002 — 与上游字段名一致
    ) -> None:
        super().__init__(id, name=name, guild=guild, nsfw=bool(getattr(parent, "nsfw", False)))
        self.parent = parent
        self.owner_id = owner_id
        self.archived = bool(archived)
        self.locked = bool(locked)
        self.type = type

    @property
    def parent_id(self) -> int | None:
        return getattr(self.parent, "id", None)

    @property
    def mention(self) -> str:
        return f"<#{self.id}>"

    def is_private(self) -> bool:
        return self.type is ChannelType.private_thread

    async def join(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Thread.join", "N.E.K.O. 没有线程概念")

    async def leave(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Thread.leave", "N.E.K.O. 没有线程概念")

    async def edit(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Thread.edit", "N.E.K.O. 没有线程概念")

    async def delete(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Thread.delete", "N.E.K.O. 没有线程概念")


class DMChannel(PrivateChannel, Messageable):
    """私聊频道。"""

    def __init__(self, id: int, *, recipient: Any = None) -> None:  # noqa: A002 — 与上游字段名一致
        super().__init__(id, recipient=recipient)
        self.type = ChannelType.private

    @property
    def name(self) -> str:
        return str(getattr(self.recipient, "name", "") or "")

    @property
    def mention(self) -> str:
        return str(getattr(self.recipient, "mention", "") or "")

    async def edit(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("DMChannel.edit", "N.E.K.O. 不承载频道管理动作")


class CategoryChannel(GuildChannel):
    """分类频道（只能包含频道，不能发消息）。"""

    def __init__(self, id: int, *, name: str = "", guild: Any = None, position: int = 0) -> None:  # noqa: A002
        super().__init__(id, name=name, guild=guild, position=position)
        self.type = ChannelType.category

    @property
    def channels(self) -> list[Any]:
        guild = self.guild
        if guild is None:
            return []
        return [item for item in guild.channels if getattr(item, "category_id", None) == self.id]

    @property
    def text_channels(self) -> list[Any]:
        return [item for item in self.channels if isinstance(item, TextChannel)]

    @property
    def voice_channels(self) -> list[Any]:
        return [item for item in self.channels if isinstance(item, VoiceChannel)]

    async def send(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("CategoryChannel.send", "分类频道不能承载消息")

    async def edit(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("CategoryChannel.edit", "N.E.K.O. 不承载频道管理动作")

    async def delete(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("CategoryChannel.delete", "N.E.K.O. 不承载频道管理动作")


class VoiceChannel(GuildChannel):
    """语音频道（N.E.K.O. 不承载语音）。"""

    def __init__(self, id: int, *, name: str = "", guild: Any = None, position: int = 0, user_limit: int = 0) -> None:  # noqa: A002
        super().__init__(id, name=name, guild=guild, position=position)
        self.type = ChannelType.voice
        self.user_limit = int(user_limit or 0)
        self.members: list[Any] = []

    async def send(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("VoiceChannel.send", "语音频道不能承载文本消息（N.E.K.O. 也不承载语音）")

    async def connect(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("VoiceChannel.connect", "N.E.K.O. 不承载语音连接")


class StageChannel(VoiceChannel):
    """舞台频道（N.E.K.O. 不承载语音）。"""

    def __init__(self, id: int, *, name: str = "", guild: Any = None, position: int = 0, user_limit: int = 0) -> None:  # noqa: A002
        super().__init__(id, name=name, guild=guild, position=position, user_limit=user_limit)
        self.type = ChannelType.stage_voice

    async def create_instance(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("StageChannel.create_instance", "N.E.K.O. 不承载语音")


class ForumChannel(GuildChannel):
    """论坛频道（N.E.K.O. 没有帖子概念）。"""

    def __init__(self, id: int, *, name: str = "", guild: Any = None, position: int = 0, nsfw: bool = False) -> None:  # noqa: A002
        super().__init__(id, name=name, guild=guild, position=position, nsfw=nsfw)
        self.type = ChannelType.forum

    async def send(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("ForumChannel.send", "N.E.K.O. 没有论坛帖子概念")

    async def create_thread(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("ForumChannel.create_thread", "N.E.K.O. 没有论坛帖子概念")
