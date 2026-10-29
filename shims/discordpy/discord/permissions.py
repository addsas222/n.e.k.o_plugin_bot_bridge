"""``discord.Permissions``。

N.E.K.O. **没有** Discord 的角色 / 权限模型，所以这里只提供位的表示与运算：
真实权限数据从哪里来由兼容层决定（``CommandContext.raw`` 里带
``permissions`` / ``bot_permissions`` 时按它判断，没带就如实报「能力缺失」）。
"""

from __future__ import annotations

from .flags import BaseFlags

__all__ = ["Permissions"]


class Permissions(BaseFlags):
    """Discord 权限位。"""

    _VALUES = {
        "create_instant_invite": 1 << 0,
        "kick_members": 1 << 1,
        "ban_members": 1 << 2,
        "administrator": 1 << 3,
        "manage_channels": 1 << 4,
        "manage_guild": 1 << 5,
        "add_reactions": 1 << 6,
        "view_audit_log": 1 << 7,
        "priority_speaker": 1 << 8,
        "stream": 1 << 9,
        "read_messages": 1 << 10,
        "view_channel": 1 << 10,
        "send_messages": 1 << 11,
        "send_tts_messages": 1 << 12,
        "manage_messages": 1 << 13,
        "embed_links": 1 << 14,
        "attach_files": 1 << 15,
        "read_message_history": 1 << 16,
        "mention_everyone": 1 << 17,
        "external_emojis": 1 << 18,
        "use_external_emojis": 1 << 18,
        "view_guild_insights": 1 << 19,
        "connect": 1 << 20,
        "speak": 1 << 21,
        "mute_members": 1 << 22,
        "deafen_members": 1 << 23,
        "move_members": 1 << 24,
        "use_voice_activation": 1 << 25,
        "change_nickname": 1 << 26,
        "manage_nicknames": 1 << 27,
        "manage_roles": 1 << 28,
        "manage_permissions": 1 << 28,
        "manage_webhooks": 1 << 29,
        "manage_guild_expressions": 1 << 30,
        "manage_emojis": 1 << 30,
        "use_application_commands": 1 << 31,
        "request_to_speak": 1 << 32,
        "manage_events": 1 << 33,
        "manage_threads": 1 << 34,
        "create_public_threads": 1 << 35,
        "create_private_threads": 1 << 36,
        "external_stickers": 1 << 37,
        "use_external_stickers": 1 << 37,
        "send_messages_in_threads": 1 << 38,
        "use_embedded_activities": 1 << 39,
        "moderate_members": 1 << 40,
        "use_soundboard": 1 << 42,
        "create_expressions": 1 << 43,
        "create_events": 1 << 44,
        "use_external_sounds": 1 << 45,
        "send_voice_messages": 1 << 46,
    }

    #: 标准频道权限（``discord.Permissions.text()``）。
    @classmethod
    def text(cls) -> "Permissions":
        return cls(
            view_channel=True,
            send_messages=True,
            send_tts_messages=True,
            manage_messages=True,
            embed_links=True,
            attach_files=True,
            read_message_history=True,
            mention_everyone=True,
            use_external_emojis=True,
            add_reactions=True,
            use_application_commands=True,
            manage_threads=True,
            send_messages_in_threads=True,
            create_public_threads=True,
            create_private_threads=True,
        )

    @classmethod
    def voice(cls) -> "Permissions":
        return cls(
            view_channel=True,
            connect=True,
            speak=True,
            mute_members=True,
            deafen_members=True,
            move_members=True,
            use_voice_activation=True,
            priority_speaker=True,
            stream=True,
            use_soundboard=True,
            use_external_sounds=True,
        )

    def is_subset(self, other: "Permissions") -> bool:
        """``other`` 是否覆盖了本实例的所有位。"""
        return (self.value & ~other.value) == 0
