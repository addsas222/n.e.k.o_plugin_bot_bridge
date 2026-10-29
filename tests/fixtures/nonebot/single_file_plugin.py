"""单文件形态的 NoneBot2 插件 fixture（桥接层要能识别「目录」和「单文件」两种布局）。"""

from __future__ import annotations

from nonebot import on_command, on_keyword

sfping = on_command("sfping", priority=5)
"""最简单的命令响应器。"""


@sfping.handle()
async def sfping_handler() -> None:
    await sfping.finish("sfpong")


sfecho = on_keyword({"sfecho"}, priority=6)


@sfecho.handle()
async def sfecho_handler() -> None:
    await sfecho.finish("sfecho ok")
