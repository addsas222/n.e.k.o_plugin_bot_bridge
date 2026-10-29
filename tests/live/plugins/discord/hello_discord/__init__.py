"""真机样例：一个最小可用的 discord.py 扩展（装到 ``data/plugins/discord/`` 就能跑）。

用法（N.E.K.O. 侧）：把本目录拷进 ``data/plugins/discord/hello_discord``，
刷新插件后面板里应当出现插件 ``discord:hello_discord``，命令列表是
``hello`` / ``echo`` / ``dice`` / ``about``，并带一个 ``on_message`` 监听器。

在 N.E.K.O. 对话里直接说：

* ``!hello`` / ``!hello 小明``   → 打招呼
* ``!echo 随便说点什么``          → 复读（``*, text`` 会吃掉整段文本）
* ``!dice 6``                     → 掷一个 1..6 的骰子
* ``!about``                      → 用 Embed 回一条信息（N.E.K.O. 里会被文本化）
* 直接说 ``ping 一下``            → ``on_message`` 监听器回 ``pong``
"""

from __future__ import annotations

import random

import discord
from discord.ext import commands


class HelloDiscord(commands.Cog, name="hello_discord"):
    """最小可用的示例 Cog。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.greeted = 0

    @commands.command(name="hello", aliases=["hi"], help="跟机器人打个招呼")
    async def hello(self, ctx: commands.Context, *, name: str = "世界") -> None:
        """打个招呼。"""
        self.greeted += 1
        await ctx.send(f"你好，{name}！我是 {self.bot.user.display_name}。")

    @commands.command(name="echo", aliases=["复读"], help="把说的话复读一遍")
    async def echo(self, ctx: commands.Context, *, text: str = "") -> None:
        if not text:
            await ctx.send("要复读点什么？")
            return
        await ctx.send(f"复读：{text}")

    @commands.command(name="dice", help="掷骰子，参数是面数（默认 6）")
    async def dice(self, ctx: commands.Context, faces: int = 6) -> None:
        if faces < 2:
            await ctx.send("面数至少是 2。")
            return
        await ctx.send(f"掷出了 {random.randint(1, faces)} 点（1~{faces}）。")

    @commands.command(name="about", help="这条插件的信息（用 Embed 回话）")
    async def about(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title="hello_discord",
            description="bot_bridge 的 discord.py 兼容层真机样例",
            colour=discord.Colour.blurple,
        )
        embed.add_field(name="前缀", value="`!` 或 `/`", inline=True)
        embed.add_field(name="已经打过招呼", value=str(self.greeted), inline=True)
        embed.set_footer(text="N.E.K.O. bot_bridge")
        await ctx.send(embed=embed)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """直接叫 ``ping`` 就回 ``pong``（不占用命令前缀）。"""
        if message.content.strip().lower().startswith("ping"):
            await message.reply("pong（来自 discord.py 监听器）")


async def setup(bot: commands.Bot) -> None:
    """扩展入口。"""
    await bot.add_cog(HelloDiscord(bot))
