"""N.E.K.O. bot_bridge 的 discord.py 兼容 shim（``discord`` 包名的**忠实子集**）。

这不是上游 discord.py，而是一个**只依赖标准库**的实现，用来在 N.E.K.O. 的解释器里
直接跑 discord.py（cogs / ``setup(bot)`` 扩展）插件的源码：

    from discord.ext import commands

    class Hello(commands.Cog, name="hello"):
        @commands.command(name="hello", aliases=["hi"], help="打个招呼")
        async def hello(self, ctx, *, name: str = "世界"):
            await ctx.send(f"你好，{name}！")

    async def setup(bot):
        await bot.add_cog(Hello(bot))

被桥接层（``bridge/backends/discordpy.py``）加载的插件会 ``import discord`` 到这里，
而不是真的 discord.py。

## 覆盖范围

* 客户端：``Client``（Cog 注册表 / ``event`` / ``listen`` / ``dispatch`` / ``wait_for`` /
  ``get_channel`` / ``get_guild`` / ``get_user`` / ``loop`` / ``is_ready``）、``Bot``
  与 ``AutoShardedBot``（``command_prefix`` / ``get_context`` / ``process_commands`` /
  ``invoke`` / ``load_extension`` / 命令注册表）
* 命令：``command`` / ``group`` / ``hybrid_command`` / ``hybrid_group``、``Command`` /
  ``Group``、``Context``、``Converter`` 家族、``Greedy[...]``、
  ``has_permissions`` / ``is_owner`` / ``guild_only`` / ``check`` / ``cooldown`` /
  ``max_concurrency``、命令错误家族、``StringView``
* 模型：``Message`` / ``User`` / ``Member`` / ``ClientUser`` / ``Guild`` / ``Role`` /
  ``TextChannel`` / ``Thread`` / ``DMChannel`` / ``CategoryChannel`` / ``VoiceChannel`` /
  ``ForumChannel`` / ``Attachment`` / ``Emoji`` / ``PartialEmoji`` / ``Object``（``Snowflake``）
* 富文本与颜色：``Embed``（``to_dict()`` 与上游一致）、``Colour`` / ``Color``
* 配置与类型：``Intents``、``Permissions``、``ChannelType``、``MessageType``、``Interaction``
* 工具：``discord.utils``（``get`` / ``find`` / ``MISSING`` / ``utcnow`` / ``format_dt``）、
  ``discord.errors``、``discord.abc``（``Snowflake`` / ``Messageable`` / ``GuildChannel`` …）、
  ``discord.app_commands``（元数据 + 显式不支持）

## 已知偏差（相对上游 discord.py）

1. **没有网关 / HTTP**：``Client.run`` / ``start`` / ``login`` / ``close``、
   ``fetch_*``、``change_presence``、语音、Webhook 等都抛 ``UnsupportedCapabilityError``
   （不是静默失败，也不编造数据）。消息来自 N.E.K.O. 的对话总线。
2. **没有 Discord 目录数据**：成员 / 频道 / 角色 / 用户只认「本次消息上下文里已知的」
   （当前作者、当前频道、当前服务器，以及 payload 里的 ``mentions`` / ``channels`` /
   ``roles`` / ``members``）；查不到就抛 ``MemberNotFound`` / ``ChannelNotFound`` 之类。
   权限与所有者身份同样按 payload（``permissions`` / ``bot_permissions`` / ``is_owner`` /
   ``roles`` / ``bot_roles``）或桥接配置（``discord_owner_ids``）判断，**没有数据时报
   能力缺失**，不会假装「有权限」或「没权限」。
3. **回复只投递文本**：``embed`` / ``embeds`` 会被文本化，附件（``File``）与交互组件
   （``View`` / ``Button``）没有 N.E.K.O. 载体，只做结构化记录 + 警告日志。
4. **``add_cog`` / ``remove_cog`` 同步注册 + 返回可等待对象**：``bot.add_cog(cog)`` 与
   ``await bot.add_cog(cog)`` 都成立；``cog_load`` / ``cog_unload`` 由兼容层的
   ``start()`` / ``stop()`` 驱动（这样后台任务才绑在真正运行的事件循环上）。
5. **``dispatch`` 直接 await 监听器**（上游用 ``create_task`` 调度），以便 N.E.K.O.
   收集回复；``@bot.event`` 覆盖 ``on_message`` 的行为与上游一致（插件要自己再调
   ``process_commands``）。
6. **命令参数解析**是「最小可用」子集：引号分组、``*, rest``、``Greedy[...]``、默认值、
   ``Optional`` / ``Union`` / ``Literal`` / 内置类型 / ``Converter`` 子类都支持；
   ``*args`` / ``**kwargs`` 在装饰时直接报错（与上游对前缀命令的限制一致）；
   ``bool`` 只认明确的真假写法（上游 ``bool("false")`` 会得到 True，这里修正了）。
7. **已补齐**（2026-10-29）：``discord.ui``（View / Button / Select / Modal / TextInput）、
   ``discord.ext.tasks``（``@tasks.loop``，用 asyncio 真跑）、``HelpCommand`` /
   ``DefaultHelpCommand``、``FlagConverter``。交互组件**能构造、能挂载、能取属性**，
   但没有「组件被点击」的回调通道 —— ``View.wait()`` 与 ``InteractionResponse.*``
   显式抛 ``UnsupportedCapabilityError``，不返回假结果。
8. **仍不提供**（导入即 ``ImportError``，不做空壳）：``discord.webhook`` /
   ``discord.voice_client`` / ``discord.player`` / ``discord.sinks`` / ``discord.ext.bridge``。
9. **``discord.Bot``** 是 ``discord.ext.commands.Bot`` 的别名（上游顶层没有这个名字，
   这里为了任务要求而提供）；``discord.errors`` 里同时包含命令错误家族（上游把它们放在
   ``discord.ext.commands.errors``，本 shim 两边都能 import 到同一批类）。
"""

