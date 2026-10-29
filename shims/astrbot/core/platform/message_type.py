"""AstrBot 消息类型枚举（N.E.K.O. shim 子集，值与上游一致）。"""

from __future__ import annotations

from enum import Enum


class MessageType(Enum):
    """消息类型。"""

    GROUP_MESSAGE = "GroupMessage"
    """群组形式的消息"""
    FRIEND_MESSAGE = "FriendMessage"
    """私聊、好友等单聊消息"""
    OTHER_MESSAGE = "OtherMessage"
    """其他类型的消息，如系统消息"""


__all__ = ["MessageType"]
