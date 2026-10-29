"""OneBot v11 权限辅助（上游 ``onebot.v11.permission`` 的子集，判定逻辑一致）。"""

from __future__ import annotations

from nonebot.permission import Permission

from .event import GroupMessageEvent, PrivateMessageEvent

__all__ = [
    "GROUP",
    "GROUP_ADMIN",
    "GROUP_MEMBER",
    "GROUP_OWNER",
    "PRIVATE",
    "PRIVATE_FRIEND",
    "PRIVATE_GROUP",
    "PRIVATE_OTHER",
]


async def _private(event: PrivateMessageEvent) -> bool:
    return True


async def _private_friend(event: PrivateMessageEvent) -> bool:
    return event.sub_type == "friend"


async def _private_group(event: PrivateMessageEvent) -> bool:
    return event.sub_type == "group"


async def _private_other(event: PrivateMessageEvent) -> bool:
    return event.sub_type == "other"


PRIVATE: Permission = Permission(_private)
"""匹配任意私聊消息类型事件。"""
PRIVATE_FRIEND: Permission = Permission(_private_friend)
"""匹配好友私聊消息类型事件。"""
PRIVATE_GROUP: Permission = Permission(_private_group)
"""匹配群临时私聊消息类型事件。"""
PRIVATE_OTHER: Permission = Permission(_private_other)
"""匹配其它私聊消息类型事件。"""


async def _group(event: GroupMessageEvent) -> bool:
    return True


def _sender_role(event: GroupMessageEvent) -> str | None:
    sender = getattr(event, "sender", None)
    return getattr(sender, "role", None) if sender is not None else None


async def _group_member(event: GroupMessageEvent) -> bool:
    return _sender_role(event) == "member"


async def _group_admin(event: GroupMessageEvent) -> bool:
    return _sender_role(event) == "admin"


async def _group_owner(event: GroupMessageEvent) -> bool:
    return _sender_role(event) == "owner"


GROUP: Permission = Permission(_group)
"""匹配任意群聊消息类型事件。"""
GROUP_MEMBER: Permission = Permission(_group_member)
"""匹配群员群聊消息类型事件（不含管理员与群主）。"""
GROUP_ADMIN: Permission = Permission(_group_admin)
"""匹配群管理员群聊消息类型事件。"""
GROUP_OWNER: Permission = Permission(_group_owner)
"""匹配群主群聊消息类型事件。"""
