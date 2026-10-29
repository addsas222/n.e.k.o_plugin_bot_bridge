"""``discord.Intents``。

N.E.K.O. 里没有 Discord 网关，因此 intent **不启用任何特权、也不影响任何行为**：
它只是被如实记录下来（插件读 ``intents.message_content`` 能拿到它自己设的值），
让按上游写法构造客户端的插件不用改代码。位定义与上游一致。
"""

from __future__ import annotations

from .flags import BaseFlags

__all__ = ["Intents"]


class Intents(BaseFlags):
    """Discord intent 位（无行为的忠实替身）。"""

    _VALUES = {
        "guilds": 1 << 0,
        "members": 1 << 1,
        "moderation": 1 << 2,
        "emojis_and_stickers": 1 << 3,
        "integrations": 1 << 4,
        "guild_integrations": 1 << 4,
        "webhooks": 1 << 5,
        "guild_webhooks": 1 << 5,
        "invites": 1 << 6,
        "guild_invites": 1 << 6,
        "voice_states": 1 << 7,
        "guild_voice_states": 1 << 7,
        "presences": 1 << 8,
        "guild_presences": 1 << 8,
        "messages": 1 << 9,
        "guild_messages": 1 << 9,
        "reactions": 1 << 10,
        "guild_reactions": 1 << 10,
        "typing": 1 << 11,
        "guild_typing": 1 << 11,
        "dm_messages": 1 << 12,
        "dm_reactions": 1 << 13,
        "dm_typing": 1 << 14,
        "message_content": 1 << 15,
        "guild_scheduled_events": 1 << 16,
        "automod_configuration": 1 << 20,
        "automod_execution": 1 << 21,
    }

    DEFAULT_VALUE = 1 << 0  # guilds

    @classmethod
    def none(cls) -> "Intents":
        """所有 intent 关闭。"""
        return cls()

    @classmethod
    def default(cls) -> "Intents":
        """所有非特权 intent（与上游 ``Intents.default()`` 一致）。"""
        return cls(
            guilds=True,
            guild_messages=True,
            guild_reactions=True,
            guild_typing=True,
            guild_scheduled_events=True,
            integrations=True,
            invites=True,
            voice_states=True,
            emojis_and_stickers=True,
            dm_messages=True,
            dm_reactions=True,
            dm_typing=True,
        )

    @classmethod
    def all(cls) -> "Intents":
        """所有 intent（含特权）。"""
        instance = cls()
        for bit in cls._VALUES.values():
            instance.value = instance.value | bit
        return instance

    @classmethod
    def _from_value(cls, value: int) -> "Intents":
        instance = cls()
        instance.value = int(value)
        return instance

    @property
    def emojis(self) -> bool:
        """``emojis_and_stickers`` 的上游别名。"""
        return bool(self.value & self._VALUES["emojis_and_stickers"])

    @emojis.setter
    def emojis(self, value: bool) -> None:
        self.emojis_and_stickers = value

    @property
    def audit_log(self) -> bool:
        """上游历史别名（等价于 ``moderation``）。"""
        return bool(self.value & self._VALUES["moderation"])
