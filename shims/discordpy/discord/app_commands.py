"""``discord.app_commands``：斜杠命令（**显式不支持**，不静默）。

N.E.K.O. 里没有 Discord 交互面，因此：

* 装饰器（``command`` / ``describe`` / ``choices`` / ``autocomplete`` / ``check`` …）都能用，
  它们只做**元数据记录**：把「用到了 app_commands」登记成能力缺失（写进当前插件的
  记录，兼容层会转成 ``LoadedPlugin.native["unsupported_capabilities"]``），函数照常返回；
* 注册动作不会假装成功：``CommandTree.sync()`` / ``fetch_commands()`` 抛
  :class:`UnsupportedCapabilityError`（带 ``to_payload()``）；``add_command()`` 会把命令
  记进树里（``get_commands()`` 能看见）但不会有任何东西调用它 —— 这一点在能力清单里
  写得很清楚，而不是静默丢弃。

结论：只写斜杠命令的插件可以加载、可以列出来，但命令永远不会被触发；兼容层的日志与
``native`` 里会明确说「app_commands 没有 N.E.K.O. 等价实现」。
"""

from __future__ import annotations

from collections.abc import Iterator
from enum import Enum
from typing import Any

from . import _runtime
from .errors import DiscordException

__all__ = [
    "AppCommandError",
    "Choice",
    "Command",
    "CommandInvokeError",
    "CommandNotFound",
    "CommandSignatureMismatch",
    "CommandTree",
    "CheckFailure",
    "Group",
    "Locale",
    "Namespace",
    "Parameter",
    "Range",
    "TranslationError",
    "allowed_contexts",
    "allowed_installs",
    "autocomplete",
    "check",
    "check_any",
    "choices",
    "command",
    "default_permissions",
    "describe",
    "guilds",
    "rename",
]

#: 记录能力缺失时统一的 capability 前缀。
_CAPABILITY = "app_commands"

#: 说明文案（进插件的能力清单）。
_DETAIL = "N.E.K.O. 没有斜杠命令 / 交互输入通道；app_commands 只做元数据记录，命令不会被触发"


# --------------------------------------------------------------------------- #
# 异常（与上游 app_commands 同名）
# --------------------------------------------------------------------------- #


class AppCommandError(DiscordException):
    """app_commands 错误的基类。"""


class CommandInvokeError(AppCommandError):
    """命令回调抛异常（``original`` 是原始异常）。"""

    def __init__(self, command: Any, e: BaseException) -> None:
        self.command = command
        self.original = e
        super().__init__(f"Command {getattr(command, 'qualified_name', command)!r} raised an exception: {e!r}")


class CommandNotFound(AppCommandError):
    """没找到这个斜杠命令。"""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"Command {name!r} not found.")


class CommandSignatureMismatch(AppCommandError):
    """命令签名与 Discord 的要求不符。"""


class CheckFailure(AppCommandError):
    """斜杠命令的前置检查失败。"""


class TranslationError(AppCommandError):
    """本地化翻译失败。"""


class Locale(Enum):
    """地区码（只列常用值，取上游数值意义 —— 这里只做标识）。"""

    english_us = "en-US"
    english_uk = "en-GB"
    chinese_simplified = "zh-CN"
    chinese_traditional = "zh-TW"
    japanese = "ja"
    korean = "ko"

    def __str__(self) -> str:
        return self.value


# --------------------------------------------------------------------------- #
# 元数据对象
# --------------------------------------------------------------------------- #


class Choice:
    """一个候选项（``name`` 展示、``value`` 实际值）。"""

    def __init__(self, name: str, value: Any) -> None:
        self.name = str(name)
        self.value = value

    def __iter__(self) -> Iterator[Any]:
        yield self.name
        yield self.value

    def __repr__(self) -> str:
        return f"Choice(name={self.name!r}, value={self.value!r})"


class Parameter:
    """斜杠命令的参数元数据（只记录，不做校验/自动补全）。"""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        required: bool = False,
        choices: Any = None,
        autocomplete: Any = None,
        default: Any = None,
    ) -> None:
        self.name = str(name)
        self.description = str(description)
        self.required = bool(required)
        self.choices = list(choices or ())
        self.autocomplete = autocomplete
        self.default = default
        self.type: Any = None
        self.range: Any = None

    def __repr__(self) -> str:
        return f"<Parameter name={self.name!r} required={self.required}>"


class Range:
    """``Range[int, 1, 10]``：注解存在即可（不校验，因为命令不会执行）。"""

    def __class_getitem__(cls, params: Any) -> Any:
        base = params[0] if isinstance(params, tuple) else params
        _runtime.note_unsupported(f"{_CAPABILITY}.Range", f"数值范围不校验：{_DETAIL}")
        return base


class Namespace:
    """斜杠命令的参数命名空间（``interaction.namespace``）。"""

    def __init__(self, **kwargs: Any) -> None:
        self.__dict__.update(kwargs)

    def __iter__(self) -> Iterator[Any]:
        return iter(self.__dict__.items())

    def __repr__(self) -> str:
        return f"<Namespace {self.__dict__!r}>"


