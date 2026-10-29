"""权限（上游 ``nonebot.permission`` 的子集）。

包含 ``Permission`` / ``User`` / ``USER`` / ``SUPERUSER`` 与
``MESSAGE`` / ``NOTICE`` / ``REQUEST`` / ``METAEVENT`` 四个类型权限。
"""

from __future__ import annotations

from nonebot.internal.adapter import Bot, Event
from nonebot.internal.permission import USER as USER
from nonebot.internal.permission import Permission as Permission
from nonebot.internal.permission import User as User
from nonebot.params import EventType

__all__ = [
    "METAEVENT",
    "MESSAGE",
    "NOTICE",
    "REQUEST",
    "SUPERUSER",
    "USER",
    "Message",
    "MetaEvent",
    "Notice",
    "Permission",
    "Request",
    "SuperUser",
    "User",
]


class Message:
    """检查是否为消息事件。"""

    def __repr__(self) -> str:
        return "Message()"

    async def __call__(self, type: str = EventType()) -> bool:  # noqa: A002 — 与上游参数名一致
        return type == "message"


class Notice:
    """检查是否为通知事件。"""

    def __repr__(self) -> str:
        return "Notice()"

    async def __call__(self, type: str = EventType()) -> bool:  # noqa: A002 — 与上游参数名一致
        return type == "notice"


class Request:
    """检查是否为请求事件。"""

    def __repr__(self) -> str:
        return "Request()"

    async def __call__(self, type: str = EventType()) -> bool:  # noqa: A002 — 与上游参数名一致
        return type == "request"


class MetaEvent:
    """检查是否为元事件。"""

    def __repr__(self) -> str:
        return "MetaEvent()"

    async def __call__(self, type: str = EventType()) -> bool:  # noqa: A002 — 与上游参数名一致
        return type == "meta_event"


MESSAGE: Permission = Permission(Message())
"""匹配任意 ``message`` 类型事件。"""
NOTICE: Permission = Permission(Notice())
"""匹配任意 ``notice`` 类型事件。"""
REQUEST: Permission = Permission(Request())
"""匹配任意 ``request`` 类型事件。"""
METAEVENT: Permission = Permission(MetaEvent())
"""匹配任意 ``meta_event`` 类型事件。"""


class SuperUser:
    """检查当前事件是否是消息事件且属于超级管理员。

    ``superusers`` 来自 N.E.K.O. 桥接配置 ``[bridge].nonebot_superusers``（默认空集，
    即默认没有任何超级用户）。上游还会比对 ``<adapter>:<user_id>`` 形式的配置项。
    """

    def __repr__(self) -> str:
        return "Superuser()"

    async def __call__(self, bot: Bot, event: Event) -> bool:
        try:
            user_id = event.get_user_id()
        except Exception:  # noqa: BLE001 — 事件没有用户概念时不匹配（上游同）
            return False
        config = getattr(bot, "config", None)
        superusers = set(getattr(config, "superusers", None) or ())
        adapter_name = ""
        try:
            adapter_name = bot.adapter.get_name().split(maxsplit=1)[0].lower()
        except Exception:  # noqa: BLE001 — 适配器没有名字时退化为纯 user_id 比对
            adapter_name = ""
        return f"{adapter_name}:{user_id}" in superusers or user_id in superusers


SUPERUSER: Permission = Permission(SuperUser())
"""匹配任意超级用户事件。"""
