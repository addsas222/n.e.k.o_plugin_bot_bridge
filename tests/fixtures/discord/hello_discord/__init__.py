"""bot_bridge 的 discord.py 兼容层测试插件（包形态：``__init__.py`` + ``setup(bot)``）。

覆盖：``setup`` 入口 / Cog 的 ``name=`` / 前缀命令（别名、``*, rest``、类型转换、引号参数）/
命令组与子命令 / Embed 文本化 / 监听器（``on_message``、``on_ready``、``on_command_error``、
``on_member_join``）/ 前置检查（``is_owner``）/ 冷却 / 生命周期钩子 / 显式不支持的能力。
"""

from __future__ import annotations

import discord
from discord.ext import commands

#: 模块级状态（重新加载会得到新的模块对象，因此不会串）。
STATE: dict[str, object] = {
    "loads": 0,
    "unloads": 0,
    "ready": 0,
    "messages": 0,
    "joins": 0,
}


class HelloDiscord(commands.Cog, name="hello_discord"):
    """示例 Cog。"""

    def __init__(self, bot: commands.Bot, greeting: str = "你好") -> None:
        self.bot = bot
        self.greeting = greeting
        self.loaded = False

    # ---- 生命周期 -------------------------------------------------------- #

    async def cog_load(self) -> None:
        self.loaded = True
        STATE["loads"] = int(STATE["loads"]) + 1

    async def cog_unload(self) -> None:
        self.loaded = False
        STATE["unloads"] = int(STATE["unloads"]) + 1

    # ---- 命令 ------------------------------------------------------------ #

    @commands.command(name="hello", aliases=["hi", "你好"], help="跟机器人打个招呼")
    async def hello(self, ctx: commands.Context, *, name: str = "世界") -> None:
        """打个招呼（``*, name`` 会吃掉剩余的整段文本）。"""
        await ctx.send(f"{self.greeting}，{name}！")

    @commands.command(name="add", help="把两个整数加起来")
    async def add(self, ctx: commands.Context, left: int, right: int) -> None:
        await ctx.send(f"{left} + {right} = {left + right}")

    @commands.command(name="quote", help="引号里的整段内容算一个参数")
    async def quote(self, ctx: commands.Context, text: str) -> None:
        await ctx.send(f"你说的是：{text}")

    @commands.command(name="info", help="用 Embed 回话（会被文本化）")
    async def info(self, ctx: commands.Context) -> None:
        embed = discord.Embed(title="插件信息", description="来自 discord.py 兼容层", colour=discord.Colour.blurple)
        embed.add_field(name="作者", value="tests", inline=True)
        embed.set_footer(text="bot_bridge")
        await ctx.send(embed=embed)

    @commands.command(name="needarg", help="必填参数：缺了要给结构化错误")
    async def needarg(self, ctx: commands.Context, target: str) -> None:
        await ctx.send(f"target={target}")

    @commands.command(name="mention", help="把 @提及 解析成成员")
    async def mention(self, ctx: commands.Context, member: discord.Member) -> None:
        await ctx.send(f"成员：{member.display_name}（{member.id}）")

    @commands.command(name="whoami", help="读 ctx.author / ctx.channel / ctx.guild")
    async def whoami(self, ctx: commands.Context) -> None:
        guild = ctx.guild.name if ctx.guild is not None else "（私聊）"
        await ctx.send(f"{ctx.author.display_name}@{ctx.channel.name}:{guild}")

    @commands.command(name="botinfo", help="读 bot.user / bot.guilds / is_ready")
    async def botinfo(self, ctx: commands.Context) -> None:
        await ctx.send(
            f"我是 {self.bot.user.display_name}；服务器 {len(self.bot.guilds)} 个；ready={self.bot.is_ready()}"
        )

    @commands.group(name="admin", help="管理命令组", invoke_without_command=False)
    async def admin(self, ctx: commands.Context) -> None:
        await ctx.send("admin 需要子命令")

    @admin.command(name="ping", help="管理组里的 ping")
    async def admin_ping(self, ctx: commands.Context, *, text: str = "") -> None:
        await ctx.send(f"admin ping：{text or 'pong'}")

    @commands.command(name="owner", help="只有机器人 owner 能用")
    @commands.is_owner()
    async def owner_only(self, ctx: commands.Context) -> None:
        await ctx.send("owner 你好")

    @commands.command(name="purge", help="需要 manage_messages 权限（按载荷里的权限数据判断）")
    @commands.has_permissions(manage_messages=True)
    async def purge(self, ctx: commands.Context, count: int = 1) -> None:
        await ctx.send(f"清理 {count} 条（示例，不会真的删）")

    @commands.command(name="slow", help="每个用户 60 秒只能用一次")
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def slow(self, ctx: commands.Context) -> None:
        await ctx.send("slow ok")

    @commands.command(name="boom", help="故意抛异常（验证结构化错误）")
    async def boom(self, ctx: commands.Context) -> None:
        raise RuntimeError("boom")

    @commands.command(name="fetch", help="调用没有 N.E.K.O. 等价实现的 API")
    async def fetch(self, ctx: commands.Context, user_id: int) -> None:
        await self.bot.fetch_user(user_id)  # 能力缺失：应当结构化失败

    @commands.command(name="sendfile", help="发附件（N.E.K.O. 只记录，不投递）")
    async def send_file(self, ctx: commands.Context) -> None:
        await ctx.send("这是附件：", file=discord.File(b"report", filename="report.txt"))

    @commands.command(name="states", help="读模块级状态（验证加载 / 生命周期）")
    async def states(self, ctx: commands.Context) -> None:
        await ctx.send(",".join(f"{key}={value}" for key, value in sorted(STATE.items())))

    @commands.hybrid_command(name="hybrid", help="混合命令：前缀可用，斜杠部分不支持")
    async def hybrid(self, ctx: commands.Context, *, text: str = "") -> None:
        await ctx.send(f"hybrid：{text or 'ok'}")

    # ---- 监听器 ---------------------------------------------------------- #

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        STATE["messages"] = int(STATE["messages"]) + 1
        if message.content.startswith("ping"):
            await message.reply("ping -> pong")

    @commands.Cog.listener("on_ready")
    async def ready_listener(self) -> None:
        STATE["ready"] = int(STATE["ready"]) + 1

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: BaseException) -> None:
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"参数不对：{error}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        STATE["joins"] = int(STATE["joins"]) + 1


async def setup(bot: commands.Bot) -> None:
    """扩展入口（上游标准形态：``async def setup(bot)``）。"""
    await bot.add_cog(HelloDiscord(bot))
