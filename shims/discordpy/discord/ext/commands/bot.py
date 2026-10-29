"""``discord.ext.commands.bot``：``Bot`` / ``AutoShardedBot``。

``Bot`` 是「能挂 Cog 的客户端」：命令注册表、前缀解析、``process_commands``、
错误处理链、扩展加载（``load_extension``）。差异见 ``discord.client`` 的 docstring
（没有网关、``dispatch`` 直接 await、Cog 能力上提到 ``Client``）。
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ... import _runtime
from ...errors import (
    CheckFailure,
    CommandNotFound,
    CommandRegistrationError,
    ExtensionAlreadyLoaded,
    ExtensionError,
    ExtensionFailed,
    ExtensionNotLoaded,
    ExtensionNotFound,
    NoEntryPointError,
)
from ...client import Client
from ...utils import MISSING
from .context import Context
from .core import Command, Group, command, group, hybrid_command, hybrid_group
from .view import StringView

__all__ = ["AutoShardedBot", "Bot", "BotBase"]


class BotBase(Client):
    """命令注册与检查的公共部分（上游同名的内部基类，这里保留名字供 isinstance 使用）。"""


class Bot(BotBase):
    """一个命令机器人（``discord.ext.commands.Bot`` 的子集）。"""

    def __init__(
        self,
        command_prefix: Any = MISSING,
        help_command: Any = MISSING,
        description: str | None = None,
        intents: Any = None,
        **options: Any,
    ) -> None:
        self.case_insensitive: bool = bool(options.pop("case_insensitive", False))
        self.owner_ids: set[int] = set(options.pop("owner_ids", ()) or ())
        self.strip_after_prefix: bool = bool(options.pop("strip_after_prefix", False))
        #: 命令名 -> Command（含别名；``case_insensitive`` 时键都是小写）
        self._commands: dict[str, Command] = {}
        self._checks: list[Any] = []
        self._before_invoke: Any = None
        self._after_invoke: Any = None
        #: ``load_extension`` 加载过的扩展（名 -> 模块）
        self.extensions: dict[str, Any] = {}
        super().__init__(intents=intents, **options)
        self.command_prefix = "!" if command_prefix is MISSING else command_prefix
        self.description = description
        self.help_command = None
        if help_command is not MISSING and help_command is not None:
            # 上游的 HelpCommand 体系（``commands.HelpCommand`` 子类）没有等价实现；
            # 这里如实登记，而不是假装装上了。
            _runtime.note_unsupported(
                "commands.HelpCommand",
                "N.E.K.O. 侧不提供 HelpCommand 体系；ctx.send_help() 仍可用（内置最小帮助）",
            )
        self._bridge_owner_ids = set(self.owner_ids)

    # ---- 命令注册表 ------------------------------------------------------ #

    @property
    def all_commands(self) -> dict[str, Command]:
        return dict(self._commands)

    def get_command(self, name: str) -> Command | None:
        if not isinstance(name, str):
            getter = getattr(name, "name", "")
            if not getter:
                return None
            return self._commands.get(getter.lower() if self.case_insensitive else getter)
        return self._commands.get(name.lower() if self.case_insensitive else name)

    def add_command(self, command: Command) -> None:
        """注册一条命令（含别名）；重名抛 ``CommandRegistrationError``（上游同义）。"""
        if not isinstance(command, Command):
            raise TypeError("The command passed must be a subclass of Command")
        names = [command.name, *command.aliases]
        for name in names:
            existing = self._commands.get(name.lower() if self.case_insensitive else name)
            if existing is None:
                continue
            if existing is command:
                return
            raise CommandRegistrationError(name, alias_conflict=name != command.name)
        for name in names:
            self._commands[name.lower() if self.case_insensitive else name] = command

    def remove_command(self, name: str) -> Command | None:
        command = self._commands.get(name.lower() if self.case_insensitive else name)
        if command is None:
            return None
        for key in [key for key, value in self._commands.items() if value is command]:
            self._commands.pop(key, None)
        return command

    def command(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        """``@bot.command()``：在机器人上注册一条命令。"""
        decorator = command(*args, **kwargs)

        def wrapper(func: Any) -> Any:
            result = decorator(func)
            self.add_command(result)
            return result

        return wrapper

    def group(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        """``@bot.group()``：注册一组命令。"""
        decorator = group(*args, **kwargs)

        def wrapper(func: Any) -> Any:
            result = decorator(func)
            self.add_command(result)
            return result

        return wrapper

    def hybrid_command(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        """``@bot.hybrid_command()``（斜杠命令部分不支持，前缀调用可用）。"""
        decorator = hybrid_command(*args, **kwargs)

        def wrapper(func: Any) -> Any:
            result = decorator(func)
            self.add_command(result)
            return result

        return wrapper

    def hybrid_group(self, *args: Any, **kwargs: Any) -> Callable[[Any], Any]:
        """``@bot.hybrid_group()``。"""
        decorator = hybrid_group(*args, **kwargs)

        def wrapper(func: Any) -> Any:
            result = decorator(func)
            self.add_command(result)
            return result

        return wrapper

    # ---- 全局检查 / 钩子 ------------------------------------------------- #

    def check(self, predicate: Any) -> Any:
        """注册一个全局检查（``@bot.check``）。"""

        def decorator(func: Any) -> Any:
            self._checks.append(predicate)
            return func

        return decorator

    def add_check(self, func: Any) -> None:
        self._checks.append(func)

    def remove_check(self, func: Any) -> None:
        if func in self._checks:
            self._checks.remove(func)

    def before_invoke(self, coro: Any) -> Any:
        self._before_invoke = coro
        return coro

    def after_invoke(self, coro: Any) -> Any:
        self._after_invoke = coro
        return coro

    async def can_run(self, ctx: Any) -> bool:
        """全局检查是否通过（``CheckFailure`` 视为 False，与上游同义）。"""
        for predicate in self._checks:
            result = predicate(ctx)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                return False
        if ctx.command is not None:
            return await ctx.command.can_run(ctx)
        return True

    # ---- Cog 挂钩（由 Client.add_cog / remove_cog 调用） ---------------- #

    def _register_cog_surface(self, cog: Any) -> None:
        """把 Cog 里的命令与监听器接进注册表。

        子命令（``@group.command()``）已经挂在组的 ``commands`` 里，不在这里重复登记。
        """
        for item in cog.get_commands():
            if getattr(item, "parent", None) is not None:
                continue
            item.cog = cog
            if isinstance(item, Group):
                for nested in item.walk_commands():
                    nested.cog = cog
            self.add_command(item)
        for event, listener in cog.get_listeners():
            self.add_listener(listener, event)

    def _unregister_cog_surface(self, cog: Any) -> None:
        for item in cog.get_commands():
            self.remove_command(item.name)
        for _event, listener in cog.get_listeners():
            target = getattr(listener, "__func__", listener)
            for key, bucket in self._listeners.items():
                self._listeners[key] = [
                    entry
                    for entry in bucket
                    if entry[1] is not cog and getattr(entry[0], "__func__", entry[0]) is not target
                ]

    # ---- 分发 ------------------------------------------------------------ #

    async def _resolve_prefix(self, message: Any) -> list[str]:
        """算出这次消息可用的前缀候选（``command_prefix`` 可以是串 / 列表 / 函数 / 协程）。"""
        prefix = self.command_prefix
        if callable(prefix):
            prefix = prefix(self, message)
        if inspect.isawaitable(prefix):
            prefix = await prefix
        if isinstance(prefix, str):
            candidates = [prefix]
        elif isinstance(prefix, (list, tuple, set, frozenset)):
            candidates = [str(item) for item in prefix]
        else:
            return []
        return sorted({item for item in candidates if item}, key=len, reverse=True)

    def _mention_prefixes(self) -> list[str]:
        user = self.user
        if user is None:
            return []
        return [f"<@{user.id}>", f"<@!{user.id}>"]

    async def get_context(self, message: Any, *, cls: Any = Context) -> Any:
        """把一条消息解析成 :class:`Context`（上游 ``Bot.get_context``）。"""
        view = StringView(getattr(message, "content", "") or "")
        ctx = cls(prefix=None, view=view, bot=self, message=message)
        if self._skip_check(getattr(message.author, "id", 0), getattr(self.user, "id", 0)):
            return ctx
        invoked_prefix = None
        for candidate in self._mention_prefixes():
            if view.skip_string(candidate):
                invoked_prefix = candidate
                break
        if invoked_prefix is not None:
            view.skip_ws()
        else:
            for candidate in await self._resolve_prefix(message):
                if view.skip_string(candidate):
                    invoked_prefix = candidate
                    break
            if invoked_prefix is None:
                return ctx
            if self.strip_after_prefix:
                view.skip_ws()
        ctx.prefix = invoked_prefix
        ctx.invoked_with = view.get_word()
        ctx.command = self.get_command(ctx.invoked_with)
        return ctx

    async def invoke(self, ctx: Any) -> None:
        """执行上下文里的命令（上游 ``Bot.invoke``）。"""
        if ctx.command is None:
            if ctx.invoked_with:
                # 前缀命中了但没这个命令：不是错误，只是没匹配（debug 级）。
                error = CommandNotFound(ctx.invoked_with)
                _runtime.emit_log("debug", f"{error}（前缀 {ctx.prefix!r}）", source="discord")
                await self._bridge_dispatch("command_error", None, ctx, error)
            return
        await self._bridge_dispatch("command", None, ctx)
        try:
            if self._before_invoke is not None:
                await _maybe_await(self._before_invoke(ctx))
            await self._run_global_checks(ctx)
            await ctx.command.invoke(ctx)
        except Exception as exc:  # noqa: BLE001 — 统一转成命令错误链（结构化）
            error = _as_command_error(exc)
            _runtime.record_command_error(ctx.command.qualified_name, error)
            if isinstance(error, CheckFailure):
                _runtime.record_check_failure(ctx.command.qualified_name, error)
            await ctx.command.dispatch_error(ctx, error)
        else:
            await self._bridge_dispatch("command_completion", None, ctx)
        finally:
            if self._after_invoke is not None:
                try:
                    await _maybe_await(self._after_invoke(ctx))
                except Exception as exc:  # noqa: BLE001 — 后置钩子失败不能吞掉命令结果
                    _runtime.record_error(
                        f"bot.after_invoke 抛错：{type(exc).__name__}: {exc}", level="error", source="discord"
                    )

    async def _run_global_checks(self, ctx: Any) -> None:
        for predicate in self._checks:
            result = predicate(ctx)
            if inspect.isawaitable(result):
                result = await result
            if not result:
                raise CheckFailure(command=ctx.command)

    async def process_commands(self, message: Any) -> None:
        """处理一条消息里的命令（上游 ``Bot.process_commands``）。"""
        if not getattr(message, "content", ""):
            return
        if getattr(getattr(message, "author", None), "bot", False):
            return
        ctx = await self.get_context(message)
        await self.invoke(ctx)

    async def on_message(self, message: Any) -> None:
        """默认的 ``on_message``：跑命令（被 ``@bot.event`` 覆盖时就不再自动跑）。"""
        await self.process_commands(message)

    async def on_command_error(self, ctx: Any, error: BaseException) -> None:
        """命令出错时的默认行为：写一条宿主日志。

        结构化记录已经在 ``Bot.invoke`` 里做过（``RunReport.command_errors``），
        这里只负责让日志里有迹可循 —— 上游同位置是打印 traceback。
        """
        name = getattr(getattr(ctx, "command", None), "qualified_name", "")
        detail = f"命令 {name or ctx.invoked_with!r} 出错：{type(error).__name__}: {error}"
        _runtime.emit_log("warning", detail, source="discord")
        self._bridge_errors.append(detail)

    # ---- 扩展 ------------------------------------------------------------ #

    def _resolve_extension(self, name: str) -> tuple[Path, bool] | None:
        """把扩展名解析成文件（相对本插件的目录），返回 ``(路径, 是否是包)``。"""
        base = Path(getattr(self, "_bridge_plugin_dir", "") or ".")
        parts = [part for part in str(name).split(".") if part]
        if not parts:
            return None
        for index in range(len(parts), 0, -1):
            head = base.joinpath(*parts[:index])
            if head.with_suffix(".py").is_file():
                return head.with_suffix(".py"), False
            if (head / "__init__.py").is_file():
                if index == len(parts):
                    return head / "__init__.py", True
                tail = parts[index:]
                nested = head.joinpath(*tail)
                if nested.with_suffix(".py").is_file():
                    return nested.with_suffix(".py"), False
        return None

    async def load_extension(self, name: str, *, package: str | None = None) -> None:
        """加载一个扩展（相对本插件目录解析，``setup(bot)`` 入口与上游一致）。"""
        if name in self.extensions:
            raise ExtensionAlreadyLoaded(name)
        resolved = self._resolve_extension(name)
        if resolved is None:
            raise ExtensionNotFound(name)
        path, is_package = resolved
        module_name = f"{getattr(self, '_bridge_module_prefix', '') or ''}ext_{name.replace('.', '_')}"
        try:
            if is_package:
                spec = importlib.util.spec_from_file_location(
                    module_name, path, submodule_search_locations=[str(path.parent)]
                )
            else:
                spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ExtensionNotFound(name)
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except ExtensionError:
            raise
        except Exception as exc:  # noqa: BLE001 — 导入期异常按上游包成 ExtensionFailed
            sys.modules.pop(module_name, None)
            raise ExtensionFailed(name, exc) from exc
        setup = getattr(module, "setup", None)
        if setup is None:
            sys.modules.pop(module_name, None)
            raise NoEntryPointError(name)
        try:
            await _maybe_await(setup(self))
        except ExtensionError:
            raise
        except Exception as exc:  # noqa: BLE001
            sys.modules.pop(module_name, None)
            raise ExtensionFailed(name, exc) from exc
        self.extensions[name] = module

    async def unload_extension(self, name: str) -> None:
        """卸载一个扩展（调用插件自己的 ``teardown(bot)``，与上游一致）。"""
        module = self.extensions.get(name)
        if module is None:
            raise ExtensionNotLoaded(name)
        teardown = getattr(module, "teardown", None)
        if teardown is not None:
            await _maybe_await(teardown(self))
        self.extensions.pop(name, None)

    async def reload_extension(self, name: str) -> None:
        """先卸载再加载（上游同义）。"""
        if name in self.extensions:
            await self.unload_extension(name)
        await self.load_extension(name)

    def __repr__(self) -> str:
        return f"<Bot user={self.user!r} commands={len(set(self._commands.values()))} cogs={len(self._cogs)}>"


class AutoShardedBot(Bot):
    """分片机器人。

    N.E.K.O. 里没有网关，也就没有分片：本类与 :class:`Bot` 行为完全一致（如实说明，
    而不是伪造 ``shard_count`` / ``shards`` 数据）。
    """

    @property
    def shard_count(self) -> int:
        return 1

    @property
    def shards(self) -> dict[int, None]:
        return {}


def _as_command_error(exc: BaseException) -> Any:
    """把任意异常转成命令错误链接受的结构（上游 ``CommandInvokeError`` 的位置）。

    能力缺失（``code == "unsupported_capability"``）原样保留：调用方要能看到
    「哪个 discord API 没有 N.E.K.O. 等价实现」，而不是被包成一条笼统的调用错误。
    """
    from ...errors import CommandError, CommandInvokeError

    if isinstance(exc, CommandError):
        return exc
    if getattr(exc, "code", "") == "unsupported_capability":
        return exc
    return CommandInvokeError(exc)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value
