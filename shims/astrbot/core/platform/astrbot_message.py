"""AstrBot 消息对象（N.E.K.O. shim 子集）。"""

from __future__ import annotations

import time
from dataclasses import dataclass

from ..message.components import BaseMessageComponent
from .message_type import MessageType


@dataclass
class MessageMember:
    """消息发送者。"""

    user_id: str
    nickname: str | None = None

    def __str__(self) -> str:
        return f"User ID: {self.user_id},Nickname: {self.nickname or 'N/A'}"


@dataclass
class Group:
    """群组信息。"""

    group_id: str
    group_name: str | None = None
    group_avatar: str | None = None
    group_owner: str | None = None
    group_admins: list[str] | None = None
    members: list[MessageMember] | None = None
    member_count: int | None = None


class AstrBotMessage:
    """AstrBot 的消息对象（平台适配器下发的消息结构）。"""

    type: MessageType
    self_id: str
    session_id: str
    message_id: str
    group: Group | None
    sender: MessageMember
    message: list[BaseMessageComponent]
    message_str: str
    raw_message: object
    timestamp: int

    def __init__(self) -> None:
        self.timestamp = int(time.time())
        self.group = None

    @property
    def group_id(self) -> str:
        """群号；私聊为空字符串。"""
        if self.group:
            return self.group.group_id
        return ""

    @group_id.setter
    def group_id(self, value: str | None) -> None:
        """设置群号（空值表示私聊）。"""
        if value:
            if self.group:
                self.group.group_id = value
            else:
                self.group = Group(group_id=value)
        else:
            self.group = None

    def __str__(self) -> str:
        return str(self.__dict__)


__all__ = ["AstrBotMessage", "Group", "MessageMember"]
