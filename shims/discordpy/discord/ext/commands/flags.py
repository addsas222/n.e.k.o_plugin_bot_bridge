"""``discord.ext.commands.flags`` —— ``FlagConverter`` 参数组。

上游允许把一组长参数塞进一个 converter：

    class BanFlags(commands.FlagConverter, prefix="--", delimiter=" "):
        member: discord.Member
        reason: str = "未说明"

    @commands.command()
    async def ban(self, ctx, *, flags: BanFlags): ...

这里实现同名同用法的**简版**：``--key value`` 逐对解析，按注解/默认值填进实例，
不做 Discord 对象转换（``discord.Member`` 之类在桥接层没有解析来源，会原样保留字符串）。
"""

from __future__ import annotations

import inspect
import shlex
from typing import Any, get_origin, get_args

__all__ = ["Flag", "FlagConverter", "FlagError", "flag"]


class FlagError(Exception):
    """参数组解析失败（上游同名）。"""


class Flag:
    """字段级配置（对应上游 ``Flag``）：别名、默认值。"""

    def __init__(self, *, name: str | None = None, aliases: list[str] | None = None,
                 default: Any = inspect.Parameter.empty, max_args: int | None = None) -> None:
        self.name = name
        self.aliases = list(aliases or [])
        self.default = default
        self.max_args = max_args


def flag(*, name: str | None = None, aliases: list[str] | None = None,
         default: Any = inspect.Parameter.empty, max_args: int | None = None) -> Any:
    """给字段附加别名/默认值（上游同名）。"""
    return Flag(name=name, aliases=aliases, default=default, max_args=max_args)


class FlagConverter:
    """把 ``--key value`` 序列解析成实例（对应上游 ``FlagConverter``）。"""

    #: 类声明里可覆盖（``class F(FlagConverter, prefix="--")``）
    __commands_flags__: dict[str, Flag] = {}

    def __init_subclass__(cls, *, prefix: str = "--", delimiter: str = " ", **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.__flag_prefix__ = prefix
        cls.__flag_delimiter__ = delimiter
        flags: dict[str, Flag] = {}
        for base in reversed(cls.__mro__):
            for name, value in vars(base).items():
                if name.startswith("_") or name in {"__annotations__"}:
                    continue
                annotation = (getattr(base, "__annotations__", {}) or {}).get(name)
                if isinstance(value, Flag):
                    flags[name] = value
                elif annotation is not None:
                    flags[name] = Flag(default=value if value is not inspect.Parameter.empty else inspect.Parameter.empty)
        cls.__commands_flags__ = flags

    # -- 解析 -------------------------------------------------------------- #
    @classmethod
    def get_flags(cls) -> dict[str, Flag]:
        return dict(cls.__commands_flags__ or {})

    @classmethod
    def _lookup(cls, token: str) -> str | None:
        flags = cls.get_flags()
        for name, spec in flags.items():
            candidates = {name, *(spec.aliases or [])}
            if spec.name:
                candidates.add(spec.name)
            if token in candidates:
                return name
        return None

    @classmethod
    def _coerce(cls, name: str, value: str) -> Any:
        annotation = (getattr(cls, "__annotations__", {}) or {}).get(name)
        if annotation is None:
            return value
        target = get_origin(annotation) or annotation
        if target in (int, float, bool, str):
            try:
                if target is bool:
                    return value.lower() in {"1", "true", "yes", "on"}
                return target(value)
            except (TypeError, ValueError) as exc:
                raise FlagError(f"{name} 需要 {getattr(target, '__name__', target)}，收到 {value!r}") from exc
        args = get_args(annotation)
        if target is list and args and args[0] is int:
            try:
                return [int(item) for item in shlex.split(value)]
            except ValueError as exc:
                raise FlagError(f"{name} 需要整数列表，收到 {value!r}") from exc
        return value

    @classmethod
    def convert(cls, argument: str = "", /) -> "FlagConverter":
        """把 ``"--reason 刷屏 --days 3"`` 解析成实例。"""
        try:
            tokens = shlex.split(argument)
        except ValueError as exc:
            raise FlagError(f"参数无法解析：{exc}") from exc

        parsed: dict[str, Any] = {}
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if not token.startswith(cls.__flag_prefix__):
                raise FlagError(f"意外的参数 {token!r}（应以 {cls.__flag_prefix__} 开头）")
            name = cls._lookup(token.lstrip("-"))
            if name is None:
                raise FlagError(f"未知参数 {token!r}")
            index += 1
            if index >= len(tokens) or tokens[index].startswith(cls.__flag_prefix__):
                parsed[name] = True
                continue
            parsed[name] = cls._coerce(name, tokens[index])
            index += 1

        instance = cls.__new__(cls)
        for name, spec in cls.get_flags().items():
            if name in parsed:
                setattr(instance, name, parsed[name])
            elif spec.default is not inspect.Parameter.empty:
                setattr(instance, name, spec.default)
            else:
                raise FlagError(f"缺少必填参数 {cls.__flag_prefix__}{name}")
        return instance
