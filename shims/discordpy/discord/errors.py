"""discord.py 的异常类型（``discord.errors`` 的忠实子集）。

上游把「网关 / HTTP 层」异常放在 ``discord.errors``，把「命令层」异常放在
``discord.ext.commands.errors``。本 shim 把两族都定义在这里，``discord.ext.commands.errors``
再原样转出 —— 这样 ``discord.errors.CommandError`` 与 ``commands.CommandError`` 是同一个
类（上游不保证这一点，但对插件更宽容），也避免了 ``discord.errors`` ↔ ``discord.ext``
的循环导入。继承关系、异常名、``message`` 文案都尽量与上游一致，插件的
``except commands.MissingPermissions`` 这类写法才能照常工作。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

__all__ = [
    "ArgumentParsingError",
    "BadArgument",
    "BadBoolArgument",
    "BadColourArgument",
    "BadInviteArgument",
    "BadLiteralArgument",
    "BadUnionArgument",
    "BotMissingAnyRole",
    "BotMissingPermissions",
    "BotMissingRole",
    "ChannelNotFound",
    "ChannelNotReadable",
    "CheckAnyFailure",
    "CheckFailure",
    "ClientException",
    "CommandError",
    "CommandInvokeError",
    "CommandNotFound",
    "CommandOnCooldown",
    "CommandRegistrationError",
    "ConnectionClosed",
    "ConversionError",
    "DiscordException",
    "DiscordServerError",
    "EmojiNotFound",
    "ExpectedClosingQuoteError",
    "ExtensionAlreadyLoaded",
    "ExtensionError",
    "ExtensionFailed",
    "ExtensionNotLoaded",
    "ExtensionNotFound",
    "Forbidden",
    "GatewayNotFound",
    "GuildNotFound",
    "HTTPException",
    "InteractionResponded",
    "InvalidArgument",
    "InvalidData",
    "InvalidEndOfQuotedStringError",
    "LoginFailure",
    "MaxConcurrencyReached",
    "MemberNotFound",
    "MessageNotFound",
    "MissingAnyRole",
    "MissingPermissions",
    "MissingRequiredArgument",
    "MissingRole",
    "NSFWChannelRequired",
    "NoMoreItems",
    "NoPrivateMessage",
    "NotFound",
    "NotOwner",
    "ObjectNotFound",
    "PartialEmojiConversionFailure",
    "PrivateMessageOnly",
    "PrivilegedIntentsRequired",
    "RoleNotFound",
    "ThreadNotFound",
    "TooManyArguments",
    "UnexpectedQuoteError",
    "UserInputError",
    "UserNotFound",
]


# --------------------------------------------------------------------------- #
# 基础族（上游 discord.errors）
# --------------------------------------------------------------------------- #


class DiscordException(Exception):
    """所有 discord.py 异常的基类。"""


class ClientException(DiscordException):
    """客户端用法错误。"""


class NoMoreItems(ClientException):
    """异步迭代器已耗尽。"""


class GatewayNotFound(DiscordException):
    """找不到可用的网关（N.E.K.O. 里没有网关，因此只可能在插件显式请求时出现）。"""

    def __init__(self) -> None:
        super().__init__("The gateway to Discord was not found.")


class HTTPException(DiscordException):
    """一次 HTTP 请求失败。

    N.E.K.O. 不承载 Discord HTTP 客户端，因此本 shim 里它主要用于
    ``except discord.HTTPException`` 的兼容与能力缺失的载体。
    """

    def __init__(self, response: Any = None, message: str = "") -> None:
        self.response = response
        self.status = int(getattr(response, "status", 0) or 0)
        self.code = getattr(response, "code", None)
        self.text = str(message or "HTTP request failed.")
        fmt = f"{self.status} {self.text}" if self.status else self.text
        super().__init__(fmt)


class Forbidden(HTTPException):
    """权限不足（HTTP 403）。"""

    def __init__(self, response: Any = None, message: str = "Forbidden") -> None:
        super().__init__(response, message)


class NotFound(HTTPException):
    """资源不存在（HTTP 404）。"""

    def __init__(self, response: Any = None, message: str = "Not Found") -> None:
        super().__init__(response, message)


class DiscordServerError(HTTPException):
    """Discord 侧 5xx。"""


class InvalidData(DiscordException):
    """收到的数据不符合预期。"""


class InvalidArgument(DiscordException):
    """参数非法。"""


class LoginFailure(DiscordException):
    """登录失败（N.E.K.O. 里没有登录流程）。"""


class ConnectionClosed(DiscordException):
    """连接关闭（N.E.K.O. 里没有网关连接）。"""


class PrivilegedIntentsRequired(ClientException):
    """特权 intent 未开启（N.E.K.O. 里没有 intent 审批）。"""


class InteractionResponded(ClientException):
    """同一次交互被响应了两次。"""


# --------------------------------------------------------------------------- #
# 命令族（上游 discord.ext.commands.errors）
# --------------------------------------------------------------------------- #


class CommandError(DiscordException):
    """命令执行失败的基类。"""

    def __init__(self, *args: Any) -> None:
        self.args = args
        message = args[0] if args else ""
        super().__init__(message)

    def __str__(self) -> str:
        return str(self.args[0]) if self.args else ""


class UserInputError(CommandError):
    """用户输入导致的失败（核心会据此静默或提示）。"""

    def __init__(self, message: str, *args: Any, param: Any = None) -> None:
        self.param = param
        super().__init__(message, *args)


class ArgumentParsingError(UserInputError):
    """参数分词失败（引号不配对之类）。"""

    def __init__(self, message: str, *args: Any) -> None:
        super().__init__(message, *args)


class UnexpectedQuoteError(ArgumentParsingError):
    """出现了引号但不在参数开头。"""

    def __init__(self, quote: str) -> None:
        super().__init__(f"Unexpected quote mark, {quote!r}, in non-quoted string field")
        self.quote = quote


class InvalidEndOfQuotedStringError(ArgumentParsingError):
    """引号字符串结束后紧跟了非空白字符。"""

    def __init__(self, char: str) -> None:
        super().__init__(f"Expected space after closing quotation but received {char!r}")
        self.char = char


class ExpectedClosingQuoteError(ArgumentParsingError):
    """引号字符串没有收尾。"""

    def __init__(self, close_quote: str) -> None:
        super().__init__(f"Expected closing {close_quote!r}")
        self.close_quote = close_quote


class TooManyArguments(UserInputError):
    """给的参数比命令声明的多（``ignore_extra=False`` 时抛）。"""

    def __init__(self) -> None:
        super().__init__("Too many arguments passed to the command.")


class MissingRequiredArgument(UserInputError):
    """缺少必填参数。"""

    def __init__(self, param: Any) -> None:
        name = getattr(param, "display_name", None) or getattr(param, "name", str(param))
        super().__init__(f"{name} is a required argument that is missing.", param=param)


class BadArgument(UserInputError):
    """参数转换失败。"""

    def __init__(self, argument: str, *, name: str = "") -> None:
        self.argument = argument
        self.name = name
        super().__init__(f'Converting to "{name}" failed for parameter "{argument}".')


class BadUnionArgument(UserInputError):
    """``Union`` 注解的每个分支都转换失败。"""

    def __init__(self, param: Any, converters: Sequence[Any], errors: Sequence[BaseException]) -> None:
        self.converters = list(converters)
        self.errors = list(errors)
        name = getattr(param, "display_name", None) or getattr(param, "name", str(param))
        super().__init__(
            f'Could not convert "{name}" into any of the following: '
            f'{", ".join(getattr(item, "__name__", str(item)) for item in self.converters)}',
            param=param,
        )


class BadLiteralArgument(UserInputError):
    """``Literal`` 注解收到了不在候选集里的值。"""

    def __init__(self, param: Any, arguments: Sequence[Any], literals: Sequence[Any]) -> None:
        self.arguments = list(arguments)
        self.literals = [str(item) for item in literals]
        name = getattr(param, "display_name", None) or getattr(param, "name", str(param))
        super().__init__(
            f'Invalid value given for {name}: expected one of {", ".join(self.literals)}', param=param
        )


class ConversionError(UserInputError):
    """转换器自己抛了非 ``BadArgument`` 的异常。"""

    def __init__(self, converter: Any, original: BaseException) -> None:
        self.converter = converter
        self.original = original
        super().__init__(f'Could not convert "{getattr(converter, "__name__", converter)}" to a compatible type.')


# --- 转换失败（都是 BadArgument 的子类，与上游一致） ------------------------ #


class _NotFound(BadArgument):
    """「在你的上下文里找不到这个对象」的基类。"""

    def __init__(self, argument: str, *, name: str = "") -> None:
        self.argument = str(argument)
        super().__init__(self.argument, name=name or type(self).__name__.replace("NotFound", ""))


class MemberNotFound(_NotFound):
    """找不到这个成员（N.E.K.O. 只知道载荷里出现过的成员）。"""

    def __init__(self, argument: str) -> None:
        super().__init__(argument, name="Member")


class UserNotFound(_NotFound):
    """找不到这个用户。"""

    def __init__(self, argument: str) -> None:
        super().__init__(argument, name="User")


class ChannelNotFound(_NotFound):
    """找不到这个频道。"""

    def __init__(self, argument: str) -> None:
        super().__init__(argument, name="Channel")


class ThreadNotFound(_NotFound):
    """找不到这个线程。"""

    def __init__(self, argument: str) -> None:
        super().__init__(argument, name="Thread")


class MessageNotFound(_NotFound):
    """找不到这条消息（N.E.K.O. 只可见当前消息）。"""

    def __init__(self, argument: str) -> None:
        super().__init__(argument, name="Message")


class GuildNotFound(_NotFound):
    """找不到这个服务器。"""

    def __init__(self, argument: str) -> None:
        super().__init__(argument, name="Guild")


class RoleNotFound(_NotFound):
    """找不到这个角色。"""

    def __init__(self, argument: str) -> None:
        super().__init__(argument, name="Role")


class ObjectNotFound(_NotFound):
    """这个 id 不是合法的对象 id。"""

    def __init__(self, argument: str) -> None:
        super().__init__(argument, name="Object")


class EmojiNotFound(_NotFound):
    """找不到这个表情。"""

    def __init__(self, argument: str) -> None:
        super().__init__(argument, name="Emoji")


class PartialEmojiConversionFailure(BadArgument):
    """表情字符串解析失败。"""

    def __init__(self, argument: str) -> None:
        self.argument = str(argument)
        super().__init__(self.argument, name="PartialEmoji")


class BadColourArgument(BadArgument):
    """颜色解析失败。"""

    def __init__(self, argument: str) -> None:
        self.argument = str(argument)
        super().__init__(self.argument, name="Colour")


class BadBoolArgument(BadArgument):
    """布尔解析失败。"""

    def __init__(self, argument: str) -> None:
        self.argument = str(argument)
        super().__init__(self.argument, name="bool")


class BadInviteArgument(BadArgument):
    """邀请链接解析失败（N.E.K.O. 里也没有可解析的邀请）。"""

    def __init__(self, argument: str) -> None:
        self.argument = str(argument)
        super().__init__(self.argument, name="Invite")


class CommandNotFound(CommandError):
    """没找到这个命令（前缀命中了，但名字不在注册表里）。"""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f'Command "{name}" is not found.')


class CheckFailure(CommandError):
    """前置检查（check）失败。"""

    def __init__(self, message: str = "", *args: Any, command: Any = None) -> None:
        self.command = command
        name = getattr(command, "qualified_name", "") or ""
        if not message:
            message = f"The check functions for command {name} failed." if name else "The check functions failed."
        super().__init__(message, *args)


class CheckAnyFailure(CheckFailure):
    """``check_any`` 的所有分支都失败。"""

    def __init__(self, checks: Sequence[Any], errors: Sequence[BaseException]) -> None:
        self.checks = list(checks)
        self.errors = list(errors)
        super().__init__("You do not have permission to run this command.")


class PrivateMessageOnly(CheckFailure):
    def __init__(self, message: str = "This command can only be used in private messages.") -> None:
        super().__init__(message)


class NoPrivateMessage(CheckFailure):
    def __init__(self, message: str = "This command cannot be used in direct messages.") -> None:
        super().__init__(message)


class NotOwner(CheckFailure):
    def __init__(self, message: str = "You are not the owner of this bot.") -> None:
        super().__init__(message)


class NSFWChannelRequired(CheckFailure):
    def __init__(self, channel: Any = None) -> None:
        self.channel = channel
        super().__init__("This command can only be used in NSFW channels.")


class _MissingRoleBase(CheckFailure):
    def __init__(self, message: str, *, role: Any = None, roles: Sequence[Any] | None = None, is_bot: bool = False) -> None:
        self.missing_role = role
        self.missing_roles = list(roles or ())
        self.bot_missing = bool(is_bot)
        super().__init__(message)


class MissingRole(_MissingRoleBase):
    """缺少某个角色。"""

    def __init__(self, role: Any) -> None:
        name = getattr(role, "name", str(role))
        super().__init__(f"You are missing {name} role to run this command.", role=role)


class BotMissingRole(_MissingRoleBase):
    def __init__(self, role: Any) -> None:
        name = getattr(role, "name", str(role))
        super().__init__(f"Bot is missing {name} role to run this command.", role=role, is_bot=True)


class MissingAnyRole(_MissingRoleBase):
    def __init__(self, roles: Sequence[Any]) -> None:
        names = [getattr(role, "name", str(role)) for role in roles]
        super().__init__(f"You are missing at least one of the following roles: {', '.join(names)}", roles=roles)


class BotMissingAnyRole(_MissingRoleBase):
    def __init__(self, roles: Sequence[Any]) -> None:
        names = [getattr(role, "name", str(role)) for role in roles]
        super().__init__(
            f"Bot is missing at least one of the following roles: {', '.join(names)}", roles=roles, is_bot=True
        )


class _MissingPermissionsBase(CheckFailure):
    def __init__(self, message: str, *, permissions: Sequence[str], is_bot: bool = False) -> None:
        self.missing_permissions = list(permissions)
        self.missing_perms = self.missing_permissions  # 上游历史别名
        self.bot_missing = bool(is_bot)
        super().__init__(message)


class MissingPermissions(_MissingPermissionsBase):
    """调用者缺少权限。"""

    def __init__(self, permissions: Sequence[str], *, detail: str = "") -> None:
        names = ", ".join(str(item) for item in permissions)
        message = f"You are missing {names} permission(s) to run this command."
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message, permissions=permissions)


class BotMissingPermissions(_MissingPermissionsBase):
    """机器人自己缺少权限。"""

    def __init__(self, permissions: Sequence[str], *, detail: str = "") -> None:
        names = ", ".join(str(item) for item in permissions)
        message = f"Bot is missing {names} permission(s) to run this command."
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message, permissions=permissions, is_bot=True)


class CommandInvokeError(CommandError):
    """命令回调自己抛了异常（``original`` 是原始异常）。"""

    def __init__(self, e: BaseException) -> None:
        self.original = e
        super().__init__(f"Command raised an exception: {e.__class__.__name__}: {e}")


class CommandOnCooldown(CommandError):
    """命令还在冷却中。"""

    def __init__(self, cooldown: Any, retry_after: float) -> None:
        self.cooldown = cooldown
        self.retry_after = retry_after
        super().__init__(f"You are on cooldown. Try again in {retry_after:.2f}s")


class MaxConcurrencyReached(CommandError):
    """并发上限（本 shim 不实现 ``max_concurrency``，保留类型供插件 ``except`` 使用）。"""

    def __init__(self, number: int, per: Any) -> None:
        self.number = number
        self.per = per
        name = getattr(per, "name", str(per))
        super().__init__(f"Too many people using this command. It can only be used {number} per {name} concurrently.")


class CommandRegistrationError(ClientException):
    """命令名冲突。"""

    def __init__(self, name: str, alias_conflict: bool = False) -> None:
        self.name = name
        self.alias_conflict = alias_conflict
        kind = "alias" if alias_conflict else "command"
        super().__init__(f"The {kind} {name} is already an existing command or alias.")


class ExtensionError(CommandError):
    """扩展加载 / 卸载失败。"""

    def __init__(self, message: str = "", *args: Any, name: str = "") -> None:
        self.name = name
        super().__init__(message, *args)


class ExtensionAlreadyLoaded(ExtensionError):
    def __init__(self, name: str) -> None:
        super().__init__(f"The extension {name!r} is already loaded.", name=name)


class ExtensionNotLoaded(ExtensionError):
    def __init__(self, name: str) -> None:
        super().__init__(f"The extension {name!r} is not loaded.", name=name)


class NoEntryPointError(ExtensionError):
    def __init__(self, name: str) -> None:
        super().__init__(f"The extension {name!r} does not have a 'setup' entry point.", name=name)


class ExtensionFailed(ExtensionError):
    def __init__(self, name: str, original: BaseException) -> None:
        self.original = original
        super().__init__(f"Extension {name!r} raised an error: {original.__class__.__name__}: {original}", name=name)


class ExtensionNotFound(ExtensionError):
    def __init__(self, name: str) -> None:
        super().__init__(f"Extension {name!r} could not be loaded.", name=name)


class ChannelNotReadable(CheckFailure):
    """频道不可读（check 失败的一种）。"""

    def __init__(self, argument: Any) -> None:
        self.argument = argument
        super().__init__(f"Can not read messages in channel {getattr(argument, 'mention', argument)}")
