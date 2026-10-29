"""``discord.ChannelType`` / ``MessageType`` / ``ButtonStyle`` / ``TextStyle`` /
``ComponentType``（上游 ``discord.enums`` 的子集）。

只包含插件做 ``isinstance`` / ``==`` 判断、或写交互组件时最常用的那几个成员；
枚举值与上游逐项一致（含 ``blurple`` / ``grey`` / ``gray`` / ``green`` / ``red``
这些历史别名 —— 很多老 cog 用的是别名）。
"""

from __future__ import annotations

from enum import Enum

__all__ = ["ButtonStyle", "ChannelType", "ComponentType", "MessageType", "TextStyle", "try_enum"]


class ChannelType(Enum):
    """频道类型（值与上游一致）。"""

    text = 0
    private = 1
    voice = 2
    group = 3
    category = 4
    news = 5
    store = 6
    news_thread = 10
    public_thread = 11
    private_thread = 12
    stage_voice = 13
    forum = 15
    directory = 14
    media = 16

    def __str__(self) -> str:
        return self.name


class MessageType(Enum):
    """消息类型（只列常用值，其余按上游数值保留为整数）。"""

    default = 0
    recipient_add = 1
    recipient_remove = 2
    call = 3
    channel_name_change = 4
    channel_icon_change = 5
    pins_add = 6
    new_member = 7
    premium_guild_subscription = 8
    premium_guild_tier_1 = 9
    premium_guild_tier_2 = 10
    premium_guild_tier_3 = 11
    channel_follow_add = 12
    guild_stream = 13
    reply = 19
    application_command = 20
    thread_starter_message = 21
    guild_invite_reminder = 22
    context_menu_command = 23

    def __str__(self) -> str:
        return self.name


class ButtonStyle(Enum):
    """按钮样式（值与上游一致，含历史别名）。"""

    primary = 1
    secondary = 2
    success = 3
    danger = 4
    link = 5
    premium = 6

    # 上游保留的历史别名（老代码常用）
    blurple = 1
    grey = 2
    gray = 2
    green = 3
    red = 4
    url = 5


class TextStyle(Enum):
    """``ui.TextInput`` 的输入框样式（值与上游一致）。"""

    short = 1
    paragraph = 2
    long = 2


class ComponentType(Enum):
    """交互组件类型（值与上游一致）。"""

    action_row = 1
    button = 2
    select = 3
    string_select = 3
    text_input = 4
    user_select = 5
    role_select = 6
    mentionable_select = 7
    channel_select = 8
    section = 9
    text_display = 10
    thumbnail = 11
    media_gallery = 12
    file = 13
    separator = 14
    container = 17
    label = 18
    file_upload = 19
    radio_group = 21
    checkbox_group = 22
    checkbox = 23


def try_enum(enum_cls: type, value: object, default: object = None) -> object:
    """按上游 ``try_enum`` 语义转换：未知值回落到 ``default``（默认原值）。"""
    try:
        return enum_cls(value)  # type: ignore[call-arg]
    except ValueError:
        return value if default is None else default
