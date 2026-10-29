"""NoneBot2 兼容 shim 的桥接运行时。

本模块**不属于上游 NoneBot 的公开 API**，它是 ``bot_bridge`` 的兼容层
(``bridge/backends/nonebot.py``) 与 shim 之间唯一的粘合点：

* 插件导入上下文（导入期间创建的 Matcher / Hook / 插件元信息归属哪个插件）；
* 回复收集（``Matcher.send`` / ``Bot.send`` 把文本写进当前的 ``CommandResult.replies``，
  而不是直接推进 N.E.K.O.）；
* 日志转发（``nonebot.log.logger`` → ``HostServices.log``）；
* 依赖插件解析（``nonebot.require``）；
* 驱动（``nonebot.get_driver``）与全局配置快照。

设计约束：所有模块级状态都必须幂等可重入（同一个插件重复 load / 多个插件并存 /
多个 N.E.K.O. 插件实例共存时都不能破坏状态）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

__all__ = [
    "GlobalHooks",
    "PluginRecord",
    "RunReport",
    "begin_run",
    "bind_runtime",
    "collector",
    "capture_replies",
    "current_plugin",
    "describe_matcher",
    "emit_log",
    "emit_reply",
    "plugin_matchers",
    "register_matcher",
    "set_session_bot",
]

_LOGGER = logging.getLogger("bot_bridge.nonebot.shim")

#: 钩子种类：与 ``Driver.on_startup`` / ``on_shutdown`` / ``on_bot_connect`` 对应。
HOOK_KINDS: tuple[str, ...] = ("startup", "shutdown", "bot_connect", "bot_disconnect")

_LOCK = threading.RLock()

# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class GlobalHooks:
    """不属于任何插件的驱动级钩子（``get_driver().on_startup`` 在导入上下文之外调用）。"""

    startup: list[Callable[..., Any]] = field(default_factory=list)
    shutdown: list[Callable[..., Any]] = field(default_factory=list)
    bot_connect: list[Callable[..., Any]] = field(default_factory=list)
    bot_disconnect: list[Callable[..., Any]] = field(default_factory=list)

    def of(self, kind: str) -> list[Callable[..., Any]]:
        return getattr(self, kind)  # type: ignore[no-any-return]


@dataclass
class PluginRecord:
    """一个外来 NoneBot 插件的运行时记录。"""

    plugin_id: str
    module_name: str = ""
    path: str = ""
    module: ModuleType | None = None
    metadata: object | None = None
    matchers: list[type[Any]] = field(default_factory=list)
    hooks: GlobalHooks = field(default_factory=GlobalHooks)
    preprocessors: dict[str, list[Any]] = field(default_factory=dict)
    config_snapshot: dict[str, Any] = field(default_factory=dict)

    def hook_count(self) -> dict[str, int]:
        return {kind: len(self.hooks.of(kind)) for kind in HOOK_KINDS}


@dataclass
class RunReport:
    """一次事件分发的结果（桥接层据此填 ``CommandResult``）。"""

    plugin_id: str = ""
    replies: list[str] = field(default_factory=list)
    matchers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def handled(self) -> bool:
        """是否有事件响应器真正匹配并执行。"""
        return bool(self.matchers)


# --------------------------------------------------------------------------- #
# 全局状态
# --------------------------------------------------------------------------- #

_plugins: dict[str, PluginRecord] = {}
_module_index: dict[str, str] = {}
_current: ContextVar[PluginRecord | None] = ContextVar("neko_nonebot_current_plugin", default=None)
_driver: Any = None
_reply_sink: ContextVar[list[str] | None] = ContextVar("neko_nonebot_reply_sink", default=None)
_fallback_sender: Callable[[str], Awaitable[bool]] | None = None
_log_sink: Callable[[str, str, str], None] | None = None
_resolver: Callable[[str], ModuleType | None] | None = None
_session_bot: Any = None
_global_hooks = GlobalHooks()


def bind_runtime(
    *,
    driver: Any,
    reply_fallback: Callable[[str, str], Awaitable[bool]] | None = None,
    log_sink: Callable[[str, str, str], None] | None = None,
    resolver: Callable[[str], ModuleType | None] | None = None,
) -> None:
    """绑定（或刷新）桥接运行时。幂等：重复调用只覆盖非 ``None`` 的部分。"""
    global _driver, _fallback_sender, _log_sink, _resolver
    with _LOCK:
        _driver = driver
        if reply_fallback is not None:
            _fallback_sender = reply_fallback
        if log_sink is not None:
            _log_sink = log_sink
        if resolver is not None:
            _resolver = resolver


def set_session_bot(bot: Any) -> None:
    """登记桥接层合成的逻辑 Bot（``nonebot.get_bot()`` 用）。"""
    global _session_bot
    with _LOCK:
        _session_bot = bot


def get_session_bot() -> Any:
    return _session_bot


def driver_or_none() -> Any:
    return _driver


def get_global_hooks() -> GlobalHooks:
    return _global_hooks


# --------------------------------------------------------------------------- #
# 插件导入上下文
# --------------------------------------------------------------------------- #


@contextmanager
def collector(plugin_id: str, module_name: str = "", path: str = "") -> Iterator[PluginRecord]:
    """进入某个插件的导入上下文；导入期间产生的 Matcher / Hook 都记到它名下。

    同一个 ``plugin_id`` 重复进入时会复用记录（幂等），但会清空旧的 matcher / hook，
    避免重复导入导致规格翻倍。
    """
    with _LOCK:
        record = _plugins.get(plugin_id)
        if record is None:
            record = PluginRecord(plugin_id=plugin_id)
            _plugins[plugin_id] = record
        else:
            record.matchers = []
            record.hooks = GlobalHooks()
            record.preprocessors = {}
        record.module_name = module_name or record.module_name
        record.path = path or record.path
        if module_name:
            _module_index[module_name] = plugin_id
    token = _current.set(record)
    try:
        yield record
    finally:
        _current.reset(token)


def current_plugin() -> PluginRecord | None:
    """当前正在导入/运行的插件记录（上下文变量）。"""
    return _current.get()


def current_plugin_id() -> str:
    record = _current.get()
    return record.plugin_id if record is not None else ""


def plugin_record(plugin_id: str) -> PluginRecord | None:
    with _LOCK:
        return _plugins.get(plugin_id)


def all_plugins() -> dict[str, PluginRecord]:
    with _LOCK:
        return dict(_plugins)


def plugin_id_for_module(module_name: str) -> str:
    with _LOCK:
        if module_name in _module_index:
            return _module_index[module_name]
    for plugin_id in list(_plugins):
        if module_name == plugin_id or module_name.startswith(f"{plugin_id}."):
            return plugin_id
    return ""


def attach_plugin_module(plugin_id: str, module: ModuleType) -> None:
    """登记插件模块对象（``nonebot.require`` / ``get_plugin`` 用）。"""
    with _LOCK:
        record = _plugins.get(plugin_id)
        if record is None:
            record = PluginRecord(plugin_id=plugin_id)
            _plugins[plugin_id] = record
        record.module = module
        record.module_name = record.module_name or module.__name__
        _module_index[module.__name__] = plugin_id
        _module_index[plugin_id] = plugin_id


def remove_plugin(plugin_id: str) -> None:
    """卸载记录（重新加载前调用，保证幂等）。"""
    with _LOCK:
        record = _plugins.pop(plugin_id, None)
        if record is not None:
            for name, owner in list(_module_index.items()):
                if owner == plugin_id:
                    _module_index.pop(name, None)


def set_plugin_config_snapshot(plugin_id: str, data: dict[str, Any]) -> None:
    with _LOCK:
        record = _plugins.get(plugin_id)
        if record is not None:
            record.config_snapshot = dict(data)


def plugin_config_snapshot(plugin_id: str = "") -> dict[str, Any]:
    pid = plugin_id or current_plugin_id()
    with _LOCK:
        record = _plugins.get(pid)
        return dict(record.config_snapshot) if record is not None else {}


# --------------------------------------------------------------------------- #
# Matcher 登记
# --------------------------------------------------------------------------- #


def register_matcher(matcher_cls: type[Any]) -> None:
    """登记一个新创建的 Matcher（由 ``nonebot.matcher.Matcher.new`` 调用）。

    全局优先级表由 ``nonebot.matcher.matchers`` 维护，这里只记录插件归属。
    """
    record = _current.get()
    if record is None:
        return
    with _LOCK:
        if matcher_cls not in record.matchers:
            record.matchers.append(matcher_cls)


def register_session_matcher(plugin_id: str, matcher_cls: type[Any]) -> None:
    """登记会话续跑用的临时 Matcher（``Matcher.run`` 在 pause / reject 后调用）。"""
    with _LOCK:
        record = _plugins.get(plugin_id)
        if record is not None and matcher_cls not in record.matchers:
            record.matchers.append(matcher_cls)


def plugin_matchers(plugin_id: str) -> list[type[Any]]:
    with _LOCK:
        record = _plugins.get(plugin_id)
        return list(record.matchers) if record is not None else []


def unregister_matcher(plugin_id: str, matcher_cls: type[Any]) -> None:
    """从插件记录里移除一个 Matcher（过期会话响应器清理用）。"""
    with _LOCK:
        record = _plugins.get(plugin_id)
        if record is not None and matcher_cls in record.matchers:
            record.matchers.remove(matcher_cls)


def record_preprocessor(kind: str, func: Any) -> None:
    """登记一个事件/运行预处理函数（按插件归属）。"""
    target = _current.get()
    if target is None:
        _LOGGER.warning("nonebot shim: preprocessor registered outside a plugin import: %r", func)
        return
    bucket = target.preprocessors.setdefault(kind, [])
    if func not in bucket:
        bucket.append(func)


def plugin_preprocessors(plugin_id: str, kind: str) -> list[Any]:
    with _LOCK:
        record = _plugins.get(plugin_id)
        if record is None:
            return []
        return list(record.preprocessors.get(kind, ()))


def all_matchers() -> list[type[Any]]:
    with _LOCK:
        result: list[type[Any]] = []
        for record in _plugins.values():
            result.extend(record.matchers)
        return result


# --------------------------------------------------------------------------- #
# Hook 登记
# --------------------------------------------------------------------------- #


def record_hook(kind: str, func: Callable[..., Any]) -> None:
    """登记一个驱动级钩子；有当前插件上下文时归属该插件。"""
    if kind not in HOOK_KINDS:
        raise ValueError(f"unknown nonebot hook kind: {kind}")
    target = _current.get()
    bucket = (target.hooks if target is not None else _global_hooks).of(kind)
    if func not in bucket:
        bucket.append(func)


def plugin_hooks(plugin_id: str, kind: str) -> list[Callable[..., Any]]:
    with _LOCK:
        record = _plugins.get(plugin_id)
        hooks = list(record.hooks.of(kind)) if record is not None else []
    hooks.extend(_global_hooks.of(kind))
    return hooks


# --------------------------------------------------------------------------- #
# 回复收集
# --------------------------------------------------------------------------- #


def begin_run(plugin_id: str = "") -> RunReport:
    """开始一次事件分发，返回结果收集器。"""
    return RunReport(plugin_id=plugin_id or current_plugin_id())


@contextmanager
def capture_replies(report: RunReport) -> Iterator[RunReport]:
    """把 ``Bot.send`` / ``Matcher.send`` 的文本收进 ``report.replies``。"""
    token = _reply_sink.set(report.replies)
    try:
        yield report
    finally:
        _reply_sink.reset(token)


def emit_reply(text: str, source: str = "") -> bool:
    """收集一段由插件发出的文本。

    返回 ``True`` 表示被当前事件分发捕获（进入 ``replies``）；
    返回 ``False`` 表示没有活跃分发，文本已交给 ``HostServices.send_chat``（如果已绑定）。

    ``source`` 是发出这段文本的插件标识（由桥接层合成的 Bot 携带），用于日志与来源标注。
    """
    body = str(text)
    if not body:
        return False
    sink = _reply_sink.get()
    if sink is not None:
        sink.append(body)
        return True
    sender = _fallback_sender
    if sender is None:
        _LOGGER.warning("nonebot shim: message dropped (no reply sink bound): %r", body)
        return False
    try:
        asyncio.get_running_loop().create_task(sender(body, source))
    except RuntimeError:
        _LOGGER.warning("nonebot shim: message dropped (no running loop): %r", body)
        return False
    return False


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #


def emit_log(level: str, message: str, source: str = "") -> None:
    """把 shim / 插件日志转发给 N.E.K.O.（未绑定时退回标准库 logging）。"""
    normalized = level if level in {"debug", "info", "warning", "error"} else "info"
    sink = _log_sink
    origin = source or current_plugin_id()
    if sink is not None:
        try:
            sink(normalized, message, origin)
            return
        except Exception as exc:  # noqa: BLE001 — 日志通道故障不能影响插件逻辑
            _LOGGER.warning("nonebot shim: log sink failed: %r", exc)
    getattr(_LOGGER, normalized if normalized != "success" else "info")(message)


# --------------------------------------------------------------------------- #
# 依赖插件解析 / 内省
# --------------------------------------------------------------------------- #


def resolve_plugin_module(name: str) -> ModuleType | None:
    """解析 ``nonebot.require(name)``。"""
    with _LOCK:
        record = _plugins.get(name)
        if record is not None and record.module is not None:
            return record.module
        owner = _module_index.get(name)
        if owner is not None:
            record = _plugins.get(owner)
            if record is not None and record.module is not None:
                return record.module
        resolver = _resolver
    if resolver is not None:
        return resolver(name)
    return None


def describe_matcher(matcher_cls: type[Any]) -> dict[str, Any]:
    """把一个 Matcher 类整理成桥接层能直接消费的描述（只读，不修改）。"""
    trigger = getattr(matcher_cls, "_trigger", None)
    handlers = list(getattr(matcher_cls, "handlers", ()) or ())
    doc = ""
    for handler in handlers:
        call = getattr(handler, "call", None)
        target = call if call is not None else handler
        text = (getattr(target, "__doc__", None) or "").strip()
        if text:
            doc = text.splitlines()[0]
            break
    source = getattr(matcher_cls, "_source", None)
    kind = getattr(trigger, "kind", "") or (getattr(matcher_cls, "type", "") or "message")
    patterns = tuple(getattr(trigger, "patterns", ()) or ())
    label = getattr(trigger, "label", "") or f"{kind}:{getattr(matcher_cls, 'type', '') or 'any'}"
    return {
        "label": label,
        "kind": kind,
        "patterns": patterns,
        "type": getattr(matcher_cls, "type", "") or "",
        "priority": int(getattr(matcher_cls, "priority", 1)),
        "block": bool(getattr(matcher_cls, "block", False)),
        "temp": bool(getattr(matcher_cls, "temp", False)),
        "handler_count": len(handlers),
        "doc": doc,
        "module": str(getattr(source, "module_name", "") or ""),
        "lineno": int(getattr(source, "lineno", 0) or 0),
        "plugin_id": str(getattr(source, "plugin_id", "") or ""),
        "class": matcher_cls,
    }


# 兼容占位：早期版本导出过该名字，保留以免外部脚本引用失败。
describematcher_placeholder = describe_matcher
