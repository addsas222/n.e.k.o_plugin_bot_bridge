"""子扩展（包形态）：``cogs/extra/__init__.py``。"""

from __future__ import annotations

from discord.ext import commands


class ExtraCog(commands.Cog, name="extra"):
    """包形态子扩展里的 Cog。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="extraping", help="包形态子扩展的命令")
    async def extraping(self, ctx: commands.Context) -> None:
        await ctx.send("extra pong")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ExtraCog(bot))


async def teardown(bot: commands.Bot) -> None:
    await bot.remove_cog("extra")
