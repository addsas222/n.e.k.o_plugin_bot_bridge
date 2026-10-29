"""坏插件：``setup(bot)`` 里抛异常（``load`` 必须给出结构化 ``PluginLoadError``）。"""

from __future__ import annotations

from discord.ext import commands


class BadSetupCog(commands.Cog):
    """正常的 Cog，但 setup 会炸。"""

    @commands.command(name="never", help="永远不会注册成功")
    async def never(self, ctx: commands.Context) -> None:
        await ctx.send("never")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(BadSetupCog(bot))
    raise RuntimeError("setup boom")
