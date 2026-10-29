"""事件监听注册器（N.E.K.O. shim 子集）。

``from astrbot.api.event import filter`` 后即可使用 ``filter.command`` /
``filter.llm_tool`` / ``filter.on_astrbot_loaded`` 等装饰器；名称与上游
``astrbot/api/event/filter/__init__.py`` 完全一致（``agent`` 除外，见后端差异表）。
"""

from __future__ import annotations

from ....core.star.filter.custom_filter import CustomFilter
from ....core.star.filter.event_message_type import (
    EventMessageType,
    EventMessageTypeFilter,
)
from ....core.star.filter.permission import PermissionType, PermissionTypeFilter
from ....core.star.filter.platform_adapter_type import (
    PlatformAdapterType,
    PlatformAdapterTypeFilter,
)
from ....core.star.register import (
    register_after_message_sent as after_message_sent,
)
from ....core.star.register import register_command as command
from ....core.star.register import register_command_group as command_group
from ....core.star.register import register_custom_filter as custom_filter
from ....core.star.register import register_event_message_type as event_message_type
from ....core.star.register import register_llm_tool as llm_tool
from ....core.star.register import register_on_agent_begin as on_agent_begin
from ....core.star.register import register_on_agent_done as on_agent_done
from ....core.star.register import register_on_astrbot_loaded as on_astrbot_loaded
from ....core.star.register import (
    register_on_decorating_result as on_decorating_result,
)
from ....core.star.register import register_on_llm_request as on_llm_request
from ....core.star.register import register_on_llm_response as on_llm_response
from ....core.star.register import register_on_llm_tool_respond as on_llm_tool_respond
from ....core.star.register import register_on_plugin_error as on_plugin_error
from ....core.star.register import register_on_plugin_loaded as on_plugin_loaded
from ....core.star.register import register_on_plugin_unloaded as on_plugin_unloaded
from ....core.star.register import register_on_platform_loaded as on_platform_loaded
from ....core.star.register import register_on_using_llm_tool as on_using_llm_tool
from ....core.star.register import (
    register_on_waiting_llm_request as on_waiting_llm_request,
)
from ....core.star.register import register_permission_type as permission_type
from ....core.star.register import register_platform_adapter_type as platform_adapter_type
from ....core.star.register import register_regex as regex

__all__ = [
    "CustomFilter",
    "EventMessageType",
    "EventMessageTypeFilter",
    "PermissionType",
    "PermissionTypeFilter",
    "PlatformAdapterType",
    "PlatformAdapterTypeFilter",
    "after_message_sent",
    "command",
    "command_group",
    "custom_filter",
    "event_message_type",
    "llm_tool",
    "on_agent_begin",
    "on_agent_done",
    "on_astrbot_loaded",
    "on_decorating_result",
    "on_llm_request",
    "on_llm_response",
    "on_llm_tool_respond",
    "on_plugin_error",
    "on_plugin_loaded",
    "on_plugin_unloaded",
    "on_platform_loaded",
    "on_using_llm_tool",
    "on_waiting_llm_request",
    "permission_type",
    "platform_adapter_type",
    "regex",
]
