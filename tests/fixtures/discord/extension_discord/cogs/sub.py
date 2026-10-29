"""子扩展（模块形态）：``cogs/sub.py``。"""

from __future__ import annotations

from discord.ext import commands


class SubCog(commands.Cog, name="sub"):
    """子扩展里的 Cog。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="subping", help="子扩展的命令")
    async def subping(self, ctx: commands.Context) -> None:
        await ctx.send("sub pong")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(SubCog(bot))


async def teardown(bot: commands.Bot) -> None:
    """卸载钩子：把自己的 Cog 摘掉（上游同义）。"""
    await bot.remove_cog("sub")
