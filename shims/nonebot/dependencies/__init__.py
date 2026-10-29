"""依赖注入的定义与处理（上游 ``nonebot.dependencies`` 的子集，纯标准库）。

与上游的差异（均为实现手段，语义一致）：

* 上游用 anyio 任务组并发求解所有参数，shim 顺序求解（顺序确定，便于测试）；
* 上游用 ``AsyncExitStack`` 管理生成器依赖的生命周期，shim 在求解时一次性驱干；
* 上游用 ``exceptiongroup.catch`` 处理 ``SkippedException``，shim 用原生 try/except。
"""

from __future__ import annotations

import abc
import inspect
import types
import typing
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar, cast

from nonebot.compat import ModelField, PydanticUndefined
from nonebot.exception import SkippedException, TypeMisMatch
from nonebot.utils import is_async_gen_callable, is_coroutine_callable, is_gen_callable, run_sync

R = TypeVar("R")


def get_typed_signature(call: Callable[..., Any]) -> inspect.Signature:
    """获取可调用对象签名（解析字符串注解 / ``from __future__ import annotations``）。"""
    signature = inspect.signature(call)
    target = _unwrap_callable(call)
    globalns: dict[str, Any] = getattr(target, "__globals__", {}) or {}
    hints: dict[str, Any] = {}
    if target is not None:
        try:
            # include_extras=True 是关键：``T_State`` 是 ``Annotated[dict, _STATE_FLAG]``，
            # 默认的 get_type_hints 会剥掉这层元数据，依赖注入就认不出状态参数了。
            hints = typing.get_type_hints(target, include_extras=True)
        except Exception:  # noqa: BLE001 — 存在无法解析的前向引用时退化为逐参数解析
            hints = {}
    parameters = []
    for name, param in signature.parameters.items():
        annotation = param.annotation
        if name in hints:
            annotation = hints[name]
        elif isinstance(annotation, str):
            try:
                annotation = eval(annotation, globalns)  # noqa: S307 — 与上游 ForwardRef 解析等价
            except Exception:  # noqa: BLE001 — 解析失败时与上游一样视为无注解
                annotation = inspect.Parameter.empty
        parameters.append(param.replace(annotation=annotation))
    return signature.replace(parameters=parameters)


def _unwrap_callable(call: Callable[..., Any]) -> Any:
    """取出真正承载注解的函数对象（支持可调用类实例 / ``staticmethod``）。"""
    if inspect.isfunction(call) or inspect.isclass(call):
        return call
    if isinstance(call, (staticmethod, classmethod)):
        return call.__func__
    method = getattr(call, "__call__", None)
    if inspect.ismethod(method):
        return method.__func__
    if inspect.isfunction(method):
        return method
    return None


def check_field_type(field: ModelField, value: Any) -> Any:
    """检查字段类型是否匹配，不匹配时抛 :class:`TypeMisMatch`（上游语义）。"""
    annotation = field.annotation
    if annotation is inspect.Parameter.empty or annotation is Any:
        return value
    if not _matches(annotation, value):
        raise TypeMisMatch(field, value)
    return value


def _matches(annotation: Any, value: Any) -> bool:
    """轻量类型判定（不做 pydantic 强制转换）。"""
    if value is None:
        return _allows_none(annotation)
    origin = typing.get_origin(annotation)
    if origin is typing.Annotated:
        args = typing.get_args(annotation)
        return _matches(args[0], value) if args else True
    if origin is not None:
        args = typing.get_args(annotation)
        if origin in (typing.Union, types.UnionType):
            return any(_matches(arg, value) for arg in args)
        if origin in (list, set, frozenset, tuple, dict):
            return isinstance(value, origin)
        import collections.abc as cabc

        if origin in (cabc.Sequence, cabc.Iterable, cabc.Mapping, cabc.Callable):
            try:
                return isinstance(value, origin)
            except TypeError:
                return True
        return True
    if annotation is float and isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(annotation, type):
        return isinstance(value, annotation)
    return True


def _allows_none(annotation: Any) -> bool:
    if annotation is None or annotation is Any or annotation is type(None):
        return True
    if typing.get_origin(annotation) is typing.Union or isinstance(annotation, types.UnionType):
        return any(arg is type(None) for arg in typing.get_args(annotation))
    return False