class _CommandBase:
    """``Command`` / ``Group`` 的共同部分。"""

    def __init__(self, *, name: str, description: str, callback: Any = None, parent: Any = None, **kwargs: Any) -> None:
        self.name = str(name)
        self.description = str(description)
        self.callback = callback
        self.parent = parent
        self.nsfw = bool(kwargs.pop("nsfw", False))
        self.guild_ids = kwargs.pop("guild_ids", None)
        self.checks: list[Any] = []
        self._on_error: Any = None
        self._autocompletes: dict[str, Any] = {}
        self.extras: dict[str, Any] = dict(kwargs.pop("extras", {}) or {})
        if kwargs:
            # 上游会拒绝未知参数；这里记下来但不炸（插件在真机上经常带新版本参数）。
            self.extras.setdefault("unknown_kwargs", sorted(kwargs))

    @property
    def qualified_name(self) -> str:
        if self.parent is None:
            return self.name
        return f"{self.parent.qualified_name} {self.name}"

    @property
    def root_parent(self) -> Any:
        parent = self.parent
        while getattr(parent, "parent", None) is not None:
            parent = parent.parent
        return parent or self

    def error(self, coro: Any) -> Any:
        self._on_error = coro
        return coro

    def autocomplete(self, name: str) -> Any:
        def decorator(func: Any) -> Any:
            self._autocompletes[str(name)] = func
            return func

        return decorator

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.qualified_name!r}>"


class Command(_CommandBase):
    """一个斜杠命令（只作为元数据存在）。"""

    def __init__(self, *, name: str, description: str, callback: Any = None, parent: Any = None, **kwargs: Any) -> None:
        super().__init__(name=name, description=description, callback=callback, parent=parent, **kwargs)
        self.parameters: dict[str, Parameter] = {}
        _runtime.note_unsupported(f"{_CAPABILITY}.Command", _DETAIL)

    def __call__(self, interaction: Any, /) -> Any:
        if self.callback is None:
            raise _runtime.unsupported(f"{_CAPABILITY}.Command", "这个斜杠命令没有回调")
        return self.callback(interaction)


class Group(_CommandBase):
    """一组斜杠命令。"""

    def __init__(self, *, name: str, description: str, callback: Any = None, parent: Any = None, **kwargs: Any) -> None:
        super().__init__(name=name, description=description, callback=callback, parent=parent, **kwargs)
        self._commands: dict[str, Any] = {}
        _runtime.note_unsupported(f"{_CAPABILITY}.Group", _DETAIL)

    @property
    def commands(self) -> list[Any]:
        return list(self._commands.values())

    def add_command(self, command: Any, /, *, override: bool = False) -> None:
        if command.name in self._commands and not override:
            raise CommandSignatureMismatch(f"Command {command.name!r} already exists in group {self.name!r}")
        command.parent = self
        self._commands[command.name] = command

    def remove_command(self, name: str, /) -> Any:
        return self._commands.pop(name, None)

    def get_command(self, name: str, /) -> Any:
        return self._commands.get(name)

    def walk_commands(self) -> Iterator[Any]:
        yield self
        for command in self.commands:
            if isinstance(command, Group):
                yield from command.walk_commands()
            else:
                yield command

    def command(self, *, name: str, description: str, **kwargs: Any) -> Any:
        """``@group.command()``：注册子命令。"""
        override = bool(kwargs.pop("override", False))

        def decorator(func: Any) -> Command:
            child = Command(name=name, description=description, callback=func, **kwargs)
            self.add_command(child, override=override)
            return child

        return decorator

    def group(self, *, name: str, description: str, **kwargs: Any) -> Any:
        """``@group.group()``：注册子命令组。"""

        def decorator(func: Any) -> Group:
            child = Group(name=name, description=description, callback=func, **kwargs)
            self.add_command(child, override=False)
            return child

        return decorator


# --------------------------------------------------------------------------- #
# CommandTree
# --------------------------------------------------------------------------- #


