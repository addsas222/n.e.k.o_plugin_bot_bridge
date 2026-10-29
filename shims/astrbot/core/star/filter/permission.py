"""权限过滤器（N.E.K.O. shim 子集，与上游同语义）。"""

from __future__ import annotations

import enum

from ...config import AstrBotConfig
from ...platform.astr_message_event import AstrMessageEvent
from . import HandlerFilter


class PermissionType(enum.Flag):
    """指令权限；MEMBER 同时允许管理员。"""

    ADMIN = enum.auto()
    MEMBER = enum.auto()
    GROUP_ADMIN = enum.auto()
    SHARED_GROUP_ADMIN = enum.auto()


COMMAND_PERMISSION_TYPES = {
    "admin": PermissionType.ADMIN,
    "member": PermissionType.MEMBER,
    "group_admin": PermissionType.GROUP_ADMIN,
    "shared_group_admin": PermissionType.SHARED_GROUP_ADMIN,
}


class PermissionTypeFilter(HandlerFilter):
    """按发送者权限过滤。"""

    def __init__(
        self,
        permission_type: PermissionType,
        raise_error: bool = True,
    ) -> None:
        self.permission_type = permission_type
        self.raise_error = raise_error

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """检查发送者在当前会话中的权限。"""
        del cfg
        if self.permission_type == PermissionType.ADMIN or (
            self.permission_type == PermissionType.GROUP_ADMIN and event.get_group_id()
        ):
            return event.is_admin()
        if (
            self.permission_type == PermissionType.SHARED_GROUP_ADMIN
            and event.get_group_id()
        ):
            return event.is_admin() or bool(event.get_extra("_session_isolated", False))
        return True


__all__ = ["COMMAND_PERMISSION_TYPES", "PermissionType", "PermissionTypeFilter"]