class Param(abc.ABC):
    """依赖注入的基本单元 —— 参数。"""

    def __init__(self, *args: Any, validate: bool = False, default: Any = PydanticUndefined, **kwargs: Any) -> None:
        self.validate = validate
        self.default = default
        self.extra = dict(kwargs)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}()"

    @classmethod
    def _check_param(cls, param: inspect.Parameter, allow_types: tuple[type["Param"], ...]) -> "Param | None":
        return None

    @classmethod
    def _check_parameterless(cls, value: Any, allow_types: tuple[type["Param"], ...]) -> "Param | None":
        return None

    @abc.abstractmethod
    async def _solve(self, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def _check(self, **kwargs: Any) -> None:
        return None


@dataclass
class Dependent(Generic[R]):
    """依赖注入容器。

    参数:
        call: 依赖注入的可调用对象
        params: 具名参数列表
        parameterless: 匿名参数列表
    """

    call: Callable[..., Any]
    params: tuple[ModelField, ...] = field(default_factory=tuple)
    parameterless: tuple[Param, ...] = field(default_factory=tuple)

    def __repr__(self) -> str:
        if inspect.isfunction(self.call) or inspect.isclass(self.call):
            call_str = getattr(self.call, "__name__", repr(self.call))
        else:
            call_str = repr(self.call)
        return f"Dependent(call={call_str}" + (f", parameterless={self.parameterless}" if self.parameterless else "") + ")"

    async def __call__(self, **kwargs: Any) -> R:
        try:
            await self.check(**kwargs)
            values = await self.solve(**kwargs)
            return cast(R, await self._run(self.call, values))
        except SkippedException:
            raise

    @staticmethod
    async def _run(call: Callable[..., Any], values: dict[str, Any]) -> Any:
        """调用 handler（支持同步 / 协程 / 生成器 / 异步生成器）。"""
        if is_coroutine_callable(call):
            return await call(**values)
        if is_async_gen_callable(call):
            result: Any = None
            async for item in call(**values):
                result = item
            return result
        if is_gen_callable(call):
            result = None
            for item in call(**values):
                result = item
            return result
        return await run_sync(call)(**values)

    @staticmethod
    def parse_params(call: Callable[..., Any], allow_types: tuple[type[Param], ...]) -> tuple[ModelField, ...]:
        fields: list[ModelField] = []
        for param in get_typed_signature(call).parameters.values():
            field_info: Param | None = None
            if isinstance(param.default, Param):
                field_info = param.default
            else:
                for allow_type in allow_types:
                    candidate = allow_type._check_param(param, allow_types)
                    if candidate is not None:
                        field_info = candidate
                        break
                else:
                    raise ValueError(
                        f"Unknown parameter {param.name} for function {call} with type {param.annotation}"
                    )
            annotation: Any = Any
            if param.annotation is not param.empty:
                annotation = param.annotation
            fields.append(ModelField.construct(name=param.name, annotation=annotation, field_info=field_info))
        return tuple(fields)

    @staticmethod
    def parse_parameterless(parameterless: tuple[Any, ...], allow_types: tuple[type[Param], ...]) -> tuple[Param, ...]:
        result: list[Param] = []
        for value in parameterless:
            for allow_type in allow_types:
                param = allow_type._check_parameterless(value, allow_types)
                if param is not None:
                    break
            else:
                raise ValueError(f"Unknown parameterless {value}")
            result.append(param)
        return tuple(result)

    @classmethod
    def parse(
        cls,
        *,
        call: Callable[..., Any],
        parameterless: Iterable[Any] | None = None,
        allow_types: Iterable[type[Param]],
    ) -> "Dependent[Any]":
        allow = tuple(allow_types)
        params = cls.parse_params(call, allow)
        parameterless_params = () if parameterless is None else cls.parse_parameterless(tuple(parameterless), allow)
        return cls(call, params, parameterless_params)

    async def check(self, **params: Any) -> None:
        for param in self.parameterless:
            await param._check(**params)
        for field in self.params:
            field_info = field.field_info
            if isinstance(field_info, Param):
                await field_info._check(**params)

    async def solve(self, **params: Any) -> dict[str, Any]:
        for param in self.parameterless:
            await param._solve(**params)
        result: dict[str, Any] = {}
        for field in self.params:
            field_info = field.field_info
            if not isinstance(field_info, Param):
                continue
            value = await field_info._solve(**params)
            if value is PydanticUndefined:
                value = field.get_default()
            result[field.name] = check_field_type(field, value)
        return result


__all__ = [
    "Dependent",
    "Param",
    "check_field_type",
    "get_typed_signature",
]
