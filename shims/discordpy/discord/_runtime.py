"""discord.py 兼容 shim 的桥接运行时。

本模块**不属于上游 discord.py 的公开 API**，它是 ``bot_bridge`` 的 discord.py 兼容层
(``bridge/backends/discordpy.py``) 与 shim 之间唯一的粘合点：

* **运行上下文**：当前插件（``activate`` 设置，后台任务创建时会继承）与当前回复收集器
  （:class:`RunReport`）；
* **回复收集**：``ctx.send`` / ``channel.send`` / ``message.reply`` 把文本写进当前
  ``CommandResult.replies``（由 N.E.K.O. 决定怎么显示）；分发之外（后台任务）的发言
  交给 ``HostServices.send_chat``；
* **没有载体的东西**：附件（``File`` / ``Attachment``）、交互组件（``View`` / ``Button``）
  在 N.E.K.O. 里没有等价投放面，因此**结构化记录 + 警告日志**，绝不假装送到了；
* **显式不支持**：``UnsupportedCapabilityError``（N.E.K.O. 没有等价实现的 discord.py API
  一律抛它，并把 capability 登记进当前插件的记录）；
* **日志转发**：shim 的 loguru/logging 风格 logger → ``HostServices.log``。

设计约束：所有模块级状态都必须幂等可重入（同一个插件重复 load / 多个插件 / 多个
N.E.K.O. 插件实例并存都不能破坏状态）。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Protocol

__all__ = [
    "RunReport",
    "ShimHostBridge",
    "UnsupportedCapabilityError",
    "activate",
    "active_loop",
    "begin_run",
    "bind_host_bridge",
    "bind_loop",
    "bind_reply_fallback",
    "current_plugin_id",
    "current_report",
    "deliver",
    "emit_log",
    "host_bridge",
    "note_attachment",
    "note_component",
    "note_unsupported",
    "plugin_capabilities",
    "record_command",
    "record_listener",
    "record_error",
    "record_check_failure",
    "record_command_error",
    "reset_plugin",
    "unsupported",
]

_LOGGER = logging.getLogger("bot_bridge.discord.shim")

#: 允许转发给宿主的日志级别（其余级别归一到 ``info``）。
_LEVELS = frozenset({"debug", "info", "warning", "error"})

_LOCK = threading.RLock()


# --------------------------------------------------------------------------- #
# 错误 / 宿主能力面
# --------------------------------------------------------------------------- #


class UnsupportedCapabilityError(NotImplementedError):
    """外来插件用到了 N.E.K.O. 侧没有等价实现的 discord.py 能力。

    这是个诚实错误：绝不假装成功。兼容层会把它转成 ``bridge.errors.UnsupportedCapabilityError``
    （结构化失败），并把它记进 ``LoadedPlugin.native["unsupported_capabilities"]``。
    """

    code = "unsupported_capability"

    def __init__(self, capability: str, detail: str = "") -> None:
        message = f"discord.{capability} has no N.E.K.O. equivalent: {detail or 'not mapped'}"
        super().__init__(message)
        self.capability = capability
        self.detail = detail

    def to_payload(self) -> dict[str, Any]:
        """结构化失败负载（桥接层原样转交调用方）。"""
        return {"code": self.code, "capability": self.capability, "error": str(self), "detail": self.detail}


class ShimHostBridge(Protocol):
    """桥接层注入的宿主能力面（duck-typed，实现在 ``bridge/backends/discordpy.py``）。"""

    def log(self, level: str, message: str, *, plugin_id: str = "", source: str = "") -> None:
        """写一条宿主日志。"""
        ...

    def data_dir(self, plugin_id: str) -> Any:
        """该插件的持久数据目录。"""
        ...

    async def send_chat(self, text: str, *, plugin_id: str, metadata: dict[str, Any]) -> bool:
        """把一段插件文本送进 N.E.K.O. 对话。"""
        ...


# --------------------------------------------------------------------------- #
# 运行上下文
# --------------------------------------------------------------------------- #


@dataclass
class RunReport:
    """一次消息 / 事件分发的收集器（桥接层据此填 ``CommandResult``）。"""

    plugin_id: str = ""
    replies: list[str] = field(default_factory=list)
    #: 命中的命令（qualified name，例如 ``admin ping``）
    commands: list[str] = field(default_factory=list)
    #: 触发过的监听器（``<event>:<name>``）
    listeners: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: ``File`` / ``Attachment`` 发送请求（N.E.K.O. 没有附件投放面，只记录）
    attachments: list[dict[str, Any]] = field(default_factory=list)
    #: ``View`` / ``Button`` / ``Select`` 等交互组件（同上）
    components: list[dict[str, Any]] = field(default_factory=list)
    #: 本轮触发的显式「能力缺失」
    unsupported: list[dict[str, Any]] = field(default_factory=list)
    #: 前置检查（check）失败的记录
    check_failures: list[dict[str, Any]] = field(default_factory=list)
    #: 命令级失败（``{"command": ..., "type": ..., "error": ...}``），桥接层用它填 ``CommandResult.error``
    command_errors: list[dict[str, Any]] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)

    @property
    def handled(self) -> bool:
        """是否有命令命中，或 on_message 监听器真的回了话。

        与上游一致：只有 ``process_commands`` 找到了命令才算「这条消息归我处理」；
        监听器是副作用，只有它产生了回复时才算接管（否则别的插件还有机会）。
        """
        return bool(self.commands) or bool(self.listeners and self.replies)

    def snapshot(self) -> dict[str, Any]:
        """给 ``CommandResult.data`` / 工具返回值用的结构化快照。"""
        data: dict[str, Any] = {
            "handled": self.handled,
            "commands": list(self.commands),
            "listeners": list(self.listeners),
            "errors": list(self.errors),
            "attachments": [dict(item) for item in self.attachments],
            "components": [dict(item) for item in self.components],
            "unsupported": [dict(item) for item in self.unsupported],
            "check_failures": [dict(item) for item in self.check_failures],
            "command_errors": [dict(item) for item in self.command_errors],
        }
        data.update(self.extras)
        return data


_current_plugin: ContextVar[str] = ContextVar("neko_discord_plugin", default="")
_current_report: ContextVar[RunReport | None] = ContextVar("neko_discord_report", default=None)

_host_bridges: dict[str, ShimHostBridge] = {}
_reply_fallbacks: dict[str, Callable[[str], Awaitable[bool]]] = {}
_capabilities: dict[str, list[dict[str, Any]]] = {}
_loop: asyncio.AbstractEventLoop | None = None


def bind_host_bridge(plugin_id: str, bridge: ShimHostBridge) -> None:
    """登记某个插件的宿主能力面（重复登记只覆盖）。"""
    with _LOCK:
        _host_bridges[plugin_id] = bridge


def bind_reply_fallback(plugin_id: str, sender: Callable[[str], Awaitable[bool]]) -> None:
    """登记「分发之外发言」的回落投递函数（``HostServices.send_chat``）。"""
    with _LOCK:
        _reply_fallbacks[plugin_id] = sender


def bind_loop(loop: asyncio.AbstractEventLoop | None) -> None:
    """登记插件真正运行的事件循环（``start`` 时由桥接层绑定）。

    上游的 ``bot.loop`` 是真实 gateway 循环；N.E.K.O. 里插件跑在桥接层的循环上，
    因此这里显式记下来，供 ``Client.loop`` 与 ``Client.create_task`` 使用。
    """
    global _loop
    with _LOCK:
        _loop = loop


def active_loop() -> asyncio.AbstractEventLoop | None:
    """当前活跃的事件循环：绑定过的桥接循环优先，否则取「正在跑的那个」。"""
    with _LOCK:
        bound = _loop
    if bound is not None and not bound.is_closed():
        return bound
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return bound if bound is not None and not bound.is_closed() else None


def host_bridge(plugin_id: str = "") -> ShimHostBridge | None:
    with _LOCK:
        return _host_bridges.get(plugin_id or current_plugin_id())


def current_plugin_id() -> str:
    return _current_plugin.get()


def current_report() -> RunReport | None:
    return _current_report.get()


@contextmanager
def activate(plugin_id: str, report: RunReport | None = None) -> Iterator[RunReport | None]:
    """进入某个插件的运行上下文。

    绑定的插件归属会被同一任务里后来创建的 asyncio 任务继承（``create_task`` 复制
    context），因此后台任务里的 ``channel.send`` 也能正确归到插件名下。
    """
    plugin_token = _current_plugin.set(plugin_id)
    report_token = _current_report.set(report) if report is not None else None
    try:
        yield report
    finally:
        _current_plugin.reset(plugin_token)
        if report_token is not None:
            _current_report.reset(report_token)


def begin_run(plugin_id: str = "") -> RunReport:
    """新建一次分发的收集器。"""
    return RunReport(plugin_id=plugin_id or current_plugin_id())


def reset_plugin(plugin_id: str) -> None:
    """清掉某个插件的运行期登记（重新加载前调用，保证不串状态）。"""
    with _LOCK:
        _capabilities.pop(plugin_id, None)
        _reply_fallbacks.pop(plugin_id, None)
        _host_bridges.pop(plugin_id, None)


# --------------------------------------------------------------------------- #
# 记录
# --------------------------------------------------------------------------- #


def record_command(name: str, *, plugin_id: str = "") -> None:
    """记下命中的命令（同一轮不重复记）。"""
    report = _current_report.get()
    if report is None:
        return
    label = str(name)
    if label and label not in report.commands:
        report.commands.append(label)
    pid = plugin_id or current_plugin_id()
    if pid:
        report.extras.setdefault("plugin_id", pid)


def record_listener(event: str, name: str) -> None:
    report = _current_report.get()
    if report is None:
        return
    label = f"{event}:{name}"
    if label not in report.listeners:
        report.listeners.append(label)


def record_error(detail: str, *, level: str = "error", source: str = "") -> None:
    """记录一条错误：进 report（结构化）+ 写宿主日志（可观测）。"""
    report = _current_report.get()
    text = str(detail)
    if report is not None:
        report.errors.append(text)
    emit_log(level, text, source=source)


def record_check_failure(name: str, error: BaseException) -> None:
    report = _current_report.get()
    if report is not None:
        report.check_failures.append({"command": name, "error": f"{type(error).__name__}: {error}"})
    record_error(f"{name} 的前置检查未通过：{type(error).__name__}: {error}", level="warning")


def record_command_error(command: str, error: BaseException) -> str:
    """记录一次命令级失败（``CommandResult.error`` 取这里的第一条）。"""
    detail = f"{type(error).__name__}: {error}"
    report = _current_report.get()
    if report is not None:
        report.command_errors.append({"command": str(command), "type": type(error).__name__, "error": detail})
    record_error(f"command '{command}' failed: {detail}", level="warning")
    return detail


def note_attachment(name: str, url: str = "", size: int = 0, *, extra: dict[str, Any] | None = None) -> None:
    """记录一次附件发送请求（N.E.K.O. 的 ``send_chat`` 只承载文本）。"""
    report = _current_report.get()
    payload = {"name": str(name), "url": str(url), "size": int(size or 0)}
    if extra:
        payload.update(extra)
    if report is not None:
        report.attachments.append(payload)
    # 附件没有 N.E.K.O. 投放面：无论是否在分发里都要留一条警告日志（不静默）。
    emit_log("warning", f"附件没有 N.E.K.O. 投放面，只记录不投递：{payload}", source="discord")
    plugin_id = current_plugin_id()
    if plugin_id:
        _remember_capability(plugin_id, "File/Attachment", "N.E.K.O. 的 send_chat 只承载文本，附件无法投递")


def note_component(kind: str, detail: str = "", *, extra: dict[str, Any] | None = None) -> None:
    """记录一次交互组件（View / Button / Select / Modal）的使用。"""
    report = _current_report.get()
    payload = {"kind": str(kind), "detail": str(detail)}
    if extra:
        payload.update(extra)
    if report is not None:
        report.components.append(payload)
    emit_log("warning", f"交互组件没有 N.E.K.O. 等价实现：{payload}", source="discord")
    plugin_id = current_plugin_id()
    if plugin_id:
        _remember_capability(plugin_id, f"ui.{kind}", "N.E.K.O. 没有消息组件 / 交互回调面")


def unsupported(capability: str, detail: str = "") -> UnsupportedCapabilityError:
    """登记并**抛出**一次能力缺失（返回值只是为了 ``raise runtime.unsupported(...)`` 顺手）。"""
    error = UnsupportedCapabilityError(capability, detail)
    report = _current_report.get()
    payload = error.to_payload()
    if report is not None:
        report.unsupported.append(payload)
    plugin_id = current_plugin_id()
    if plugin_id:
        _remember_capability(plugin_id, capability, detail)
    emit_log("warning", str(error), source="discord")
    return error


def note_unsupported(capability: str, detail: str = "") -> dict[str, Any]:
    """只登记能力缺失（不抛异常），用于装饰器这类「用到了但暂时不致命」的场景。"""
    error = UnsupportedCapabilityError(capability, detail)
    report = _current_report.get()
    payload = error.to_payload()
    if report is not None:
        report.unsupported.append(payload)
    plugin_id = current_plugin_id()
    if plugin_id:
        _remember_capability(plugin_id, capability, detail)
    return payload


def _remember_capability(plugin_id: str, capability: str, detail: str) -> None:
    with _LOCK:
        bucket = _capabilities.setdefault(plugin_id, [])
        key = (capability, detail)
        if all((item.get("capability"), item.get("detail")) != key for item in bucket):
            bucket.append({"capability": capability, "detail": detail})


def plugin_capabilities(plugin_id: str) -> list[dict[str, Any]]:
    """该插件用到的、N.E.K.O. 侧没有等价实现的能力（结构化清单）。"""
    with _LOCK:
        return [dict(item) for item in _capabilities.get(plugin_id, ())]


# --------------------------------------------------------------------------- #
# 回复
# --------------------------------------------------------------------------- #


async def deliver(
    text: str,
    *,
    source: str = "",
    plugin_id: str = "",
    sender: Callable[[str], Awaitable[bool]] | None = None,
) -> bool:
    """投递一段由插件产生的文本。

    返回 ``True`` 表示被当前分发捕获（进 ``replies``）；返回 ``False`` 表示没有活跃
    分发，文本已交给宿主 ``HostServices.send_chat``（如果已绑定）；两者都不成立就写
    一条警告日志并如实返回 ``False``（不假装送到）。

    ``sender`` 是这次投递专属的回落函数（桥接层把它挂在频道 / 上下文对象上），
    优先级高于按 ``plugin_id`` 登记的全局回落 —— 同一个插件被重复加载时，
    旧实例的对象仍然投给旧实例的宿主。
    """
    body = str(text)
    if not body:
        return False
    report = _current_report.get()
    if report is not None:
        report.replies.append(body)
        return True
    pid = plugin_id or current_plugin_id()
    resolved = sender
    if resolved is None:
        with _LOCK:
            resolved = _reply_fallbacks.get(pid)
    if resolved is None:
        emit_log("warning", f"分发之外的发言被丢弃（没有绑定宿主投递）：{body!r}", source=source or pid)
        return False
    try:
        return bool(await resolved(body))
    except Exception as exc:  # noqa: BLE001 — 投递失败要如实报告，不能静默
        emit_log("error", f"分发之外的发言投递失败：{type(exc).__name__}: {exc}", source=source or pid)
        return False


# --------------------------------------------------------------------------- #
# 日志
# --------------------------------------------------------------------------- #


def emit_log(level: str, message: str, *, source: str = "") -> None:
    """把 shim / 插件日志转发给 N.E.K.O.（未绑定时退回标准库 logging）。"""
    normalized = level if level in _LEVELS else "info"
    pid = current_plugin_id()
    bridge = host_bridge(pid)
    detail = f"{type(message).__name__}: {message}" if not isinstance(message, str) else message
    if bridge is not None:
        try:
            bridge.log(normalized, detail, plugin_id=pid, source=source or "discord")
            return
        except Exception as exc:  # noqa: BLE001 — 日志通道故障不能影响插件逻辑
            _LOGGER.warning("discord shim: log bridge failed: %r", exc)
    getattr(_LOGGER, normalized)(detail)
