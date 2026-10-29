"""``discord.Client``：客户端基类（含 Cog 注册表 / 事件分发 / 生命周期）。

与上游的差异（都是为了在 N.E.K.O. 里工作）：

* **没有网关循环**：``run`` / ``start`` / ``login`` / ``close`` 都没有等价物，显式抛
  ``UnsupportedCapabilityError``；插件由桥接层的 ``load`` / ``start`` / ``stop`` 驱动
  （``cog_load`` 在 ``start`` 里跑，``cog_unload`` 在 ``stop`` 里跑）。
* **``add_cog`` / ``remove_cog`` 同步注册、返回可等待对象**：上游 2.x 它们是协程，
  旧插件里有同步写法。两种写法都成立；``cog_load`` 只在 ``start`` 阶段跑一次。
* **``dispatch`` 直接 await**：上游用 ``create_task`` 调度监听器（fire-and-forget），
  N.E.K.O. 需要可观测、可收集的回复，所以同步等待监听器跑完。
* **Cog 能力上提到 Client**：上游只有 ``commands.Bot`` 能挂 Cog，本 shim 让
  ``discord.Client`` 也能挂（任务要求），这样只用 ``Client`` 的插件也能工作。
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterator
from typing import Any, TypeVar

from . import _runtime
from .errors import ClientException
from .intents import Intents
from .user import ClientUser

__all__ = ["Client", "MaybeAwait", "when_mentioned", "when_mentioned_or"]

T = TypeVar("T")


class MaybeAwait:
    """``add_cog`` / ``remove_cog`` 的返回值：同步部分已完成，``await`` 它才跑生命周期钩子。

    这样 ``bot.add_cog(cog)``（旧写法）与 ``await bot.add_cog(cog)``（上游 2.x 写法）都成立，
    而且不会被 Python 的「coroutine was never awaited」警告打扰。
    """

    __slots__ = ("_factory",)

    def __init__(self, factory: Callable[[], Awaitable[Any]]) -> None:
        self._factory = factory

    def __await__(self) -> Iterator[Any]:
        async def _run() -> Any:
            result = self._factory()
            return await result if inspect.isawaitable(result) else result

        return _run().__await__()


class Client:
    """一个「Discord 客户端」的替身：N.E.K.O. 侧的一条机器人通道。"""

    def __init__(self, *, intents: Intents | None = None, loop: Any = None, **options: Any) -> None:
        self.intents: Intents = intents if isinstance(intents, Intents) else Intents.default()
        self._loop = loop
        self.user: ClientUser | None = None
        self.guilds: list[Guild] = []
        self._closed = False
        self._ready = False
        self._ready_waiters: list[asyncio.Future[Any]] = []
        #: 事件名 -> 监听器（``bot.listen`` / ``Cog.listener`` 注册）
        self._listeners: dict[str, list[tuple[Any, Any]]] = {}
        #: 等待中的 ``wait_for``（事件名 -> future）
        self._waiters: dict[str, list[Any]] = {}
        #: Cog 注册表（qualname -> Cog）
        self._cogs: dict[str, Any] = {}
        #: 已知对象（由兼容层按消息 payload 登记；N.E.K.O. 里没有全局用户/频道目录）
        self._channels: dict[int, Any] = {}
        self._users: dict[int, Any] = {}
        #: 桥接层填充的上下文
        self._bridge_plugin_id = ""
        self._bridge_options = dict(options)
        self._bridge_errors: list[str] = []
        self._app_commands_tree: Any = None

    # ------------------------------------------------------------------ 基础

    @property
    def loop(self) -> Any:
        """插件运行的事件循环（桥接层在 ``start`` 时绑定到真实循环）。"""
        return self._loop or _runtime.active_loop()

    @property
    def latency(self) -> float:
        """N.E.K.O. 里没有网关心跳，如实返回 0.0（不编造延迟数字）。"""
        return 0.0

    @property
    def tree(self) -> Any:
        """``app_commands.CommandTree``：N.E.K.O. 没有斜杠命令注册面，用到才报错。"""
        if self._app_commands_tree is None:
            from .app_commands import CommandTree

            self._app_commands_tree = CommandTree(self)
        return self._app_commands_tree

    def is_ready(self) -> bool:
        return bool(self._ready)

    def is_closed(self) -> bool:
        return bool(self._closed)

    @staticmethod
    def _skip_check(user_id: Any, bot_id: Any) -> bool:
        """是否忽略这条消息（机器人自己发的）。"""
        return bot_id is not None and user_id is not None and int(user_id) == int(bot_id)

    async def wait_until_ready(self) -> None:
        """等客户端就绪（桥接层在 ``start`` 时置位；不会真的等待网关）。"""
        if self._ready:
            return
        future = asyncio.get_running_loop().create_future()
        self._ready_waiters.append(future)
        await future

    async def setup_hook(self) -> None:
        """登录前的异步初始化钩子（上游 ``Client.setup_hook``）。

        上游在 ``login()`` 里 await 它；N.E.K.O. 里没有登录，与之等价的位置是兼容层的
        ``start()`` —— 它会 await 插件覆盖过的 ``setup_hook``（基类这个空实现会被跳过）。
        """

    # ---- 桥接层接口（不属于上游 API） ----------------------------------- #

    def _bridge_mark_ready(self) -> None:
        self._ready = True
        waiters, self._ready_waiters = self._ready_waiters, []
        for future in waiters:
            if not future.done():
                future.set_result(True)

    def _bridge_reset_ready(self) -> None:
        self._ready = False

    def _bridge_register_channel(self, channel: Any) -> Any:
        self._channels[int(channel.id)] = channel
        channel._bridge_plugin_id = self._bridge_plugin_id
        if getattr(self, "_bridge_sender", None) is not None:
            channel._bridge_sender = self._bridge_sender
        return channel

    def _bridge_register_user(self, user: Any) -> Any:
        self._users[int(user.id)] = user
        user._bridge_plugin_id = self._bridge_plugin_id
        return user

    # ------------------------------------------------------------------ 事件

    def event(self, coro: T) -> T:
        """``@bot.event``：把协程挂成 ``on_<event>`` 方法（**会覆盖**同名默认实现）。

        这是上游行为：用 ``@bot.event`` 定义 ``on_message`` 会顶掉默认的
        ``process_commands``，插件得自己再调一次 ``await bot.process_commands(message)``。
        """
        name = getattr(coro, "__name__", "")
        if not name.startswith("on_"):
            raise ClientException("The event name must start with 'on_'.")
        if not inspect.iscoroutinefunction(coro):
            raise ClientException("The event function must be a coroutine.")
        setattr(self, name, coro)
        return coro

    def listen(self, name: str | None = None) -> Callable[[Any], Any]:
        """``@bot.listen('on_message')``：追加监听器（不覆盖默认实现）。"""
        if name is not None and not isinstance(name, str):
            raise TypeError("name must be a str")

        def decorator(func: Any) -> Any:
            self.add_listener(func, name)
            return func

        return decorator

    def add_listener(self, func: Any, name: str | None = None) -> None:
        """注册一个监听器（上游 ``Client.add_listener``）。"""
        event = name or getattr(func, "__name__", "")
        if not event.startswith("on_"):
            raise ClientException("The event name must start with 'on_'.")
        if not inspect.iscoroutinefunction(func):
            raise ClientException("The event function must be a coroutine.")
        bucket = self._listeners.setdefault(event[3:], [])
        tag = getattr(func, "__self__", None)
        if any(existing[0] is func and existing[1] is tag for existing in bucket):
            return
        bucket.append((func, tag))

    def remove_listener(self, func: Any, name: str | None = None) -> None:
        """移除一个监听器（上游 ``Client.remove_listener``）。"""
        event = name or getattr(func, "__name__", "")
        bucket = self._listeners.get(event[3:] if event.startswith("on_") else event, [])
        self._listeners[event[3:] if event.startswith("on_") else event] = [
            item for item in bucket if item[0] is not func
        ]

    def clear(self) -> None:
        """清掉内部监听器 / Cog（上游 ``Client.clear`` 的语义：重置内部状态）。"""
        self._listeners.clear()
        self._cogs.clear()
        self._waiters.clear()

    async def dispatch(self, event: str, *args: Any, **kwargs: Any) -> None:
        """触发一个事件（上游 ``Client.dispatch``）。

        与上游的差别：这里**直接 await** 监听器（上游用 ``create_task`` 调度），
        这样调用方在 ``dispatch`` 返回时就能看到副作用。监听器抛错会向上抛
        （插件自己 dispatch 的事件，保持上游语义）。
        """
        name = event[3:] if event.startswith("on_") else event
        method = getattr(self, f"on_{name}", None)
        if method is not None:
            await method(*args, **kwargs)
        await self._run_listeners(name, *args, **kwargs)
        self._resolve_waiters(name, *args, **kwargs)

    async def _bridge_dispatch(self, event: str, report: _runtime.RunReport | None, *args: Any) -> int:
        """桥接层的分发：逐个调用 ``on_<event>`` 与监听器，错误收进 report（不上抛）。

        返回真正被调用的处理者数量。``report`` 省略时用当前激活的收集器。
        """
        if report is None:
            report = _runtime.current_report()
        name = event[3:] if event.startswith("on_") else event
        count = 0
        method = getattr(self, f"on_{name}", None)
        if method is not None:
            count += 1
            await self._guard(self.on_error, name, report, method, *args)
        for func, tag in list(self._listeners.get(name, [])):
            count += 1
            _runtime.record_listener(name, getattr(func, "__qualname__", repr(func)))
            await self._guard(self.on_error, name, report, func, *args)
        self._resolve_waiters(name, *args)
        return count

    async def _run_listeners(self, name: str, *args: Any) -> None:
        for func, _tag in list(self._listeners.get(name, [])):
            await func(*args)

    async def _guard(
        self, on_error: Callable[..., Any], name: str, report: _runtime.RunReport | None, func: Any, *args: Any
    ) -> None:
        """执行一个处理者，异常按「记录 + 调 on_error」处理（不静默）。"""
        try:
            result = func(*args)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — 单个监听器失败不能丢掉整条消息
            detail = f"{name} 的处理者 {getattr(func, '__qualname__', func)!r} 抛错：{type(exc).__name__}: {exc}"
            if report is not None:
                report.errors.append(detail)
            _runtime.emit_log("error", detail, source="discord")
            try:
                await self.on_error(name, *args)
            except Exception as nested:  # noqa: BLE001 — on_error 自己坏了也只记录
                _runtime.emit_log("error", f"on_error 处理 {name} 时又抛错：{nested!r}", source="discord")

    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        """事件处理出错时的默认行为：写一条宿主日志（上游是打印 traceback）。"""
        detail = f"事件 {event_method} 的处理出错"
        self._bridge_errors.append(detail)
        _runtime.emit_log("error", detail, source="discord")

    def wait_for(
        self,
        event: str,
        *,
        timeout: float | None = None,
        check: Callable[..., bool] | None = None,
    ) -> Awaitable[Any]:
        """等一个事件（上游 ``Client.wait_for``）。

        真机里事件来自桥接层的消息分发，因此多数场景只能等到超时 —— 超时如实抛
        ``asyncio.TimeoutError``，不伪造事件。
        """
        name = event[3:] if event.startswith("on_") else event
        future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(name, []).append((future, check))

        async def _wait() -> Any:
            try:
                if timeout is None:
                    return await future
                return await asyncio.wait_for(future, timeout)
            finally:
                bucket = self._waiters.get(name, [])
                self._waiters[name] = [item for item in bucket if item[0] is not future]
                if not self._waiters[name]:
                    self._waiters.pop(name, None)

        return _wait()

    def _resolve_waiters(self, name: str, *args: Any) -> None:
        for future, check in list(self._waiters.get(name, [])):
            if future.done():
                continue
            try:
                match = True if check is None else bool(check(*args))
            except Exception:  # noqa: BLE001 — check 自己抛错视为不匹配（上游同义）
                match = False
            if match:
                future.set_result(args[0] if len(args) == 1 else args)

    # ------------------------------------------------------------------ Cog

    @property
    def cogs(self) -> dict[str, Any]:
        return dict(self._cogs)

    def get_cog(self, name: str) -> Any:
        """按 Cog 名（``Cog.qualified_name``）取 Cog。"""
        if isinstance(name, str):
            return self._cogs.get(name)
        getter = getattr(name, "qualified_name", None)
        return self._cogs.get(getter) if isinstance(getter, str) else None

    def add_cog(self, cog: Any, *, override: bool = False) -> MaybeAwait:
        """挂上一个 Cog（同步注册；``await`` 它只是「确认已登记」）。

        注册阶段就会把 Cog 里的命令 / 监听器接进注册表，因此 ``load`` 结束就能拿到
        完整的 ``CommandSpec`` / ``EventSpec``。

        **与上游的差异**：``cog_load`` 不由这里调度，而是留到兼容层的 ``start()``
        （在真正的事件循环上跑）。``load()`` 阶段的临时循环一旦关闭，在那里创建的后台
        任务会跟着死掉，所以 ``await bot.add_cog(cog)`` 只表示「已登记」。因此
        ``cog_load`` 里建立的后台循环是安全的，而 ``setup`` 里不要依赖 ``cog_load`` 已经跑完。
        """
        from .ext.commands.cog import Cog

        if not isinstance(cog, Cog):
            raise TypeError(f"cogs must derive from Cog, not {type(cog).__name__}")
        name = cog.qualified_name
        if name in self._cogs and not override:
            from .errors import CommandRegistrationError

            raise CommandRegistrationError(name)
        self._cogs[name] = cog
        cog.bot = self
        cog._bridge_plugin_id = self._bridge_plugin_id
        register = getattr(self, "_register_cog_surface", None)
        if callable(register):
            register(cog)
        return MaybeAwait(lambda: self._acknowledge_cog(cog))

    def _acknowledge_cog(self, cog: Any) -> None:
        """``await bot.add_cog(cog)`` 的返回值：标记「插件期望这个 Cog 被装载」。"""
        cog._bridge_load_requested = True

    def remove_cog(self, name: str) -> MaybeAwait | None:
        """摘掉一个 Cog（同步注销；``await`` 它可跑 ``cog_unload``）。"""
        cog = self._cogs.pop(name, None) if isinstance(name, str) else None
        if cog is None:
            return None
        cog.bot = None
        unregister = getattr(self, "_unregister_cog_surface", None)
        if callable(unregister):
            unregister(cog)
        return MaybeAwait(lambda: self._unload_cog(cog))

    async def _load_cog(self, cog: Any) -> None:
        """跑一次 ``cog_load``（同一个 Cog 实例只跑一次）。"""
        if getattr(cog, "_bridge_loaded", False):
            return
        cog._bridge_loaded = True
        await _maybe_await(cog.cog_load())

    async def _unload_cog(self, cog: Any) -> None:
        """跑一次 ``cog_unload``（没有 load 成功过就不跑，避免插件在半启动状态崩）。"""
        if not getattr(cog, "_bridge_loaded", False):
            return
        cog._bridge_loaded = False
        await _maybe_await(cog.cog_unload())

    def _bridge_cogs(self) -> list[Any]:
        """按注册顺序返回 Cog（``start`` / ``stop`` 用）。"""
        return list(self._cogs.values())

    # ------------------------------------------------------------------ 其它

    def get_channel(self, id: int) -> Any:  # noqa: A002 — 与上游签名一致
        channel = self._channels.get(int(id))
        if channel is not None:
            return channel
        for guild in self.guilds:
            found = guild.get_channel(id)
            if found is not None:
                return found
        return None

    def get_guild(self, id: int) -> Any:  # noqa: A002 — 与上游签名一致
        for guild in self.guilds:
            if guild.id == int(id):
                return guild
        return None

    def get_user(self, id: int) -> Any:  # noqa: A002 — 与上游签名一致
        """按 id 取已知用户（N.E.K.O. 没有全局用户目录，只认载荷里出现过的）。"""
        return self._users.get(int(id))

    @property
    def users(self) -> list[Any]:
        return list(self._users.values())

    def get_all_channels(self) -> Iterator[Any]:
        for channel in self._channels.values():
            yield channel

    def get_all_members(self) -> Iterator[Any]:
        for guild in self.guilds:
            yield from guild.members

    def get_emoji(self, id: int) -> Any:  # noqa: A002 — 与上游签名一致
        return None

    @property
    def emojis(self) -> list[Any]:
        return []

    @property
    def private_channels(self) -> list[Any]:
        return [channel for channel in self._channels.values() if not hasattr(channel, "guild")]

    def get_partial_message(self, message_id: int) -> Any:
        del message_id
        raise _runtime.unsupported("Client.get_partial_message", "N.E.K.O. 没有可按 id 取用的消息")

    # ---- 显式不支持 ------------------------------------------------------ #

    async def login(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Client.login", "N.E.K.O. 里没有 Discord 登录流程")

    async def start(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Client.start", "N.E.K.O. 里没有网关；插件由桥接层驱动")

    def run(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Client.run", "N.E.K.O. 里没有网关；插件由桥接层驱动")

    async def close(self) -> None:
        raise _runtime.unsupported("Client.close", "关闭由 N.E.K.O. 的 stop 生命周期负责")

    async def change_presence(self, *args: Any, **kwargs: Any) -> None:
        raise _runtime.unsupported("Client.change_presence", "N.E.K.O. 没有在线状态展示位")

    async def fetch_user(self, user_id: int) -> Any:
        user = self.get_user(user_id)
        if user is not None:
            return user
        raise _runtime.unsupported("Client.fetch_user", f"N.E.K.O. 不提供用户查询接口：user id={user_id}")

    async def fetch_channel(self, channel_id: int) -> Any:
        channel = self.get_channel(channel_id)
        if channel is not None:
            return channel
        raise _runtime.unsupported("Client.fetch_channel", f"N.E.K.O. 不提供频道查询接口：channel id={channel_id}")

    async def fetch_guild(self, guild_id: int) -> Any:
        guild = self.get_guild(guild_id)
        if guild is not None:
            return guild
        raise _runtime.unsupported("Client.fetch_guild", f"N.E.K.O. 不提供服务器查询接口：guild id={guild_id}")

    async def fetch_guilds(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.unsupported("Client.fetch_guilds", "N.E.K.O. 不提供服务器清单接口")

    async def application_info(self) -> Any:
        raise _runtime.unsupported("Client.application_info", "N.E.K.O. 不承载 Discord 应用信息")

    def __repr__(self) -> str:
        return f"<{type(self).__name__} user={self.user!r} guilds={len(self.guilds)} cogs={len(self._cogs)}>"


async def _maybe_await(value: Any) -> Any:
    """同时兼容同步 / 异步钩子（上游的 ``cog_load`` 是协程，但允许同步实现）。"""
    return await value if inspect.isawaitable(value) else value


# --------------------------------------------------------------------------- #
# 前缀工具（上游 discord.ext.commands 提供的两个便捷函数）
# --------------------------------------------------------------------------- #


def when_mentioned(bot: Any, message: Any) -> list[str]:
    """前缀为「@机器人 」：``commands.when_mentioned``。"""
    user = getattr(bot, "user", None)
    if user is None:
        return []
    return [f"{user.mention} "]


def when_mentioned_or(*prefixes: Any) -> Callable[[Any, Any], list[str]]:
    """前缀为「给的前缀 + @机器人 」：``commands.when_mentioned_or``。"""

    def inner(bot: Any, message: Any) -> list[str]:
        del message
        return list(prefixes) + when_mentioned(bot, message)

    return inner
