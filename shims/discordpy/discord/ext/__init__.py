"""``discord.ext``：扩展命名空间。

上游这里只有 ``commands`` 与 ``tasks`` 两个官方扩展。shim 两个都提供：

* :mod:`discord.ext.commands` —— Bot / Cog / Command / checks / converters / HelpCommand …
* :mod:`discord.ext.tasks` —— ``@tasks.loop`` 后台定时循环（真跑，用 asyncio）

显式导入两个子模块，这样 ``import discord.ext`` 之后 ``discord.ext.tasks`` 也能
直接取到（上游靠 Python 的子模块 autoload，显式导入对两种写法都成立）。
"""

from __future__ import annotations

from . import commands as commands
from . import tasks as tasks

__all__ = ["commands", "tasks"]