class CommandTree:
    """``bot.tree``：斜杠命令注册树。

    只维护本地注册表（``get_commands`` / ``walk_commands`` / ``clear_commands`` 是真实行为），
    真正的注册动作（``sync`` / ``fetch_commands``）**显式抛能力缺失**。
    """

    def __init__(self, client: Any) -> None:
        self.client = client
        self._commands: dict[str, Any] = {}
        self._on_error: Any = None
        _runtime.note_unsupported(f"{_CAPABILITY}.CommandTree", _DETAIL)

    # ---- 注册表 ---------------------------------------------------------- #

    @property
    def commands(self) -> list[Any]:
        return list(self._commands.values())

    def get_commands(self, *, guild: Any = None) -> list[Any]:
        del guild
        return list(self._commands.values())

    def get_command(self, name: str, /, *, guild: Any = None) -> Any:
        del guild
        return self._commands.get(name)

    def walk_commands(self) -> Iterator[Any]:
        for command in self.commands:
            if isinstance(command, Group):
                yield from command.walk_commands()
            else:
                yield command

    def add_command(self, command: Any, /, *, guild: Any = None, override: bool = False) -> None:
        """把命令记进树里（它永远不会被触发 —— 原因记在插件的能力清单里）。"""
        del guild
        if command.name in self._commands and not override:
            existing = self._commands[command.name]
            if existing is not command:
                raise CommandSignatureMismatch(f"Command {command.name!r} already exists")
        self._commands[command.name] = command

    def remove_command(self, name: str, /, *, guild: Any = None) -> bool:
        del guild
        return self._commands.pop(name, None) is not None

    def clear_commands(self, *, guild: Any = None) -> None:
        """清空本地注册表（不涉及任何远端同步）。"""
        del guild
        self._commands.clear()

    def copy_global_to(self, *, guild: Any) -> None:
        """上游把全局命令复制成服务器命令；这里没有远端，只登记能力缺失。"""
        del guild
        _runtime.note_unsupported(f"{_CAPABILITY}.copy_global_to", _DETAIL)

    def error(self, coro: Any) -> Any:
        self._on_error = coro
        return coro

    # ---- 与远端交互（显式不支持） ---------------------------------------- #

    async def sync(self, *, guild: Any = None) -> list[Any]:
        raise _runtime.unsupported(
            f"{_CAPABILITY}.sync", "N.E.K.O. 没有斜杠命令注册面（Discord Application Command API）"
        )

    async def fetch_commands(self, *, guild: Any = None) -> list[Any]:
        raise _runtime.unsupported(f"{_CAPABILITY}.fetch_commands", "N.E.K.O. 没有斜杠命令注册面")

    # ---- 装饰器 ---------------------------------------------------------- #

    def command(self, *, name: str = "", description: str = "", **kwargs: Any) -> Any:
        """``@tree.command()``：造一个 :class:`Command` 并登记进树。"""

        def decorator(func: Any) -> Command:
            command = Command(
                name=name or getattr(func, "__name__", "command"),
                description=_default_description(func, description),
                callback=func,
                **kwargs,
            )
            self.add_command(command)
            return command

        return decorator


# --------------------------------------------------------------------------- #
# 装饰器（全部只做元数据记录）
# --------------------------------------------------------------------------- #


def _record(name: str, detail: str = "") -> None:
    _runtime.note_unsupported(f"{_CAPABILITY}.{name}", detail or _DETAIL)


def _default_description(func: Any, description: str) -> str:
    """没给 description 时退化到函数 docstring 的第一行。"""
    if description:
        return str(description)
    doc = (getattr(func, "__doc__", "") or "").strip()
    return doc.splitlines()[0] if doc else ""


def command(*, name: str = "", description: str = "", **kwargs: Any) -> Any:
    """``@app_commands.command()``：返回元数据对象（不会被触发）。"""

    def decorator(func: Any) -> Command:
        return Command(
            name=name or getattr(func, "__name__", "command"),
            description=_default_description(func, description),
            callback=func,
            **kwargs,
        )

    return decorator


def describe(**kwargs: Any) -> Any:
    """``@app_commands.describe(...)``。"""
    del kwargs
    _record("describe")

    def decorator(target: Any) -> Any:
        return target

    return decorator


def choices(**kwargs: Any) -> Any:
    """``@app_commands.choices(...)``。"""
    del kwargs
    _record("choices")

    def decorator(target: Any) -> Any:
        return target

    return decorator


def autocomplete(**kwargs: Any) -> Any:
    """``@app_commands.autocomplete(...)``。"""
    del kwargs
    _record("autocomplete")

    def decorator(target: Any) -> Any:
        return target

    return decorator


def rename(**kwargs: Any) -> Any:
    """``@app_commands.rename(...)``。"""
    del kwargs
    _record("rename")

    def decorator(target: Any) -> Any:
        return target

    return decorator


def guilds(*guild_ids: Any) -> Any:
    """``@app_commands.guilds(...)``：限定可用服务器的装饰器。"""
    del guild_ids
    _record("guilds")

    def decorator(target: Any) -> Any:
        return target

    return decorator


def allowed_contexts(*contexts: Any) -> Any:
    """``@app_commands.allowed_contexts(...)``。"""
    del contexts
    _record("allowed_contexts")

    def decorator(target: Any) -> Any:
        return target

    return decorator


def allowed_installs(**kwargs: Any) -> Any:
    """``@app_commands.allowed_installs(...)``。"""
    del kwargs
    _record("allowed_installs")

    def decorator(target: Any) -> Any:
        return target

    return decorator


def default_permissions(**perms: bool) -> Any:
    """``@app_commands.default_permissions(...)``。"""
    del perms
    _record("default_permissions")

    def decorator(target: Any) -> Any:
        return target

    return decorator


def check(predicate: Any) -> Any:
    """``@app_commands.check``（只记录，不执行）。"""
    _record("check")

    def decorator(target: Any) -> Any:
        if isinstance(target, _CommandBase):
            target.checks.append(predicate)
        return target

    return decorator


def check_any(*predicates: Any) -> Any:
    """``@app_commands.check_any``。"""
    del predicates
    _record("check_any")

    def decorator(target: Any) -> Any:
        return target

    return decorator
