"""插件面向的日志接口（N.E.K.O. shim）。

``from astrbot.api import logger`` 拿到的就是本模块：``logger.info("...")`` 会按调用
者模块路由到对应插件的宿主日志（与上游 ``_PluginContextLogger`` 的行为一致），
找不到归属时退回 ``logging.getLogger("astrbot")``。

``from astrbot.api.logger import logger`` 也能用（``logger`` 指回本模块自身）。
"""

from __future__ import annotations

import logging
import sys
import traceback
from typing import Any

from .._runtime import RUNTIME

_FALLBACK = logging.getLogger("astrbot")
_SHIM_PACKAGE = "astrbot"
_NUMERIC = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def _level_name(level: int) -> str:
    """把 ``logging`` 数值级别归一成 shim 使用的级别名。"""
    if level >= logging.CRITICAL:
        return "critical"
    if level >= logging.ERROR:
        return "error"
    if level >= logging.WARNING:
        return "warning"
    if level >= logging.INFO:
        return "info"
    return "debug"


def _resolve_plugin_id(module_name: str) -> str:
    """按模块名找到它所属的原生插件 id（找不到返回空串）。"""
    from ..core.star.star import star_map

    for module_path, metadata in star_map.items():
        if not module_path or not metadata.name:
            continue
        package = module_path.rpartition(".")[0]
        if module_name == module_path or module_name.startswith(package + "."):
            return metadata.plugin_id
    return ""


def _format(message: Any, args: tuple[Any, ...]) -> str:
    if not args:
        return str(message)
    try:
        return str(message) % args
    except (TypeError, ValueError):
        return " ".join([str(message), *(str(arg) for arg in args)])


def _dispatch(level: str, message: Any, args: tuple[Any, ...], *, exc: bool = False) -> None:
    """写一条日志：定位调用者模块 → 找到插件 → 交给宿主。"""
    frame = sys._getframe(1).f_back
    module_name = frame.f_globals.get("__name__", "") if frame is not None else ""
    text = _format(message, args)
    if exc:
        text = f"{text}\n{traceback.format_exc()}"
    if module_name.startswith(_SHIM_PACKAGE):
        # shim 内部调用：直接进全局 logger，避免被误判成插件日志
        _FALLBACK.log(_NUMERIC.get(level, logging.INFO), text)
        return
    native_id = _resolve_plugin_id(module_name)
    bridge_id = RUNTIME.bridge_plugin_id(native_id) if native_id else ""
    RUNTIME.log(
        level,
        text,
        plugin_id=bridge_id,
        source=f"astrbot.plugin.{native_id or module_name}",
    )


def debug(message: Any, *args: Any, **kwargs: Any) -> None:
    """DEBUG 级日志。"""
    del kwargs
    _dispatch("debug", message, args)


def info(message: Any, *args: Any, **kwargs: Any) -> None:
    """INFO 级日志。"""
    del kwargs
    _dispatch("info", message, args)


def warning(message: Any, *args: Any, **kwargs: Any) -> None:
    """WARNING 级日志。"""
    del kwargs
    _dispatch("warning", message, args)


def error(message: Any, *args: Any, **kwargs: Any) -> None:
    """ERROR 级日志。"""
    del kwargs
    _dispatch("error", message, args)


def exception(message: Any, *args: Any, **kwargs: Any) -> None:
    """ERROR 级日志，附带当前异常栈。"""
    del kwargs
    _dispatch("error", message, args, exc=True)


def critical(message: Any, *args: Any, **kwargs: Any) -> None:
    """CRITICAL 级日志。"""
    del kwargs
    _dispatch("critical", message, args)


def log(level: int, message: Any, *args: Any, **kwargs: Any) -> None:
    """按 ``logging`` 数值级别写日志。"""
    del kwargs
    _dispatch(_level_name(level), message, args)


logger = sys.modules[__name__]
"""``from astrbot.api.logger import logger`` 的兼容入口。"""

__all__ = [
    "critical",
    "debug",
    "error",
    "exception",
    "info",
    "log",
    "logger",
    "warning",
]
