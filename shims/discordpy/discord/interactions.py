"""``discord.interactions``：交互对象（只读替身）。

**N.E.K.O. 没有「交互」这一路输入**：没有斜杠命令、没有按钮回调、没有模态框。所以
``Interaction`` 只是为了让插件能写类型注解、能 import 成功；任何真正的交互动作
（``interaction.response.send_message()`` / ``followup.send()`` / ``edit_message()``）
都显式抛 :class:`UnsupportedCapabilityError`，绝不假装发出去了。
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any

from . import _runtime
from .abc import Snowflake
from .enums import try_enum

__all__ = ["Interaction", "InteractionResponse", "InteractionType"]


class InteractionType(IntEnum):
    """交互类型（数值与上游一致）。"""

    ping = 1
    application_command = 2
    component = 3
    autocomplete = 4
    modal_submit = 5


class InteractionResponse:
    """``interaction.response``：所有响应动作都不支持。"""

    def __init__(self, parent: "Interaction") -> None:
        self._parent = parent
        self._responded = False

    def is_done(self) -> bool:
        return self._responded

    async def send_message(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("interaction.response.send_message", "N.E.K.O. 没有交互响应通道")

    async def edit_message(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("interaction.response.edit_message", "N.E.K.O. 没有交互响应通道")

    async def defer(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("interaction.response.defer", "N.E.K.O. 没有交互响应通道")

    async def send_modal(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("interaction.response.send_modal", "N.E.K.O. 没有模态框")

    async def autocomplete(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("interaction.response.autocomplete", "N.E.K.O. 没有自动补全通道")


class Interaction(Snowflake):
    """一次交互（只读替身；实际动作都抛能力缺失）。"""

    def __init__(
        self,
        id: int,  # noqa: A002 — 与上游字段名一致
        *,
        type: Any = InteractionType.application_command,  # noqa: A002
        data: Any = None,
        guild: Any = None,
        channel: Any = None,
        user: Any = None,
        token: str = "",
        command_name: str = "",
        plugin_id: str = "",
    ) -> None:
        super().__init__(id)
        self.type = try_enum(InteractionType, type)
        self.data = data
        self.guild = guild
        self.channel = channel
        self.user = user
        self.token = token
        self.command_name = command_name
        self.response = InteractionResponse(self)
        self._bridge_plugin_id = plugin_id

    @property
    def namespace(self) -> Any:
        """斜杠命令参数（本 shim 恒为空：没有交互输入）。"""
        from .app_commands import Namespace

        return Namespace()

    @property
    def guild_id(self) -> int | None:
        return getattr(self.guild, "id", None)

    @property
    def guild_locale(self) -> None:
        return None

    @property
    def locale(self) -> None:
        return None

    @property
    def permissions(self) -> Any:
        from .permissions import Permissions

        return Permissions.none()

    @property
    def app_permissions(self) -> Any:
        from .permissions import Permissions

        return Permissions.none()

    @property
    def client(self) -> Any:
        return None

    @property
    def followup(self) -> Any:
        raise _runtime.unsupported("Interaction.followup", "N.E.K.O. 没有交互响应通道")

    async def original_response(self) -> Any:
        raise _runtime.unsupported("Interaction.original_response", "N.E.K.O. 没有交互响应通道")

    async def edit_original_response(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Interaction.edit_original_response", "N.E.K.O. 没有交互响应通道")

    async def delete_original_response(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Interaction.delete_original_response", "N.E.K.O. 没有交互响应通道")

    def __repr__(self) -> str:
        return f"<Interaction id={self.id} command={self.command_name!r}>"
