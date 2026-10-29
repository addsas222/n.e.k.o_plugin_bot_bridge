"""``discord.ext.commands``：命令扩展（N.E.K.O. bot_bridge 兼容 shim）。

覆盖：``Bot`` / ``AutoShardedBot`` / ``Cog``（含 ``name=`` / ``@Cog.listener`` /
生命周期钩子）、``command`` / ``group`` / ``hybrid_command`` / ``hybrid_group`` 装饰器、
``Command`` / ``Group``、``Context``、``Converter`` 家族、``has_permissions`` 等检查、
``cooldown`` / ``max_concurrency``、命令错误家族、``StringView``。

已知缺口（不提供的名字导入即 ``ImportError``，不做空壳）：
``HelpCommand`` / ``DefaultHelpCommand`` / ``Paginator``、``FlagConverter`` /
``Flag`` / ``flag`` 装饰器、``GuildConverter`` 之外的 ``StageChannelConverter`` /
``CategoryChannelConverter``、``bot_has_guild_permissions`` 之外的角色管理工具、
``View`` / ``discord.ui``（消息组件与交互回调在 N.E.K.O. 里没有对应面）、
``ext.tasks``（后台循环请直接放进 ``cog_load`` 并用 ``asyncio`` 管理）。
"""

from __future__ import annotations

from ...errors import (
    ArgumentParsingError,
    BadArgument,
    BadBoolArgument,
    BadColourArgument,
    BadInviteArgument,
    BadLiteralArgument,
    BadUnionArgument,
    BotMissingAnyRole,
    BotMissingPermissions,
    BotMissingRole,
    CheckAnyFailure,
    CheckFailure,
    CommandError,
    CommandInvokeError,
    CommandNotFound,
    CommandOnCooldown,
    CommandRegistrationError,
    ConversionError,
    EmojiNotFound,
    ExpectedClosingQuoteError,
    ExtensionAlreadyLoaded,
    ExtensionError,
    ExtensionFailed,
    ExtensionNotLoaded,
    ExtensionNotFound,
    GuildNotFound,
    MaxConcurrencyReached,
    MemberNotFound,
    MessageNotFound,
    MissingAnyRole,
    MissingPermissions,
    MissingRequiredArgument,
    MissingRole,
    NSFWChannelRequired,
    NoPrivateMessage,
    NotOwner,
    ObjectNotFound,
    PartialEmojiConversionFailure,
    PrivateMessageOnly,
    RoleNotFound,
    ThreadNotFound,
    TooManyArguments,
    UnexpectedQuoteError,
    UserInputError,
    UserNotFound,
)
from . import errors as errors
from .bot import AutoShardedBot, Bot, BotBase
from .checks import (
    bot_has_any_role,
    bot_has_permissions,
    bot_has_role,
    check,
    check_any,
    dm_only,
    guild_only,
    has_any_role,
    has_guild_permissions,
    has_permissions,
    has_role,
    is_nsfw,
    is_owner,
)
from .cog import Cog, CogMeta
from .context import Context
from .converter import (
    ChannelConverter,
    ColorConverter,
    ColourConverter,
    Converter,
    EmojiConverter,
    GuildConverter,
    MemberConverter,
    MessageConverter,
    PartialEmojiConverter,
    RoleConverter,
    TextChannelConverter,
    UserConverter,
)
from .cooldowns import BucketType, Cooldown, CooldownMapping, MaxConcurrency
from .core import Command, Group, Greedy, Parameter, command, cooldown, group, hybrid_command, hybrid_group
from .core import max_concurrency
from .flags import Flag, FlagConverter, FlagError, flag
from .help import DefaultHelpCommand, HelpCommand
from .view import StringView, quoted_word

__all__ = [
    "ArgumentParsingError",
    "AutoShardedBot",
    "BadArgument",
    "BadBoolArgument",
    "BadColourArgument",
    "BadInviteArgument",
    "BadLiteralArgument",
    "BadUnionArgument",
    "Bot",
    "BotBase",
    "BotMissingAnyRole",
    "BotMissingPermissions",
    "BotMissingRole",
    "BucketType",
    "ChannelConverter",
    "CheckAnyFailure",
    "CheckFailure",
    "Cog",
    "CogMeta",
    "ColorConverter",
    "ColourConverter",
    "Command",
    "CommandError",
    "CommandInvokeError",
    "CommandNotFound",
    "CommandOnCooldown",
    "CommandRegistrationError",
    "Context",
    "ConversionError",
    "Converter",
    "Cooldown",
    "CooldownMapping",
    "DefaultHelpCommand",
    "Flag",
    "FlagConverter",
    "FlagError",
    "HelpCommand",
    "flag",
    "EmojiConverter",
    "EmojiNotFound",
    "ExpectedClosingQuoteError",
    "ExtensionAlreadyLoaded",
    "ExtensionError",
    "ExtensionFailed",
    "ExtensionNotLoaded",
    "ExtensionNotFound",
    "Greedy",
    "Group",
    "GuildConverter",
    "GuildNotFound",
    "MaxConcurrency",
    "MaxConcurrencyReached",
    "MemberConverter",
    "MemberNotFound",
    "MessageConverter",
    "MessageNotFound",
    "MissingAnyRole",
    "MissingPermissions",
    "MissingRequiredArgument",
    "MissingRole",
    "NSFWChannelRequired",
    "NoPrivateMessage",
    "NotOwner",
    "ObjectNotFound",
    "Parameter",
    "PartialEmojiConversionFailure",
    "PartialEmojiConverter",
    "PrivateMessageOnly",
    "RoleConverter",
    "RoleNotFound",
    "StringView",
    "TextChannelConverter",
    "ThreadNotFound",
    "TooManyArguments",
    "UnexpectedQuoteError",
    "UserInputError",
    "UserNotFound",
    "UserConverter",
    "bot_has_any_role",
    "bot_has_permissions",
    "bot_has_role",
    "check",
    "check_any",
    "command",
    "cooldown",
    "dm_only",
    "group",
    "guild_only",
    "has_any_role",
    "has_guild_permissions",
    "has_permissions",
    "has_role",
    "hybrid_command",
    "hybrid_group",
    "is_nsfw",
    "is_owner",
    "max_concurrency",
    "quoted_word",
]

#: 上游的 ``checks`` / ``converter`` / ``cooldowns`` / ``view`` 子模块路径也保留
#: （``from discord.ext.commands.checks import guild_only`` 这类写法照常可用）。
from . import checks as checks  # noqa: E402,F401
from . import converter as converter  # noqa: E402,F401
from . import cooldowns as cooldowns  # noqa: E402,F401
from . import view as view  # noqa: E402,F401
