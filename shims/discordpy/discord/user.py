"""``discord.User`` / ``Member`` / ``ClientUser``。

N.E.K.O. 里没有 Discord 用户数据库：这些对象的字段来自兼容层从 ``CommandContext``
与消息 payload（``mentions`` / ``members`` / ``roles`` / ``channels``）里读到的信息，
读不到就留空，**不伪造**。改动类 API（踢人 / 封禁 / 加角色）一律显式不支持。
"""

from __future__ import annotations

import datetime
from typing import Any

from . import _runtime
from .abc import Snowflake
from .permissions import Permissions

__all__ = ["BaseUser", "ClientUser", "Member", "User"]

#: 默认头像的分母（上游用它算默认头像序号，本 shim 只保证字段存在）。
DEFAULT_AVATAR = ""


class BaseUser(Snowflake):
    """用户基类（``discord.abc.User`` 的最小可用替身）。"""

    def __init__(
        self,
        id: int,  # noqa: A002 — 与上游字段名一致
        *,
        name: str = "",
        discriminator: str = "0",
        global_name: str | None = None,
        bot: bool = False,
        avatar: str | None = None,
        system: bool = False,
        plugin_id: str = "",
    ) -> None:
        super().__init__(id)
        self.name = str(name)
        self.discriminator = str(discriminator or "0")
        self.global_name = None if global_name is None else str(global_name)
        self.bot = bool(bot)
        self.avatar = avatar
        self.system = bool(system)
        self._bridge_plugin_id = plugin_id

    # ---- 展示 ------------------------------------------------------------ #

    @property
    def display_name(self) -> str:
        return self.global_name or self.name

    @property
    def display_avatar(self) -> Any:
        return self.avatar

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    @property
    def default_avatar(self) -> str:
        return DEFAULT_AVATAR

    @property
    def dm_channel(self) -> Any:
        return None

    # ---- 改动作（N.E.K.O. 没有对应面） ----------------------------------- #

    async def create_dm(self) -> Any:
        raise _runtime.unsupported("User.create_dm", "N.E.K.O. 的私聊由宿主会话决定，插件不能自己开私聊")

    async def fetch_message(self, message_id: int) -> Any:
        raise _runtime.unsupported(
            "User.fetch_message", f"N.E.K.O. 不提供按用户翻历史（message id={message_id}）"
        )

    @property
    def mutual_guilds(self) -> list[Any]:
        return []

    def __str__(self) -> str:
        return self.name or str(self.id)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} id={self.id} name={self.name!r} bot={self.bot}>"


class ClientUser(BaseUser):
    """机器人自己（``bot.user``）。

    字段来自桥接配置（``discord_bot_name`` / ``discord_bot_id``）；N.E.K.O. 里没有
    可编辑的「机器人档案」，因此 ``edit`` 显式不支持。
    """

    async def edit(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("ClientUser.edit", "N.E.K.O. 不承载 Discord 客户端档案")


class User(BaseUser):
    """普通用户。"""


class Member(User):
    """服务器成员（``discord.Member`` 的最小可用替身）。"""

    def __init__(
        self,
        id: int,  # noqa: A002 — 与上游字段名一致
        *,
        name: str = "",
        guild: Any = None,
        nick: str | None = None,
        roles: Any = (),
        joined_at: datetime.datetime | None = None,
        discriminator: str = "0",
        global_name: str | None = None,
        bot: bool = False,
        plugin_id: str = "",
    ) -> None:
        super().__init__(
            id,
            name=name,
            discriminator=discriminator,
            global_name=global_name,
            bot=bot,
            plugin_id=plugin_id or getattr(guild, "_bridge_plugin_id", ""),
        )
        self.guild = guild
        self.nick = None if nick is None else str(nick)
        self.roles = [role for role in roles if role is not None]
        self.joined_at = joined_at

    @property
    def display_name(self) -> str:
        return self.nick or self.global_name or self.name

    @property
    def mention(self) -> str:
        return f"<@!{self.id}>" if self.nick else f"<@{self.id}>"

    @property
    def raw_status(self) -> str:
        return "offline"  # N.E.K.O. 不提供在线状态

    @property
    def status(self) -> str:
        return "offline"

    @property
    def top_role(self) -> Any:
        if not self.roles:
            return None
        return max(self.roles, key=lambda role: getattr(role, "position", 0))

    @property
    def colour(self) -> Any:
        role = self.top_role
        return getattr(role, "colour", None)

    @property
    def color(self) -> Any:
        return self.colour

    @property
    def guild_permissions(self) -> Permissions:
        """成员在服务器上的权限。

        N.E.K.O. **没有** Discord 权限数据：载荷里带了 ``permissions`` 就按它算，
        否则返回「无任何位」（并且这不代表用户真的没有权限 —— ``has_permissions``
        检查会在没有数据时如实报能力缺失，见 ``ext.commands.checks``）。
        """
        raw = getattr(self, "_bridge_permissions", None)
        if raw is not None:
            return raw
        return Permissions.none()

    # ---- 改动作 ---------------------------------------------------------- #

    async def add_roles(self, *roles: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Member.add_roles", "N.E.K.O. 没有 Discord 角色模型")

    async def remove_roles(self, *roles: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Member.remove_roles", "N.E.K.O. 没有 Discord 角色模型")

    async def edit(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Member.edit", "N.E.K.O. 没有 Discord 成员档案")

    async def kick(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Member.kick", "N.E.K.O. 不承载服务器管理动作")

    async def ban(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Member.ban", "N.E.K.O. 不承载服务器管理动作")

    async def unban(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Member.unban", "N.E.K.O. 不承载服务器管理动作")

    async def timeout(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Member.timeout", "N.E.K.O. 不承载服务器管理动作")

    async def move_to(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Member.move_to", "N.E.K.O. 不承载语音")

    async def send(self, content: Any = None, **kwargs: Any) -> Any:
        """给成员发私聊（``Member.send``）。

        上游会给用户开一个 DM 频道；N.E.K.O. 里没有「按用户开私聊」的能力，因此
        必须显式失败 —— 假装成功会让人以为消息发出去了。
        """
        del content, kwargs
        raise _runtime.unsupported("Member.send", "N.E.K.O. 不能按用户开私聊；请用 Messageable.send 走当前频道")
