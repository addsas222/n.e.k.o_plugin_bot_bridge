"""``astrbot.api.all`` —— 上游那个「一行导入全部插件面」的聚合模块。

上游 ``astrbot/api/all.py`` 是插件模板的默认首行：

    from astrbot.api.all import *

它把 event / star / filter / message_components / provider 全部摊平到同一个命名空间。
shim 这里按**同样的名字、同样的来源顺序**重导出能兑现的部分：

* 已兑现：``filter`` / ``command`` / ``command_group`` / ``regex`` /
  ``event_message_type`` / ``platform_adapter_type`` / ``llm_tool`` / ``register``、
  ``Star`` / ``Context`` / ``AstrMessageEvent``、``MessageEventResult`` /
  ``MessageChain`` / ``CommandResult`` / ``EventResultType``、
  ``message_components`` 全量、``Provider`` / ``ProviderMetaData`` / ``LLMResponse``、
  ``AstrBotConfig`` / ``html_renderer`` / ``logger``；
* 不兑现（上游有、N.E.K.O. 无等价物）：``Personality``（``astrbot.core.db.po``）、
  ``Platform``（平台适配器基类）、``astrbot.core.star.config`` 的全局配置对象。
  这三个**故意不导出** —— 与其给个能 import 的空壳让插件在运行期才发现不对，
  不如让它在这一行就 fail fast，错误信息也更准确。
"""

# ruff: noqa: F401, F403
from __future__ import annotations

from astrbot import logger
from astrbot.core import html_renderer
from astrbot.core.config import AstrBotConfig
from astrbot.core.message.message_event_result import (
    CommandResult,
    EventResultType,
    MessageChain,
    MessageEventResult,
)
from astrbot.core.platform import (
    AstrMessageEvent,
    AstrBotMessage,
    MessageMember,
    MessageType,
    PlatformMetadata,
)
from astrbot.core.provider import LLMResponse, Provider, ProviderMetaData
from astrbot.core.star import Context, Star
from astrbot.core.star.filter.event_message_type import (
    EventMessageType,
    EventMessageTypeFilter,
)
from astrbot.core.star.filter.platform_adapter_type import (
    PlatformAdapterType,
    PlatformAdapterTypeFilter,
)
from astrbot.core.star.register import (
    register_command as command,
)
from astrbot.core.star.register import (
    register_command_group as command_group,
)
from astrbot.core.star.register import (
    register_event_message_type as event_message_type,
)
from astrbot.core.star.register import (
    register_llm_tool as llm_tool,
)
from astrbot.core.star.register import (
    register_platform_adapter_type as platform_adapter_type,
)
from astrbot.core.star.register import (
    register_regex as regex,
)
from astrbot.core.star.register import (
    register_star as register,
)

from astrbot.api.event import filter as filter
from astrbot.api.message_components import *  # noqa: F403
