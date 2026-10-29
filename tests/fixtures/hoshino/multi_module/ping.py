"""多文件模块目录里的第二个文件（验证同目录的兄弟模块都会被加载）。"""

from __future__ import annotations

import hoshino  # noqa: F401 — 这个文件用 `import hoshino` 的写法（另一种真实风格）
from hoshino import Service
from hoshino.typing import CQEvent

ping_sv = Service("multi_module_ping", help_="多文件目录插件里的 ping 模块")


@ping_sv.on_command("mm_ping")
async def mm_ping(bot, ev: CQEvent):
    """多文件目录里的第二个命令"""
    await bot.send(ev, "mm_pong")
