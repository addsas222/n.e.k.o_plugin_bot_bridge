"""坏插件：导入期就抛异常（``load`` 必须给出结构化 ``PluginLoadError``）。"""

from __future__ import annotations

from discord.ext import commands


class BrokenCog(commands.Cog):
    """还没被挂上就炸了。"""


raise RuntimeError("fixture import boom")
