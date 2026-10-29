"""位标志基类（``discord.flags`` 的最小子集）。

上游的 ``Intents`` / ``Permissions`` 都建在这个基类上。行为对齐上游：按位组合、
``__getattr__`` 读标志、``__setattr__`` 改标志、``__iter__`` 产出已开启的
``(名字, 值)``、``==`` 与整数比较、``|`` / ``&`` / ``~`` / ``^`` 运算。
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any, ClassVar

__all__ = ["BaseFlags"]


class BaseFlags:
    """位标志的通用实现。"""

    #: 标志名 -> 位（子类必须覆盖）。
    _VALUES: ClassVar[Mapping[str, int]] = {}
    #: 新建实例时的初始位。
    DEFAULT_VALUE: ClassVar[int] = 0

    def __init__(self, **kwargs: bool) -> None:
        object.__setattr__(self, "value", int(type(self).DEFAULT_VALUE))
        if kwargs:
            self._set_flags(**kwargs)

    # ---- 内部 ------------------------------------------------------------ #

    def _set_flags(self, **kwargs: bool) -> None:
        for key, enabled in kwargs.items():
            if key not in self._VALUES:
                raise TypeError(f"Invalid flag name {key!r} for {type(self).__name__}")
            if enabled is True:
                object.__setattr__(self, "value", self.value | self._VALUES[key])
            elif enabled is False:
                object.__setattr__(self, "value", self.value & ~self._VALUES[key])
            else:
                raise TypeError(f"Flag value for {key!r} must be True or False, not {enabled!r}")

    # ---- 读写 ------------------------------------------------------------ #

    def __getattr__(self, name: str) -> Any:
        values = type(self)._VALUES
        if name in values:
            return bool(self.value & values[name])
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "value":
            object.__setattr__(self, "value", int(value))
            return
        values = type(self)._VALUES
        if name in values:
            if value is True:
                object.__setattr__(self, "value", self.value | values[name])
            elif value is False:
                object.__setattr__(self, "value", self.value & ~values[name])
            else:
                raise TypeError(f"Flag value for {name!r} must be True or False, not {value!r}")
            return
        object.__setattr__(self, name, value)

    # ---- 工厂 ------------------------------------------------------------ #

    @classmethod
    def none(cls) -> "BaseFlags":
        """所有位都不开启。"""
        return cls()

    @classmethod
    def all(cls) -> "BaseFlags":
        """所有位都开启。"""
        instance = cls()
        for bit in cls._VALUES.values():
            object.__setattr__(instance, "value", instance.value | bit)
        return instance

    @classmethod
    def _from_value(cls, value: int) -> "BaseFlags":
        instance = cls()
        object.__setattr__(instance, "value", int(value))
        return instance

    # ---- 运算 ------------------------------------------------------------ #

    def __eq__(self, other: object) -> bool:
        if isinstance(other, BaseFlags):
            return self.value == other.value
        if isinstance(other, int):
            return self.value == other
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self) -> int:
        return hash(self.value)

    def __bool__(self) -> bool:
        return bool(self.value)

    def __or__(self, other: "BaseFlags") -> "BaseFlags":
        return type(self)._from_value(self.value | other.value)

    def __and__(self, other: "BaseFlags") -> "BaseFlags":
        return type(self)._from_value(self.value & other.value)

    def __xor__(self, other: "BaseFlags") -> "BaseFlags":
        return type(self)._from_value(self.value ^ other.value)

    def __invert__(self) -> "BaseFlags":
        return type(self)._from_value(~self.value)

    def __iter__(self) -> Iterator[tuple[str, bool]]:
        for name, bit in type(self)._VALUES.items():
            yield name, bool(self.value & bit)

    def __repr__(self) -> str:
        enabled = [name for name, bit in type(self)._VALUES.items() if self.value & bit]
        return f"<{type(self).__name__} {'|'.join(sorted(enabled)) or 'none'}>"
