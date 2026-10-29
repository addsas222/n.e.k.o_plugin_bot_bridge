"""``hello_hoshino`` 的兄弟模块：验证兼容层会把目录里的每个 ``*.py`` 都导入。

上游 ``nonebot.load_plugins`` 会递归加载 ``hoshino/modules/<name>/`` 下的每个文件，
所以一个插件目录里可以有多个 Service。这个文件里的 Service 属于同一个 ``LoadedPlugin``。
"""

from __future__ import annotations

from hoshino import Service
from hoshino.typing import CQEvent

extra_sv = Service("hello_hoshino_extra", help_="同目录兄弟模块里的第二个 Service")


@extra_sv.on_command("extracmd")
async def extra_cmd(bot, ev: CQEvent):
    """兄弟模块里的命令"""
    await bot.send(ev, "extra ok")


@extra_sv.on_keyword("extra_kw")
async def extra_keyword(bot, ev: CQEvent):
    """兄弟模块里的关键词触发器"""
    await bot.send(ev, "extra keyword ok")
