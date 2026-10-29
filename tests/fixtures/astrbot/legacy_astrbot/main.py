"""bot_bridge 的 AstrBot 兼容层测试插件（旧版写法）。

不使用 ``Star`` 基类、也不使用装饰器：

* 主类名以 ``Plugin`` 结尾（上游 v3.4 之前靠类名识别插件主类）；
* 用已废弃的 ``@register`` 写元数据（``metadata.yaml`` 优先）；
* 在 ``__init__`` 里用 ``context.register_commands`` / ``context.register_llm_tool``
  注册指令与工具。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, register


@register(
    name="legacy_astrbot_decorator",
    author="decorator-author",
    desc="装饰器里的描述（应当被 metadata.yaml 覆盖）",
    version="0.0.0",
)
class LegacyHelloPlugin:
    """旧版插件主类（不继承 Star）。"""

    def __init__(self, context: Context) -> None:
        self.context = context
        self.calls: list[str] = []
        context.register_commands(
            "legacy_astrbot",
            "legacy",
            "旧接口注册的指令",
            10,
            self.legacy,
        )
        context.register_commands(
            "legacy_astrbot",
            "legacyre",
            "旧接口注册的正则指令",
            10,
            self.legacy_re,
            use_regex=True,
        )
        context.register_llm_tool(
            "legacy_time",
            [],
            "返回一段固定文本",
            self.legacy_time,
        )

    async def legacy(self, event: AstrMessageEvent) -> MessageEventResult:
        """旧接口指令（返回 MessageEventResult）。"""
        self.calls.append("legacy")
        logger.info("legacy 指令被触发")
        return event.plain_result("legacy ok")

    async def legacy_re(self, event: AstrMessageEvent) -> MessageEventResult:
        """旧接口正则指令。"""
        self.calls.append("legacy_re")
        return event.plain_result("legacy regex ok")

    async def legacy_time(self, event: AstrMessageEvent) -> str:
        """旧接口 LLM 工具。"""
        del event
        return "legacy-time: 12:00"
