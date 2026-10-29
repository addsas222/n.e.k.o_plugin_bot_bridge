"""``hoshino.log`` 的兼容实现：把日志转发到 ``HostServices.log``。

上游的 ``log.new_logger(name, debug)`` 返回一个标准库 ``logging.Logger``，插件与框架胶水
都用它（``hoshino.logger`` / ``sv.logger`` / ``hoshino.log.new_logger(...)``）。shim 返回
一个同形的最小对象：``debug`` / ``info`` / ``warning`` / ``error`` / ``exception`` /
``success`` / ``critical`` 都转到宿主日志，``opt`` / ``bind`` / ``addHandler`` 这类装饰性
调用是空操作（保留它们是为了让 Hoshino 自己的胶水代码不炸）。
"""

from __future__ import annotations

import logging
import traceback
from typing import Any

from aiocqhttp._runtime import emit_log

__all__ = [
    "Logger",
    "critical_handler",
    "default_handler",
    "error_handler",
    "formatter",
    "logger",
    "new_logger",
]

#: 供 ``nonebot.logger.addHandler(hoshino.log.error_handler)`` 这类代码使用。
formatter = logging.Formatter("[%(asctime)s %(name)s] %(levelname)s: %(message)s")
default_handler = logging.NullHandler()
"""shim 不写文件、不重复注册 stdout handler —— 日志已经进 N.E.K.O. 日志。"""
error_handler = logging.NullHandler()
critical_handler = logging.NullHandler()


class Logger:
    """loguru 风格 + logging 风格混合的最小 Logger。"""

    def __init__(self, name: str = "hoshino", *, debug: bool = True, source: str = "") -> None:
        self.name = name
        self.debug_enabled = bool(debug)
        self._source = source or name
        self._bound: dict[str, Any] = {}

    # ---- loguru 风格装饰性接口（空操作） ---------------------------------- #

    def opt(self, **kwargs: Any) -> "Logger":
        """loguru 的 ``logger.opt(...)``；shim 不做颜色/深度处理，原样返回自己。"""
        return self

    def bind(self, **kwargs: Any) -> "Logger":
        """loguru 的 ``logger.bind(...)``：把附加值并进 source，便于在日志里区分。"""
        bound = Logger(self.name, debug=self.debug_enabled, source=self._source)
        bound._bound = {**self._bound, **kwargs}
        return bound

    # ---- logging 风格装饰性接口（空操作） -------------------------------- #

    def addHandler(self, handler: Any) -> None:  # noqa: N802 — 上游是 stdlib 风格
        """shim 已经转发到宿主日志，不再接别的 handler。"""

    def removeHandler(self, handler: Any) -> None:  # noqa: N802
        """见上。"""

    def setLevel(self, level: Any) -> None:  # noqa: N802
        """``debug=False`` 时只放行 info 及以上（与上游 ``log.new_logger`` 一致）。"""
        self.debug_enabled = str(getattr(level, "name", level)).upper() == "DEBUG"

    def isEnabledFor(self, level: int) -> bool:  # noqa: N802
        return level > logging.DEBUG or self.debug_enabled

    # ---- 真正的转发 ------------------------------------------------------ #

    def _log(self, level: str, message: Any, *args: Any, **kwargs: Any) -> None:
        text = _format(message, args)
        if level == "debug" and not self.debug_enabled:
            return
        suffix = "".join(f" {key}={value}" for key, value in self._bound.items())
        emit_log(level, f"{text}{suffix}", source=self._source)

    def debug(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("debug", message, *args)

    def info(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("info", message, *args)

    def success(self, message: Any, *args: Any, **kwargs: Any) -> None:
        """loguru 的 ``success`` 级别（宿主日志里记成 info）。"""
        self._log("info", message, *args)

    def warning(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("warning", message, *args)

    warn = warning

    def error(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("error", message, *args)

    def critical(self, message: Any, *args: Any, **kwargs: Any) -> None:
        self._log("error", message, *args)

    def exception(self, message: Any, *args: Any, **kwargs: Any) -> None:
        """``logger.exception(e)``：把异常对象连同栈一起记成 error。"""
        detail = _format(message, args)
        stack = _format_exception(message)
        if stack:
            detail = f"{detail}\n{stack}"
        self._log("error", detail)

    def __repr__(self) -> str:
        return f"<hoshino.Logger {self.name!r}>"


#: ``hoshino.logger``（框架级日志；插件用它写日志会被转发到 ``HostServices.log``）。
logger = Logger("hoshino")


def new_logger(name: str, debug: bool = True) -> Logger:
    """造一个命名的 logger（上游签名：``new_logger(name, debug=True)``）。"""
    return Logger(name, debug=debug)


def _format(message: Any, args: tuple[Any, ...]) -> str:
    text = str(message)
    if args:
        try:
            text = text.format(*args)
        except (IndexError, KeyError, ValueError):
            text = " ".join([text, *(str(item) for item in args)])
    return text


def _format_exception(message: Any) -> str:
    """取出异常对象的栈（插件常写 ``logger.exception(e)``）。"""
    if isinstance(message, BaseException):
        return "".join(traceback.format_exception(type(message), message, message.__traceback__)).strip()
    if traceback.format_exc().strip() not in ("NoneType: None", ""):
        return traceback.format_exc().strip()
    return ""
