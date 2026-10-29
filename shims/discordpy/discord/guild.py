"""``discord.Guild`` / ``Role``。

服务器对象是**兼容层按一次入站消息的上下文合成的**：名字 / id 来自桥接配置与
``CommandContext.raw``，成员 / 频道 / 角色的清单来自 payload（没有就是空列表）。
所有服务器管理动作（建频道、改角色、封禁…）在 N.E.K.O. 里都没有对应面，显式不支持。
"""

from __future__ import annotations

from typing import Any

from . import _runtime
from .abc import Snowflake
from .permissions import Permissions

__all__ = ["Guild", "Role"]


class Role(Snowflake):
    """服务器角色（只读替身）。"""

    def __init__(
        self,
        id: int,  # noqa: A002 — 与上游字段名一致
        *,
        name: str = "",
        guild: Any = None,
        position: int = 0,
        permissions: Any = None,
        colour: Any = None,
        hoist: bool = False,
        managed: bool = False,
        mentionable: bool = False,
        is_default: bool = False,
    ) -> None:
        super().__init__(id)
        self.name = str(name)
        self.guild = guild
        self.position = int(position)
        self.permissions = permissions if isinstance(permissions, Permissions) else Permissions.none()
        self.colour = colour
        self.hoist = bool(hoist)
        self.managed = bool(managed)
        self.mentionable = bool(mentionable)
        self.is_default = bool(is_default)

    @property
    def color(self) -> Any:
        return self.colour

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"

    @property
    def created_at(self) -> Any:
        return super().created_at

    def is_bot_managed(self) -> bool:
        return self.managed

    def is_integration(self) -> bool:
        return False

    async def edit(self, *args: Any, **kwargs: Any) -> "Role":
        raise _runtime.unsupported("Role.edit", "N.E.K.O. 没有 Discord 角色模型")

    async def delete(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Role.delete", "N.E.K.O. 没有 Discord 角色模型")

    def __lt__(self, other: "Role") -> bool:
        if not isinstance(other, Role) or self.guild != other.guild:
            raise TypeError("Cannot compare roles from different guilds")
        return self.position < other.position

    def __le__(self, other: "Role") -> bool:
        if not isinstance(other, Role) or self.guild != other.guild:
            raise TypeError("Cannot compare roles from different guilds")
        return self.position <= other.position

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<Role id={self.id} name={self.name!r}>"


class Guild(Snowflake):
    """服务器（兼容层按消息上下文合成）。"""

    def __init__(
        self,
        id: int,  # noqa: A002 — 与上游字段名一致
        *,
        name: str = "",
        owner_id: int | None = None,
        members: Any = (),
        channels: Any = (),
        roles: Any = (),
        me: Any = None,
        plugin_id: str = "",
    ) -> None:
        super().__init__(id)
        self.name = str(name)
        self.owner_id = owner_id
        self._members = list(members)
        self._channels = list(channels)
        self._roles = list(roles)
        self.me = me
        self._bridge_plugin_id = plugin_id
        self.icon = None
        self.banner = None
        self.description = None
        self.owner = None

    # ---- 视图 ------------------------------------------------------------ #

    @property
    def members(self) -> list[Any]:
        return list(self._members)

    @property
    def channels(self) -> list[Any]:
        return list(self._channels)

    @property
    def roles(self) -> list[Any]:
        return list(self._roles)

    @property
    def text_channels(self) -> list[Any]:
        return [item for item in self._channels if getattr(item, "type", None) is not None and item.type.name == "text"]

    @property
    def voice_channels(self) -> list[Any]:
        return [item for item in self._channels if getattr(item, "type", None) is not None and item.type.name == "voice"]

    @property
    def categories(self) -> list[Any]:
        return [item for item in self._channels if getattr(item, "type", None) is not None and item.type.name == "category"]

    @property
    def member_count(self) -> int:
        return len(self._members)

    @property
    def default_role(self) -> Any:
        for role in self._roles:
            if getattr(role, "is_default", False):
                return role
        return None

    @property
    def icon_url(self) -> str:
        return ""

    @property
    def banner_url(self) -> str:
        return ""

    def get_member(self, user_id: int) -> Any:
        for member in self._members:
            if member.id == int(user_id):
                return member
        return None

    def get_member_named(self, name: str) -> Any:
        for member in self._members:
            if name in (member.name, member.display_name, str(member)):
                return member
        return None

    def get_channel(self, channel_id: int) -> Any:
        for channel in self._channels:
            if channel.id == int(channel_id):
                return channel
        return None

    def get_role(self, role_id: int) -> Any:
        for role in self._roles:
            if role.id == int(role_id):
                return role
        return None

    def get_thread(self, thread_id: int) -> Any:
        return self.get_channel(thread_id)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return f"<Guild id={self.id} name={self.name!r} members={len(self._members)}>"

    # ---- 动作（N.E.K.O. 没有对应面） ------------------------------------- #

    async def fetch_member(self, user_id: int) -> Any:
        member = self.get_member(user_id)
        if member is not None:
            return member
        raise _runtime.unsupported(
            "Guild.fetch_member", f"N.E.K.O. 不提供成员查询接口（只在载荷里带了成员时可见）：user id={user_id}"
        )

    async def fetch_members(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Guild.fetch_members", "N.E.K.O. 不提供成员清单接口")

    async def fetch_channels(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Guild.fetch_channels", "N.E.K.O. 不提供频道清单接口")

    async def fetch_roles(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Guild.fetch_roles", "N.E.K.O. 没有 Discord 角色模型")

    async def edit(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Guild.edit", "N.E.K.O. 不承载服务器管理动作")

    async def create_text_channel(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Guild.create_text_channel", "N.E.K.O. 不承载服务器管理动作")

    async def create_role(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Guild.create_role", "N.E.K.O. 没有 Discord 角色模型")

    async def ban(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Guild.ban", "N.E.K.O. 不承载服务器管理动作")

    async def kick(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Guild.kick", "N.E.K.O. 不承载服务器管理动作")

    async def prune_members(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Guild.prune_members", "N.E.K.O. 不承载服务器管理动作")
