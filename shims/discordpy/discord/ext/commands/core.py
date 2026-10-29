"""``discord.ext.commands.core``：``Command`` / ``Group`` 与命令装饰器。

参数解析规则（本 shim 的**最小可用**语义，与上游一致的部分都标了「同上游」）：

============  ==========================================================================
注解 / 写法    行为
============  ==========================================================================
不写 / ``str`` 原样字符串
``int``/``float``/``bool``
              内置转换；``bool`` 只认 true/false/yes/no/on/off/1/0（上游直接 ``bool()``，
              会把 ``"false"`` 当 True —— 这里是有意的修正，失败抛 ``BadArgument``）
``Literal[...]``
              必须是候选值之一，否则 ``BadLiteralArgument``
``Optional[X]`` / ``Union[X, Y]``
              依次尝试；``Optional`` 且参数缺失时传 ``None``
``Converter`` 子类 / 已实现的 ``discord.*`` 类型
              按转换器解析，失败抛 ``MemberNotFound`` / ``BadArgument`` 之类
``*, rest`` （KEYWORD_ONLY）
              **吃掉剩余全部参数**（同上游），按引号分词后再转换
``Greedy[X]``  吃掉剩余全部参数并逐个转换成一个列表
``*args`` / ``**kwargs``
              不支持：装饰时直接抛 ``TypeError``（同上游对前缀命令的限制）
============  ==========================================================================

其它约定：参数缺失且有默认值用默认值；多出来的参数按 ``ignore_extra`` 处理
（默认忽略，``ignore_extra=False`` 抛 ``TooManyArguments``）；引号包裹的参数算一个
（``!say "hello world"``）。
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import Callable, Iterator
from typing import Any, get_args, get_origin

from ...errors import (
    BadArgument,
    BadLiteralArgument,
    BadUnionArgument,
    CheckFailure,
    CommandError,
    CommandInvokeError,
    CommandOnCooldown,
    MissingRequiredArgument,
    TooManyArguments,
)
from ...utils import MISSING
from .cooldowns import BucketType, Cooldown, CooldownMapping, MaxConcurrency
from .view import get_signature_parameters

__all__ = [
    "Command",
    "Group",
    "Greedy",
    "Parameter",
    "command",
    "cooldown",
    "group",
    "hybrid_command",
    "hybrid_group",
    "max_concurrency",
]

#: 装饰器在函数上留的标记。
_CHECKS_ATTR = "__commands_checks__"
_COOLDOWN_ATTR = "__commands_cooldown__"
_MAX_CONCURRENCY_ATTR = "__commands_max_concurrency__"


class Greedy:
    """``Greedy[X]``：把这个参数之后的所有参数都转成 X 并收集成列表。"""

    def __init__(self, converter: Any = str) -> None:
        self.converter = converter

    def __class_getitem__(cls, item: Any) -> "Greedy":
        return cls(item)

    def __repr__(self) -> str:
        return f"Greedy[{getattr(self.converter, '__name__', self.converter)}]"


class Parameter:
    """一条命令参数（上游 ``Command.params`` 里那种对象的替身）。"""

    def __init__(
        self,
        *,
        name: str,
        kind: Any,
        annotation: Any,
        default: Any,
        display_name: str = "",
        greedy: Any = None,
    ) -> None:
        self.name = name
        self.kind = kind
        self.annotation = annotation
        self.default = default
        self.display_name = display_name or name
        self.greedy = greedy

    @property
    def required(self) -> bool:
        return self.default is inspect.Parameter.empty and not _is_optional(self.annotation)

    @property
    def is_keyword_only(self) -> bool:
        return self.kind is inspect.Parameter.KEYWORD_ONLY

    def __repr__(self) -> str:
        return f"<Parameter {self.name} required={self.required}>"


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #


def _is_optional(annotation: Any) -> bool:
    """``Optional[X]`` / ``Union[X, None]``。"""
    origin = get_origin(annotation)
    return origin is typing.Union and type(None) in get_args(annotation)


def _union_members(annotation: Any) -> tuple[Any, ...]:
    return tuple(item for item in get_args(annotation) if item is not type(None))


def _is_literal(annotation: Any) -> bool:
    return get_origin(annotation) is typing.Literal


def _wrap_callback(func: Any, owner: Any) -> Any:
    """给回调注入 ``self``（命令属于 Cog 时）。"""
    if owner is None:
        return func
    if inspect.ismethod(func) and getattr(func, "__self__", None) is owner:
        return func

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        return func(owner, *args, **kwargs)

    return wrapper


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


def _builtin_bool(argument: str) -> bool:
    """``bool`` 的转换（见模块 docstring：只认明确的真假写法）。"""
    text = str(argument).strip().lower()
    if text in ("true", "t", "yes", "y", "on", "1"):
        return True
    if text in ("false", "f", "no", "n", "off", "0"):
        return False
    raise ValueError(f"{argument!r} is not a valid boolean")


#: 注解 -> 内置转换器（``discord.*`` 类型走 converter 模块里的实现）。
_BUILTIN_CONVERTERS: dict[Any, Callable[[str], Any]] = {
    str: lambda value: str(value),
    int: int,
    float: float,
    bool: _builtin_bool,
}


def _discord_type_converter(annotation: Any) -> Any:
    """``discord.Member`` 这类注解 → 对应转换器实例。"""
    from ...channel import TextChannel, Thread
    from ...colour import Colour
    from ...emoji import Emoji, PartialEmoji
    from ...guild import Guild, Role
    from ...message import Message
    from ...user import Member, User
    from .converter import (
        ChannelConverter,
        ColourConverter,
        EmojiConverter,
        GuildConverter,
        MemberConverter,
        MessageConverter,
        PartialEmojiConverter,
        RoleConverter,
        TextChannelConverter,
        UserConverter,
    )

    mapping = {
        Member: MemberConverter(),
        User: UserConverter(),
        Message: MessageConverter(),
        TextChannel: TextChannelConverter(),
        Thread: ChannelConverter(),
        Guild: GuildConverter(),
        Role: RoleConverter(),
        Emoji: EmojiConverter(),
        PartialEmoji: PartialEmojiConverter(),
        Colour: ColourConverter(),
    }
    for key, converter in mapping.items():
        if annotation is key:
            return converter
    # ``discord.abc.GuildChannel`` / ``discord.abc.Messageable`` 这类宽泛注解
    from ...abc import GuildChannel, Messageable, PrivateChannel, Snowflake

    if annotation is GuildChannel or annotation is PrivateChannel:
        return ChannelConverter()
    if annotation is Messageable:
        return ChannelConverter()
    if annotation is Snowflake:
        from .converter import ObjectConverter

        return ObjectConverter()
    return None


class _UnsupportedAnnotation(BadArgument):
    """注解不是本 shim 支持的转换目标（明确报出来，而不是瞎猜）。"""

    def __init__(self, param: Parameter, annotation: Any) -> None:
        name = getattr(annotation, "__name__", None) or str(annotation)
        super().__init__(param.display_name, name=f"UnsupportedAnnotation({name})")
        self.param = param


async def convert_argument(ctx: Any, param: Parameter, argument: Any) -> Any:
    """把一个原始参数按注解转换（上游 ``Command.transform`` 的核心）。"""
    annotation = param.annotation
    if annotation is inspect.Parameter.empty or annotation is Any or annotation is str:
        return str(argument)
    if annotation in (int, float, bool):
        try:
            return _BUILTIN_CONVERTERS[annotation](str(argument))
        except (TypeError, ValueError) as exc:
            raise BadArgument(str(argument), name=getattr(annotation, "__name__", "value")) from exc
    if _is_literal(annotation):
        candidates = get_args(annotation)
        for candidate in candidates:
            if str(candidate) == str(argument):
                return candidate
        raise BadLiteralArgument(param, [argument], candidates)
    if isinstance(annotation, Greedy):
        return await _convert_greedy(ctx, param, str(argument))
    origin = get_origin(annotation)
    if origin is typing.Union:
        errors: list[BaseException] = []
        for member in _union_members(annotation):
            try:
                return await convert_argument(ctx, _derive(param, member), argument)
            except BadArgument as exc:
                errors.append(exc)
        raise BadUnionArgument(param, _union_members(annotation), errors)
    converter = _discord_type_converter(annotation)
    if converter is None and isinstance(annotation, type):
        from .converter import Converter

        if issubclass(annotation, Converter):
            converter = annotation()
    if converter is None and not inspect.isclass(annotation) and hasattr(annotation, "convert"):
        converter = annotation
    if converter is None and inspect.isclass(annotation) and hasattr(annotation, "convert"):
        converter = annotation()
    if converter is None:
        raise _UnsupportedAnnotation(param, annotation)
    try:
        return await _maybe_await(converter.convert(ctx, str(argument)))
    except CommandError:
        raise
    except Exception as exc:  # noqa: BLE001 — 转换器自己的异常要转成结构化失败
        raise BadArgument(str(argument), name=getattr(annotation, "__name__", "converter")) from exc


def _derive(param: Parameter, annotation: Any) -> Parameter:
    """派生一个换了注解的参数（``Union`` 分支 / ``Greedy`` 元素用）。"""
    return Parameter(
        name=param.name,
        kind=param.kind,
        annotation=annotation,
        default=param.default,
        display_name=param.display_name,
    )


async def _convert_greedy(ctx: Any, param: Parameter, raw: str) -> list[Any]:
    """``Greedy[X]``：把剩余原文分词后逐个转换。"""
    tokens = _split_tokens(raw)
    inner = _derive(param, param.greedy)
    return [await convert_argument(ctx, inner, token) for token in tokens]


def _split_tokens(raw: str) -> list[str]:
    """按空白切分（尊重引号，与参数解析同一套规则）。"""
    from .view import StringView

    view = StringView(raw)
    tokens: list[str] = []
    while not view.eof:
        token = view.get_quoted_word()
        if token is None:
            break
        if token:
            tokens.append(token)
    return tokens


# --------------------------------------------------------------------------- #
# Command
# --------------------------------------------------------------------------- #


class Command:
    """一条命令（上游 ``discord.ext.commands.Command`` 的子集）。"""

    def __init__(self, func: Any, **kwargs: Any) -> None:
        if not callable(func):
            raise TypeError("The command callback must be a callable")
        self.callback = func
        self.cog: Any = None
        self.parent: Group | None = kwargs.pop("parent", None)
        self.name: str = kwargs.pop("name", None) or getattr(func, "__name__", "command")
        if not self.name or not str(self.name).strip():
            raise ValueError("Command names cannot be empty")
        self.aliases: list[str] = [str(item) for item in kwargs.pop("aliases", ()) or ()]
        self.help: str | None = kwargs.pop("help", None)
        self.brief: str | None = kwargs.pop("brief", None)
        self.usage: str | None = kwargs.pop("usage", None)
        self.extras: dict[str, Any] = dict(kwargs.pop("extras", {}) or {})
        self.hidden: bool = bool(kwargs.pop("hidden", False))
        self.enabled: bool = bool(kwargs.pop("enabled", True))
        self.ignore_extra: bool = bool(kwargs.pop("ignore_extra", True))
        self.rest_is_raw: bool = bool(kwargs.pop("rest_is_raw", False))
        self.require_var_positional: bool = bool(kwargs.pop("require_var_positional", False))
        self.hybrid: bool = bool(kwargs.pop("hybrid", False))
        # 上游允许把 description 当 help 用（hybrid_command 需要它）。
        description = kwargs.pop("description", None)
        self.checks: list[Any] = list(getattr(func, _CHECKS_ATTR, ()) or ())
        self.checks.extend(kwargs.pop("checks", ()) or ())
        cooldown_value = kwargs.pop("cooldown", None) or getattr(func, _COOLDOWN_ATTR, None)
        self._buckets = CooldownMapping(cooldown_value) if cooldown_value is not None else CooldownMapping()
        self._max_concurrency = kwargs.pop("max_concurrency", None) or getattr(func, _MAX_CONCURRENCY_ATTR, None)
        if kwargs:
            raise TypeError(f"Unknown kwargs: {', '.join(sorted(kwargs))}")
        for attribute in (_CHECKS_ATTR, _COOLDOWN_ATTR, _MAX_CONCURRENCY_ATTR):
            if hasattr(func, attribute):
                try:
                    delattr(func, attribute)
                except AttributeError:  # pragma: no cover — 已经被别处删掉
                    pass
        self.on_error: Any = None
        self._before_invoke: Any = None
        self._after_invoke: Any = None
        self._params = get_signature_parameters(func)
        self.params: dict[str, Parameter] = {}
        for name, signature_param in self._params.items():
            if signature_param.kind is inspect.Parameter.VAR_POSITIONAL:
                raise TypeError(
                    f"command '{self.name}': *args is not supported by the bot_bridge shim; "
                    "use a keyword-only rest parameter or Greedy[...]"
                )
            if signature_param.kind is inspect.Parameter.VAR_KEYWORD:
                raise TypeError(
                    f"command '{self.name}': **kwargs is not supported by the bot_bridge shim; "
                    "declare the parameters explicitly"
                )
            annotation = signature_param.annotation
            greedy = annotation.converter if isinstance(annotation, Greedy) else None
            self.params[name] = Parameter(
                name=name,
                kind=signature_param.kind,
                annotation=annotation,
                default=signature_param.default,
                display_name=name,
                greedy=greedy,
            )
        if description is not None and self.help is None:
            self.help = str(description)

    # ---- 视图 ------------------------------------------------------------ #

    @property
    def clean_params(self) -> dict[str, Parameter]:
        return dict(self.params)

    @property
    def full_parent_name(self) -> str:
        if self.parent is None:
            return ""
        root = self.parent
        while root.parent is not None:
            root = root.parent
        parts: list[str] = []
        current: Group | None = root
        while current is not None:
            parts.append(current.name)
            if current is self:  # pragma: no cover — 只用于路径拼接
                break
            current = current.parent
        return " ".join(parts)

    @property
    def qualified_name(self) -> str:
        if self.parent is None:
            return self.name
        return f"{self.parent.qualified_name} {self.name}"

    @property
    def root_parent(self) -> "Command":
        parent = self.parent
        while parent is not None and parent.parent is not None:
            parent = parent.parent
        return parent or self

    @property
    def signature(self) -> str:
        """参数的展示形式（``<必填> [可选]``），与上游风格一致。"""
        parts: list[str] = []
        for param in self.params.values():
            if param.required:
                parts.append(f"<{param.display_name}>")
            else:
                parts.append(f"[{param.display_name}]")
        return " ".join(parts)

    @property
    def help(self) -> str | None:  # type: ignore[no-redef]
        return self._help if self._help is not None else (self.callback.__doc__ or None)

    @help.setter
    def help(self, value: Any) -> None:
        self._help = None if value is None else str(value)

    @property
    def brief(self) -> str | None:  # type: ignore[no-redef]
        if self._brief:
            return self._brief
        doc = self.callback.__doc__
        return doc.strip().splitlines()[0] if doc else None

    @brief.setter
    def brief(self, value: Any) -> None:
        self._brief = None if value is None else str(value)

    @property
    def short_doc(self) -> str:
        return self.brief or ""

    @property
    def description(self) -> str:
        return self.help or ""

    def error(self, coro: Any) -> Any:
        """``@cmd.error``：这条命令自己的错误处理器。"""
        self.on_error = coro
        return coro

    def before_invoke(self, coro: Any) -> Any:
        self._before_invoke = coro
        return coro

    def after_invoke(self, coro: Any) -> Any:
        self._after_invoke = coro
        return coro

    def add_check(self, func: Any) -> None:
        self.checks.append(func)

    def remove_check(self, func: Any) -> None:
        if func in self.checks:
            self.checks.remove(func)

    def update(self, **kwargs: Any) -> None:
        """就地更新属性（上游 ``Command.update``，供 ``copy`` / 帮助命令使用）。"""
        for key, value in kwargs.items():
            if key == "name":
                root = self.root_parent
                if root is not self:
                    root.aliases.append(self.name)
                self.name = str(value)
            elif key in ("help", "brief", "usage"):
                setattr(self, key, value)
            elif key == "aliases":
                self.aliases = [str(item) for item in value]
            elif key == "checks":
                self.checks = list(value)
            elif key == "enabled":
                self.enabled = bool(value)
            elif key == "hidden":
                self.hidden = bool(value)
            else:
                raise TypeError(f"Unknown attribute: {key}")

    def copy(self) -> "Command":
        clone: Command = type(self).__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone.checks = list(self.checks)
        clone.aliases = list(self.aliases)
        clone.params = dict(self.params)
        return clone

    # ---- 运行 ------------------------------------------------------------ #

    async def __call__(self, context: Any, *args: Any, **kwargs: Any) -> Any:
        """直接调用命令回调（``Command.__call__``，上游同义）。"""
        if self.cog is not None and not inspect.ismethod(self.callback):
            return await self.callback(self.cog, context, *args, **kwargs)
        return await self.callback(context, *args, **kwargs)

    async def can_run(self, ctx: Any) -> bool:
        """检查全部前置条件（``CheckFailure`` 视为不可运行，与上游同义）。"""
        try:
            await self._verify_checks(ctx)
        except CheckFailure:
            return False
        return True

    async def _verify_checks(self, ctx: Any) -> None:
        for predicate in self.checks:
            result = predicate(ctx)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                raise CheckFailure(command=self)
        cog = self.cog
        if cog is not None:
            checker = getattr(cog, "cog_check", None)
            if checker is not None:
                result = checker(ctx)
                if inspect.isawaitable(result):
                    result = await result
                if not result:
                    raise CheckFailure(command=self)

    async def prepare(self, ctx: Any) -> None:
        """解析参数（上游 ``Command.prepare``）。"""
        await self._parse_arguments(ctx)

    async def _parse_arguments(self, ctx: Any) -> None:
        ctx.args = [ctx] if self.cog is None else [self.cog, ctx]
        ctx.kwargs = {}
        view = ctx.view
        for param in self.params.values():
            ctx.current_parameter = param
            if param.greedy is not None:
                raw = view.read_rest().strip()
                ctx.current_argument = raw
                ctx.args.append(await _convert_greedy(ctx, param, raw) if raw else [])
                continue
            if param.is_keyword_only:
                raw = view.read_rest().strip()
                if not raw:
                    if param.required:
                        raise MissingRequiredArgument(param)
                    ctx.kwargs[param.name] = self._default_for(param)
                    continue
                ctx.current_argument = raw
                ctx.kwargs[param.name] = await convert_argument(ctx, param, raw)
                continue
            token = view.get_quoted_word()
            if not token:
                if param.required:
                    raise MissingRequiredArgument(param)
                ctx.args.append(self._default_for(param))
                continue
            ctx.current_argument = token
            ctx.args.append(await convert_argument(ctx, param, token))
        ctx.current_parameter = None
        ctx.current_argument = None
        if not self.ignore_extra and view.read_rest().strip():
            raise TooManyArguments()

    @staticmethod
    def _default_for(param: Parameter) -> Any:
        """参数缺失时的取值：有默认值用默认值，``Optional`` 注解用 ``None``。"""
        if param.default is not inspect.Parameter.empty:
            return param.default
        return None

    def _cooldown_retry_after(self, ctx: Any) -> float | None:
        if not self._buckets.valid:
            return None
        return self._buckets.update_rate_limit(ctx.message)

    async def invoke(self, ctx: Any) -> None:
        """执行这条命令（上游 ``Command.invoke`` 的主流程）。"""
        from ... import _runtime

        _runtime.record_command(self.qualified_name)
        if not self.enabled:
            raise CommandError(f"command '{self.qualified_name}' is disabled")
        await self.prepare(ctx)
        retry_after = self._cooldown_retry_after(ctx)
        if retry_after:
            raise CommandOnCooldown(self._buckets.get_bucket(ctx.message), retry_after)
        await self._verify_checks(ctx)
        if self._max_concurrency is None:
            await self._run_body(ctx)
            return
        async with await self._max_concurrency.acquire(ctx.message):
            await self._run_body(ctx)

    async def _run_body(self, ctx: Any) -> None:
        """前置钩子 → 回调 → 后置钩子（后置钩子一定跑，与上游一致）。"""
        cog = self.cog
        try:
            if cog is not None:
                hook = getattr(cog, "cog_before_invoke", None)
                if hook is not None:
                    await _maybe_await(hook(ctx))
            if self._before_invoke is not None:
                await _maybe_await(_wrap_callback(self._before_invoke, cog)(ctx))
            await self._invoke_callback(ctx)
        finally:
            if cog is not None:
                hook = getattr(cog, "cog_after_invoke", None)
                if hook is not None:
                    await _maybe_await(hook(ctx))
            if self._after_invoke is not None:
                await _maybe_await(_wrap_callback(self._after_invoke, cog)(ctx))

    async def _invoke_callback(self, ctx: Any) -> None:
        # ``ctx.args`` 已经以 ``[cog?, ctx]`` 开头（见 ``_parse_arguments``），这里不能再补。
        try:
            await self.callback(*ctx.args, **ctx.kwargs)
        except CommandError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except NotImplementedError as exc:
            # 能力缺失原样上报（包成 CommandInvokeError 会把「哪个能力不支持」埋掉）。
            if getattr(exc, "code", "") == "unsupported_capability":
                raise
            raise CommandInvokeError(exc) from exc
        except Exception as exc:  # noqa: BLE001 — 回调异常按上游包成 CommandInvokeError
            raise CommandInvokeError(exc) from exc

    async def dispatch_error(self, ctx: Any, error: BaseException) -> None:
        """把命令错误交给三层处理器（上游 ``Command.dispatch_error``）。

        顺序与上游一致：``@cmd.error`` → ``cog_command_error`` → ``command_error`` 事件
        （``Bot.on_command_error`` + 监听器）。任一层抛错都会向上传播。
        """
        ctx.command_failed = True
        if self.on_error is not None:
            await _maybe_await(_wrap_callback(self.on_error, self.cog)(ctx, error))
        cog = self.cog
        if cog is not None:
            handler = getattr(cog, "cog_command_error", None)
            if handler is not None and not getattr(handler, "_bridge_base_impl", False):
                await _maybe_await(handler(ctx, error))
        await ctx.bot._bridge_dispatch("command_error", None, ctx, error)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Command):
            return self.name == other.name and self.callback is other.callback
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.name, self.callback))

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.qualified_name!r}>"


class Group(Command):
    """一组命令（上游 ``discord.ext.commands.Group``）。"""

    def __init__(self, func: Any, **kwargs: Any) -> None:
        self.invoke_without_command: bool = bool(kwargs.pop("invoke_without_command", False))
        self.case_insensitive: bool = bool(kwargs.pop("case_insensitive", False))
        super().__init__(func, **kwargs)
        self.commands: dict[str, Command] = {}

    @property
    def all_commands(self) -> dict[str, Command]:
        return dict(self.commands)

    def add_command(self, command: Command) -> None:
        if not isinstance(command, Command):
            raise TypeError("The command passed must be a subclass of Command")
        if command.parent is not None and command.parent is not self:
            raise ValueError(f"The given command {command.name} is already registered to {command.parent.name}")
        command.parent = self
        if self.case_insensitive:
            self.commands[command.name.lower()] = command
            for alias in command.aliases:
                self.commands[alias.lower()] = command
            return
        self.commands[command.name] = command
        for alias in command.aliases:
            self.commands[alias] = command

    def remove_command(self, name: str) -> Command | None:
        command = self.commands.pop(name, None)
        if command is None:
            return None
        for alias in list(command.aliases):
            if self.commands.get(alias) is command:
                self.commands.pop(alias, None)
        return command

    def get_command(self, name: str) -> Command | None:
        return self.commands.get(name.lower() if self.case_insensitive else name)

    def walk_commands(self) -> Iterator[Command]:
        """产出自己 + 全部子命令（上游 ``Group.walk_commands``）。"""
        yield self
        for command in self.commands.values():
            if isinstance(command, Group):
                yield from command.walk_commands()
            else:
                yield command

    def command(self, *args: Any, **kwargs: Any) -> Callable[[Any], Command]:
        """在组上注册一条子命令（``@group.command()``）。"""
        kwargs["parent"] = self

        def decorator(func: Any) -> Command:
            if isinstance(func, Command):
                raise TypeError("The command decorator cannot be applied to an existing Command")
            result = Command(func, *args, **kwargs)
            self.add_command(result)
            return result

        return decorator

    def group(self, *args: Any, **kwargs: Any) -> Callable[[Any], "Group"]:
        """在组上注册一个子命令组（``@group.group()``）。"""
        kwargs["parent"] = self

        def decorator(func: Any) -> "Group":
            result = Group(func, *args, **kwargs)
            self.add_command(result)
            return result

        return decorator

    async def invoke(self, ctx: Any) -> None:
        """执行组命令：先按下一个词解析子命令（上游 ``Group.invoke``）。"""
        view = ctx.view
        ctx.invoked_subcommand = None
        ctx.subcommand_passed = None
        if not view.eof:
            previous = view.index
            view.skip_ws()
            trigger = view.get_word()
            if trigger:
                ctx.subcommand_passed = trigger
                ctx.invoked_subcommand = self.get_command(trigger)
            if ctx.invoked_subcommand is None:
                # 没命中子命令：把游标还回去，让组自己的回调去解析剩余参数。
                view.index = previous
                view.previous = previous
        if ctx.invoked_subcommand is None:
            if not self.invoke_without_command:
                # 命令名命中了（消息归本插件处理），只是缺子命令 —— 记下来让上层如实报告。
                from ... import _runtime

                _runtime.record_command(self.qualified_name)
                raise CommandError(
                    f"group '{self.qualified_name}' requires a subcommand"
                    + (f" (got {ctx.subcommand_passed!r})" if ctx.subcommand_passed else "")
                )
            await super().invoke(ctx)
            return
        ctx.command = ctx.invoked_subcommand
        view.skip_ws()
        await ctx.invoked_subcommand.invoke(ctx)


# --------------------------------------------------------------------------- #
# 装饰器
# --------------------------------------------------------------------------- #


def _make_decorator(cls: type, name: Any, extra: dict[str, Any]) -> Callable[[Any], Any]:
    def decorator(func: Any) -> Any:
        if isinstance(func, Command):
            raise TypeError("The command decorator cannot be applied to an existing Command")
        resolved = None if name is MISSING else name
        return cls(func, name=resolved, **extra)

    return decorator


def command(name: Any = MISSING, cls: Any = MISSING, **attrs: Any) -> Any:
    """``@commands.command``（也支持 ``@commands.command("名字")`` 与不带括号）。"""
    if callable(name) and cls is MISSING:
        func = name
        return _make_decorator(Command, MISSING, attrs)(func)
    target = Command if cls is MISSING else cls
    return _make_decorator(target, name, attrs)


def group(name: Any = MISSING, cls: Any = MISSING, **attrs: Any) -> Any:
    """``@commands.group()``：一组命令，默认必须带子命令。"""
    if callable(name) and cls is MISSING:
        func = name
        return _make_decorator(Group, MISSING, attrs)(func)
    target = Group if cls is MISSING else cls
    return _make_decorator(target, name, attrs)


def hybrid_command(name: Any = MISSING, **attrs: Any) -> Any:
    """``@commands.hybrid_command()``：前缀调用可用，斜杠命令部分**不支持**。

    兼容层会把「斜杠命令没有 N.E.K.O. 等价实现」登记为能力缺失
    （``LoadedPlugin.native["unsupported_capabilities"]``），不做静默降级。
    """
    from ... import _runtime

    _runtime.note_unsupported(
        "commands.hybrid_command",
        "N.E.K.O. 没有斜杠命令注册面；hybrid_command 只有前缀调用可用",
    )
    attrs.setdefault("hybrid", True)
    return command(name, **attrs)


def hybrid_group(name: Any = MISSING, **attrs: Any) -> Any:
    """``@commands.hybrid_group()``：同上，组形态。"""
    from ... import _runtime

    _runtime.note_unsupported(
        "commands.hybrid_group",
        "N.E.K.O. 没有斜杠命令注册面；hybrid_group 只有前缀调用可用",
    )
    attrs.setdefault("hybrid", True)
    return group(name, **attrs)


def cooldown(rate: int, per: float, type: BucketType = BucketType.default) -> Callable[[Any], Any]:  # noqa: A002
    """``@commands.cooldown(rate, per, BucketType.user)``。"""

    def decorator(func: Any) -> Any:
        func.__commands_cooldown__ = Cooldown(rate, per, type)
        return func

    return decorator


def max_concurrency(
    number: int, per: BucketType = BucketType.default, *, wait: bool = False
) -> Callable[[Any], Any]:
    """``@commands.max_concurrency(1, per=BucketType.default)``。"""

    def decorator(func: Any) -> Any:
        func.__commands_max_concurrency__ = MaxConcurrency(number, per=per, wait=wait)
        return func

    return decorator
