"""单文件 discord.py 扩展：一个 Cog 类 + 老式同步 ``setup(client)``。

覆盖：单文件形态的 ``detect``、同步 ``setup``、``bot.add_cog`` 的同步写法（不 await）。
"""

from __future__ import annotations

from discord.ext import commands


class SingleFileCog(commands.Cog, name="single_file"):
    """单文件插件里的 Cog。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="sfping", help="单文件插件的 ping")
    async def sfping(self, ctx: commands.Context) -> None:
        await ctx.send("sfpong")

    @commands.command(name="sfsay", aliases=["sfs"], help="单文件插件的复读")
    async def sfsay(self, ctx: commands.Context, *, text: str = "") -> None:
        await ctx.send(f"复读：{text}")


def setup(client: commands.Bot) -> None:
    """老式同步入口（上游 1.x 风格：``def setup(client)``）。"""
    client.add_cog(SingleFileCog(client))
