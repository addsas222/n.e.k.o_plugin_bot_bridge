"""通用工具（上游 ``nonebot.utils`` 的子集，纯标准库实现）。"""

from __future__ import annotations

import dataclasses
import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from functools import wraps
from pathlib import Path
from typing import Any, Generic, TypeVar

P = TypeVar("P")
R = TypeVar("R")
T = TypeVar("T")


def escape_tag(s: str) -> str:
    """转义日志富文本标签（上游用 rich 标记）。"""
    return str(s).replace("<", "&lt;").replace(">", "&gt;")


def unescape_tag(s: str) -> str:
    return str(s).replace("&lt;", "<").replace("&gt;", ">")


def lenient_issubclass(cls: Any, class_or_tuple: Any) -> bool:
    if isinstance(cls, type) or hasattr(cls, "__origin__"):
        try:
            return issubclass(cls, class_or_tuple)
        except TypeError:
            return False
    return False


def generic_check_issubclass(cls: Any, class_or_tuple: Any) -> bool:
    """检查（可能是泛型/联合/Annotated 的）注解是否可视为某个类的子类。"""
    if cls is inspect.Parameter.empty or cls is None:
        return False
    if cls is Any:
        return False
    if isinstance(cls, type):
        try:
            return issubclass(cls, class_or_tuple)
        except TypeError:
            return False
    args = getattr(cls, "__args__", ())
    if args:
        return any(generic_check_issubclass(arg, class_or_tuple) for arg in args)
    return False


def _unwrap_calls(call: Any) -> list[Any]:
    """列出用于判定「是不是协程/生成器」的内部函数对象。"""
    result: list[Any] = []
    if isinstance(call, (staticmethod, classmethod)):
        result.append(call.__func__)
        return result
    if inspect.isfunction(call) or inspect.ismethod(call):
        result.append(call)
        return result
    # 可调用实例 / 类：看 __call__
    if not inspect.isclass(call):
        result.append(call)
    inner = getattr(call, "__call__", None)
    if inspect.ismethod(inner):
        result.append(inner.__func__)
    elif inspect.isfunction(inner):
        result.append(inner)
    return result


def is_coroutine_callable(call: Any) -> bool:
    """函数 / 方法 / 可调用对象是否为协程函数（``functools.partial`` 也会展开）。"""
    return any(inspect.iscoroutinefunction(item) for item in _unwrap_calls(call))


def is_gen_callable(call: Any) -> bool:
    """函数 / 方法 / 可调用对象是否为生成器函数。"""
    return any(inspect.isgeneratorfunction(item) for item in _unwrap_calls(call))


def is_async_gen_callable(call: Any) -> bool:
    """函数 / 方法 / 可调用对象是否为异步生成器函数。"""
    return any(inspect.isasyncgenfunction(item) for item in _unwrap_calls(call))


def run_sync(call: Callable[P, R]) -> Callable[P, Awaitable[R]]:
    """把同步可调用对象包成协程函数。"""

    @wraps(call)
    async def _wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        return call(*args, **kwargs)

    return _wrapper


def get_name(obj: Any) -> str:
    if inspect.isfunction(obj) or inspect.isclass(obj):
        return obj.__name__
    if hasattr(obj, "__class__") and not isinstance(obj, (str, int, float, bool)):
        call = getattr(obj, "__call__", None)
        if call is not None and not inspect.ismethod(obj):
            return obj.__class__.__name__
    return repr(obj)


def path_to_module_name(path: Path) -> str:
    if path.is_dir():
        path = path / "__init__.py"
    if not path.name.endswith(".py"):
        raise ValueError(f"path must be a python file or directory, got {path!r}")
    if path.name == "__init__.py":
        return ".".join(path.parent.parts)
    return ".".join((*path.parent.parts, path.stem))


def resolve_dot_notation(obj_str: str, default_prefix: str = "", attribute: str = "") -> Any:
    parts = obj_str.split(".")
    if default_prefix:
        parts.insert(0, default_prefix)
    for index in range(len(parts), 0, -1):
        module_path = ".".join(parts[:index])
        try:
            module = __import__(module_path, fromlist=[""])
        except ImportError:
            continue
        obj = module
        for part in parts[index:]:
            obj = getattr(obj, part)
        if attribute and isinstance(obj, type):
            obj = getattr(obj, attribute)
        return obj
    raise ImportError(f"Cannot resolve '{obj_str}'")


class classproperty(Generic[T]):
    """类属性描述符。"""

    def __init__(self, method: Callable[[type[Any]], T]) -> None:
        self.method = method

    def __get__(self, instance: Any, owner: type[Any]) -> T:
        return self.method(owner)


class DataclassEncoder(json.JSONEncoder):
    """支持 dataclass / 任意对象回退到 ``repr`` 的 JSON 编码器。"""

    def default(self, o: Any) -> Any:
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            return dataclasses.asdict(o)
        if isinstance(o, Mapping):
            return dict(o)
        if hasattr(o, "__dict__"):
            return {key: value for key, value in vars(o).items() if not key.startswith("_")}
        return repr(o)


def flatten_exception_group(group: BaseException) -> list[BaseException]:
    """展开异常组（Python 3.11 原生 ``ExceptionGroup``）。"""
    if isinstance(group, BaseExceptionGroup):  # type: ignore[name-defined]
        result: list[BaseException] = []
        for exc in group.exceptions:
            result.extend(flatten_exception_group(exc))
        return result
    return [group]


def logger_wrapper(logger_name: str) -> Callable[..., None]:
    """生成一个 ``log(level, message, exception=None)`` 风格的函数（上游用于 adapter）。

    shim 下所有日志都转发到 :func:`nonebot._runtime.emit_log`。
    """
    from nonebot._runtime import emit_log

    def _log(level: str, message: str, exception: BaseException | None = None) -> None:
        text = message if exception is None else f"{message} ({exception!r})"
        normalized = level.lower()
        if normalized in {"warn", "warning"}:
            normalized = "warning"
        if normalized not in {"debug", "info", "warning", "error"}:
            normalized = "info"
        emit_log(normalized, f"[{logger_name}] {text}")

    return _log


def is_typed_callable(call: Any) -> bool:
    """判断 callable 是否带注解（上游用于签名解析）。"""
    return bool(getattr(call, "__annotations__", None))


__all__ = [
    "DataclassEncoder",
    "classproperty",
    "escape_tag",
    "flatten_exception_group",
    "generic_check_issubclass",
    "get_name",
    "is_async_gen_callable",
    "is_coroutine_callable",
    "is_gen_callable",
    "is_typed_callable",
    "lenient_issubclass",
    "logger_wrapper",
    "path_to_module_name",
    "resolve_dot_notation",
    "run_sync",
    "unescape_tag",
]
