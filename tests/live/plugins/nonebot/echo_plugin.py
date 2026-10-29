"""示例 NoneBot2 插件（单文件）—— 用来真机验证 bot_bridge 的 NoneBot2 兼容层。

写法与标准 NoneBot2 插件一致：``on_command`` + ``Matcher.handle`` + ``CommandArg``。
"""

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, Event, Message
from nonebot.params import CommandArg

echo = on_command("echo", aliases={"复读"}, priority=5, block=True)


@echo.handle()
async def handle_echo(bot: Bot, event: Event, args: Message = CommandArg()) -> None:
    text = args.extract_plain_text().strip()
    if not text:
        await echo.finish("要复读点什么？")
    await echo.finish(f"复读：{text}")
