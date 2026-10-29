"""子扩展样例：宿插件在 ``cog_load`` / ``cog_unload`` 里 load / unload 子扩展。

这是上游很常见的形态（一个机器人拆成多个 extension，用 ``bot.load_extension`` 装配）。
N.E.K.O. 侧看得到的效果：``start`` 之后 ``subping`` / ``extraping`` 也会出现在插件的
命令列表里（兼容层在 ``start`` 结束时会刷新规格）。
"""

from __future__ import annotations

from discord.ext import commands

#: 子扩展加载 / 卸载的轨迹（测试读它）。
STATE: dict[str, list[str]] = {"loaded": [], "unloaded": []}


class ExtensionHost(commands.Cog, name="extension_host"):
    """宿主 Cog：装配两个子扩展。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="extping", help="宿主自己的命令")
    async def extping(self, ctx: commands.Context) -> None:
        await ctx.send("ext pong")

    async def cog_load(self) -> None:
        for name in ("cogs.sub", "cogs.extra"):
            await self.bot.load_extension(name)
            STATE["loaded"].append(name)

    async def cog_unload(self) -> None:
        for name in ("cogs.extra", "cogs.sub"):
            await self.bot.unload_extension(name)
            STATE["unloaded"].append(name)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ExtensionHost(bot))
