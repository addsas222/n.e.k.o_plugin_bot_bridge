"""``discord.ext.commands.cog``：``Cog`` 与它的元类。

与上游一致的部分：``name=`` 类关键字改名、``@Cog.listener()`` 声明监听器、
``cog_load`` / ``cog_unload`` / ``cog_check`` / ``cog_before_invoke`` /
``cog_after_invoke`` / ``cog_command_error`` 钩子、同一个 Cog 类只会有一个实例。

与上游的差异（都在 ``bridge/backends/discordpy.py`` 的 docstring 里记过）：

* ``cog_load`` 由兼容层的 ``start()`` 驱动（不是 ``add_cog`` 的副作用），
  这样后台任务才会绑在真正运行的事件循环上；
* 基类实现了 ``cog_command_error``（空实现），方便插件 ``super()`` 调用；
  命令错误链会跳过这个空实现（否则插件的错误处理会被静默吞掉）。
"""

from __future__ import annotations

import weakref
from typing import Any

__all__ = ["Cog", "CogMeta"]

#: 同一个 Cog 类只保留一个实例（上游同义）；用弱引用键，重新加载后旧类会被回收。
_COG_INSTANCES: "weakref.WeakKeyDictionary[type, Any]" = weakref.WeakKeyDictionary()


class CogMeta(type):
    """收集类体里的 ``Command`` / ``@Cog.listener`` 并登记 Cog 名。"""

    def __new__(mcs, clsname: str, bases: Any, attrs: Any, *, name: str | None = None, **kwargs: Any) -> Any:
        new_cls = super().__new__(mcs, clsname, bases, dict(attrs))
        new_cls.__cog_name__ = str(name or attrs.get("__cog_name__") or clsname)
        commands: dict[str, Any] = {}
        listeners: dict[str, Any] = {}
        for base in reversed(new_cls.__mro__):
            for key, value in base.__dict__.items():
                commands.pop(key, None)
                listeners.pop(key, None)
                if _is_command(value):
                    commands[key] = value
                elif callable(value) and getattr(value, "__cog_listeners__", None):
                    listeners[key] = value
        new_cls.__cog_commands__ = list(commands.values())
        new_cls.__cog_listeners__ = list(listeners.values())
        return new_cls

    def __init__(cls, clsname: str, bases: Any, attrs: Any, **kwargs: Any) -> None:
        super().__init__(clsname, bases, attrs)

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        """Cog 是单例（上游同义）：同一个类重复实例化返回同一个对象。"""
        instance = _COG_INSTANCES.get(cls)
        if instance is not None:
            return instance
        instance = super().__call__(*args, **kwargs)
        _COG_INSTANCES[cls] = instance
        return instance


def _is_command(value: Any) -> bool:
    from .core import Command

    return isinstance(value, Command)


class Cog(metaclass=CogMeta):
    """一个 Cog（功能模块）。"""

    #: Cog 名（``class X(Cog, name="y")`` 可以覆盖）。
    __cog_name__ = ""
    __cog_commands__: list[Any] = []
    __cog_listeners__: list[Any] = []

    def __init__(self, *args: Any, bot: Any = None, **kwargs: Any) -> None:
        # 上游允许 ``super().__init__(bot)``；多余的参数忽略（有些插件会多传）。
        if args and bot is None:
            bot = args[0]
        self.bot = bot
        self._bridge_plugin_id = getattr(bot, "_bridge_plugin_id", "")
        self._bridge_loaded = False
        del kwargs

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # 上游用 ``__cog_special_methods__`` 做校验；本 shim 只需要「基类空实现」标记
        # （见文件末尾），因此这里只把类关键字吃掉。
        super().__init_subclass__()

    # ---- 视图 ------------------------------------------------------------ #

    @property
    def qualified_name(self) -> str:
        """Cog 名（默认类名，``name=`` 可覆盖）。"""
        return type(self).__cog_name__ or type(self).__name__

    def get_commands(self) -> list[Any]:
        """本 Cog 里声明的命令（含组；组的子命令在 ``Group.commands`` 里）。"""
        return list(type(self).__cog_commands__)

    def get_listeners(self) -> list[tuple[str, Any]]:
        """本 Cog 声明的监听器（``[(事件名, 已绑定的方法), ...]``，事件名带 ``on_``，与上游一致）。"""
        result: list[tuple[str, Any]] = []
        for func in type(self).__cog_listeners__:
            for event, target in getattr(func, "__cog_listeners__", ()):
                bound = getattr(self, getattr(target, "__name__", ""), target)
                result.append((event, bound))
        return result

    @classmethod
    def listener(cls, name: str | None = None) -> Any:
        """``@Cog.listener()`` / ``@Cog.listener('on_message')``（也支持不带括号）。"""
        if callable(name):  # 不带括号：@Cog.listener
            return cls.listener()(name)

        def decorator(func: Any) -> Any:
            event = name or getattr(func, "__name__", "")
            if not event.startswith("on_"):
                raise TypeError("The event name must start with 'on_'.")
            entries = list(getattr(func, "__cog_listeners__", ()) or ())
            entries.append((event, func))
            func.__cog_listeners__ = entries
            return func

        return decorator

    # ---- 生命周期（由兼容层驱动） ---------------------------------------- #

    async def cog_load(self) -> None:
        """Cog 装载完成（兼容层的 ``start()`` 里 await 一次）。"""

    async def cog_unload(self) -> None:
        """Cog 卸载（兼容层的 ``stop()`` 里 await 一次）。"""

    async def cog_check(self, ctx: Any) -> bool:
        """本 Cog 所有命令共用的前置检查（返回 False → ``CheckFailure``）。"""
        return True

    async def cog_before_invoke(self, ctx: Any) -> None:
        """每条命令执行前的钩子。"""

    async def cog_after_invoke(self, ctx: Any) -> None:
        """每条命令执行后的钩子（无论成功失败都会跑）。"""

    async def cog_command_error(self, ctx: Any, error: BaseException) -> None:
        """本 Cog 的命令错误处理器（空实现；命令错误链会跳过基类实现）。"""
        del ctx, error


#: 基类的 ``cog_command_error`` 是空实现：命令错误链要能识别它并跳过。
Cog.cog_command_error._bridge_base_impl = True  # type: ignore[attr-defined]
