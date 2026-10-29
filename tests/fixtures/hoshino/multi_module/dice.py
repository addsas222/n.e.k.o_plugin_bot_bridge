"""多文件模块目录（**没有** ``__init__.py``）里的一个文件。

上游 ``nonebot.load_plugins`` 会把 ``hoshino/modules/`` 下的每个 ``*.py`` 都导入，
所以这种"一个目录里散着若干模块"的布局很常见（上游自带的 ``dice/dice.py`` 就是这样）。
兼容层应当把同目录的每个顶层模块都加载，并归到同一个 ``LoadedPlugin``。
"""

from __future__ import annotations

from hoshino import Service
from hoshino.typing import CQEvent

dice_sv = Service("multi_module_dice", help_="多文件目录插件里的掷骰模块")


@dice_sv.on_command("roll")
async def roll(bot, ev: CQEvent):
    """掷骰命令"""
    await bot.send(ev, "roll 100")
