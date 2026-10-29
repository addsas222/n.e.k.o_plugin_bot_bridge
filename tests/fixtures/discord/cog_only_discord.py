"""只有 Cog 类、没有 ``setup`` 的插件（兼容层应当自己实例化并挂上）。"""

from __future__ import annotations

from discord.ext import commands


class CogOnly(commands.Cog, name="cog_only"):
    """没有 setup 入口的 Cog。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="cogonly", help="没有 setup 也能被挂上")
    async def cogonly(self, ctx: commands.Context) -> None:
        await ctx.send("cog-only ok")