from __future__ import annotations

from . import errors as errors  # noqa: F401 — 上游 ``discord.errors`` 子模块路径
from . import utils as utils  # noqa: F401 — 上游 ``discord.utils`` 子模块路径
from . import abc as abc  # noqa: F401 — 上游 ``discord.abc`` 子模块路径
from .abc import Connectable, GuildChannel, Messageable, PrivateChannel, Snowflake
from .client import Client, when_mentioned, when_mentioned_or
from .colour import Color, Colour
from .embeds import Embed
from .emoji import Emoji, PartialEmoji
from .enums import ButtonStyle, ChannelType, ComponentType, MessageType, TextStyle, try_enum
from .errors import DiscordException
from .file import File
from .flags import BaseFlags
from .guild import Guild, Role
from .intents import Intents
from .interactions import Interaction, InteractionResponse, InteractionType
from .message import Attachment, Message, Reaction
from .object import Object
from .permissions import Permissions
from .user import BaseUser, ClientUser, Member, User
from .channel import (
    CategoryChannel,
    DMChannel,
    ForumChannel,
    StageChannel,
    TextChannel,
    Thread,
    VoiceChannel,
)
from .utils import MISSING

__version__ = "2.4.0"
"""模拟的上游 discord.py 版本号（本 shim 复刻的 API 子集对应这个版本）。"""

_NEKO_BRIDGE_SHIM = True
"""桥接层用来确认 ``sys.modules['discord']`` 确实是本 shim 而不是真的 discord.py。"""

_NEKO_SHIM_VERSION = "0.1.0"
"""shim 自身版本。"""

_NEKO_SHIM_UPSTREAM = "https://github.com/Rapptz/discord.py"

__all__ = [
    "Attachment",
    "BaseFlags",
    "BaseUser",
    "ButtonStyle",
    "ChannelType",
    "Client",
    "ClientUser",
    "Color",
    "Colour",
    "ComponentType",
    "Connectable",
    "DiscordException",
    "DMChannel",
    "Embed",
    "Emoji",
    "File",
    "ForumChannel",
    "Guild",
    "GuildChannel",
    "Intents",
    "Interaction",
    "InteractionResponse",
    "InteractionType",
    "Member",
    "Message",
    "MessageType",
    "Messageable",
    "MISSING",
    "Object",
    "PartialEmoji",
    "Permissions",
    "PrivateChannel",
    "Reaction",
    "Role",
    "Snowflake",
    "StageChannel",
    "TextChannel",
    "TextStyle",
    "Thread",
    "User",
    "VoiceChannel",
    "CategoryChannel",
    "abc",
    "errors",
    "try_enum",
    "utils",
    "when_mentioned",
    "when_mentioned_or",
]

# --------------------------------------------------------------------------- #
# 子包（放在文件末尾导入，避免与 ext.commands → discord.client 的初始化顺序打架）
# --------------------------------------------------------------------------- #

from . import app_commands as app_commands  # noqa: E402,F401
from . import ext as ext  # noqa: E402,F401
from .ext.commands import Bot  # noqa: E402 — 任务要求的 ``discord.Bot`` 别名

__all__ += ["Bot", "app_commands", "ext"]
