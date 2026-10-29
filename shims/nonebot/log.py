"""日志记录器（上游 ``nonebot.log`` 的子集）。

偏差：上游 ``logger`` 是 `loguru.logger`；shim 用纯标准库实现了同名接口的常用子集
（``trace/debug/info/success/warning/error/critical/exception`` + ``opt`` / ``bind`` / ``log`` / ``level``），
并把每一条日志转发给 N.E.K.O.（``HostServices.log``）；未绑定宿主时退回标准库 logging。
"""

from __future__ import annotations

import sys
import traceback
from typing import Any

from nonebot._runtime import emit_log

_LEVEL_NAMES = {
    "trace": "debug",
    "debug": "debug",
    "info": "info",
    "success": "info",
    "warning": "warning",
    "error": "error",
    "critical": "error",
}


class _Level:
    """loguru level 对象的最小替身（只暴露 ``name`` / ``no``）。"""

    def __init__(self, name: str, no: int) -> None:
        self.name = name
        self.no = no

    def __repr__(self) -> str:
        return f"Level(name={self.name!r}, no={self.no})"


class _Logger:
    """loguru 风格 logger 的最小替身。"""

    def __init__(self, *, extra: dict[str, Any] | None = None, exception: BaseException | None = None) -> None:
        self._extra: dict[str, Any] = dict(extra or {})
        self._exception = exception

    # ---- loguru 常用配置接口 -------------------------------------------- #
    def remove(self) -> None:
        """上游用于移除 handler；shim 的日志出口由宿主接管。"""

    def add(self, *args: Any, **kwargs: Any) -> int:
        """上游用于添加 handler；shim 忽略并返回固定 id。"""
        return 0

    def level(self, name: str, **kwargs: Any) -> _Level:
        normalized = name.lower()
        number = {"trace": 5, "debug": 10, "info": 20, "success": 22, "warning": 30, "error": 40, "critical": 50}
        return _Level(name, number.get(normalized, 20))

    def opt(self, **kwargs: Any) -> "_Logger":
        """loguru 的 ``opt(...)``；支持 ``exception``（其余参数忽略）。"""
        return _Logger(extra=self._extra, exception=kwargs.get("exception"))

    def bind(self, **kwargs: Any) -> "_Logger":
        return _Logger(extra={**self._extra, **kwargs}, exception=self._exception)

    def log(self, level: str, message: object, *args: object, **kwargs: object) -> None:
        self._emit(str(level).lower(), message, *args, **kwargs)

    # ---- 具体级别 -------------------------------------------------------- #
    def trace(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit("trace", message, *args, **kwargs)

    def debug(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit("debug", message, *args, **kwargs)

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit("info", message, *args, **kwargs)

    def success(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit("success", message, *args, **kwargs)

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit("warning", message, *args, **kwargs)

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit("error", message, *args, **kwargs)

    def critical(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit("critical", message, *args, **kwargs)

    def exception(self, message: object, *args: object, **kwargs: object) -> None:
        self._emit("error", message, *args, exc_info=sys.exc_info(), **kwargs)

    # ---- 内部 ------------------------------------------------------------ #
    def _emit(
        self,
        level: str,
        message: object,
        *args: object,
        exc_info: tuple[Any, Any, Any] | None = None,
        **kwargs: object,
    ) -> None:
        text = str(message)
        if args:
            try:
                text = text.format(*args)
            except (IndexError, KeyError, ValueError):
                text = f"{text} {' '.join(str(arg) for arg in args)}"
        if self._exception is not None:
            exc_info = (type(self._exception), self._exception, self._exception.__traceback__)
        if exc_info is not None and exc_info[0] is not None:
            text = f"{text}\n{''.join(traceback.format_exception(*exc_info))}"
        if self._extra:
            prefix = " ".join(f"{key}={value}" for key, value in self._extra.items())
            text = f"[{prefix}] {text}"
        emit_log(_LEVEL_NAMES.get(level, "info"), text)


logger = _Logger()
"""NoneBot 日志记录器对象。"""


def default_filter(record: Any) -> bool:
    """上游的日志过滤器；shim 的等级由宿主决定，恒为 True。"""
    return True


def default_format(record: Any) -> str:
    """上游的默认日志格式（shim 直接返回 record 的字符串形式）。"""
    return str(record)


__all__ = ["default_filter", "default_format", "logger"]
