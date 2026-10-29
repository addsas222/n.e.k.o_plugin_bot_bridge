"""单文件形态的 Hoshino 插件 fixture（``hoshino/modules/<name>.py`` 那种布局）。"""

from __future__ import annotations

from hoshino import Service
from hoshino.typing import CQEvent

sf_sv = Service("single_file_hoshino", help_="单文件插件：sfping / sfecho")


@sf_sv.on_command("sfping")
async def sfping(bot, ev: CQEvent):
    """单文件插件的命令"""
    await bot.send(ev, "sfpong")


@sf_sv.on_keyword("sfecho")
async def sfecho(bot, ev: CQEvent):
    """单文件插件的关键词触发器"""
    await bot.send(ev, "sfecho ok")
