"""示例 AstrBot 插件 —— 用来真机验证 bot_bridge 的 AstrBot 兼容层。

写法与 AstrBot 官方最小示例一致：继承 ``Star``，用 ``@filter.command`` 注册命令，
在异步生成器里 ``yield event.plain_result(...)``。
"""

from __future__ import annotations

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star


class HelloAstrBot(Star):
    def __init__(self, context: Context):
        super().__init__(context)

    @filter.command("helloworld")
    async def helloworld(self, event: AstrMessageEvent):
        """打个招呼（示例命令）。"""
        user_name = event.get_sender_name()
        logger.info("helloworld 命令被触发：%s", event.message_str)
        yield event.plain_result(f"Hello, {user_name}!（来自 AstrBot 插件）")

    @filter.command("astr_info")
    async def astr_info(self, event: AstrMessageEvent) -> MessageEventResult:
        """返回一段插件自述。"""
        return event.plain_result("我是 hello_astrbot，一个跑在 N.E.K.O. 里的 AstrBot 插件。")

    async def terminate(self) -> None:
        logger.info("hello_astrbot 已停止")
