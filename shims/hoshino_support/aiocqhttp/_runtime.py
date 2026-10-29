"""``aiocqhttp`` 兼容 shim 的桥接运行时（不是上游 API）。

这是 shim 与 ``bridge/backends/hoshino.py`` 之间唯一的粘合点，负责：

* 保存宿主能力面（:class:`ShimHostBridge`：发消息 / 日志 / 数据目录 / 插件配置）；
* 输出收集：插件在**一次分发期间**发出的文本进 ``RunReport.replies``，
  分发之外发言回落 ``HostServices.send_chat``（与 NoneBot 兼容层同一套语义）；
* 插件上下文（ContextVar）：当前插件 id 与当前事件；
* 能力缺失的登记（``native["unsupported_calls"]``）。

``hoshino`` 包在这一层之上再加 Service / 触发器 / 生命周期等记录。所有模块级状态
都必须幂等可重入：同一个插件重复 load、多个插件并存、多个 N.E.K.O. 插件实例共存
时都不能破坏状态。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Protocol

from .errors import UnsupportedCapabilityError, install_recorder

__all__ = [
    "DELIVERED_CAPTURED",
    "DELIVERED_DROPPED",
    "DELIVERED_HOST",
    "RUNTIME",
    "ShimHostBridge",
    "UnsupportedCapabilityError",
    "capture_output",
    "current_event",
    "current_plugin_id",
    "deliver",
    "deliver_message",
    "emit_log",
    "emit_message",
    "emit_reply",
    "scope",
]

_LOGGER = logging.getLogger("bot_bridge.hoshino.shim")

#: 投递状态：进了当前分发的回复 / 交给了宿主（分发之外发言）/ 真的没送出去。
DELIVERED_CAPTURED = "captured"
DELIVERED_HOST = "host"
DELIVERED_DROPPED = "dropped"

_LOG_LEVELS = frozenset({"debug", "info", "warning", "error"})
_REPLY_LEVELS = {"success": "info", "exception": "error", "critical": "error"}


class ShimHostBridge(Protocol):
    """兼容层注入的 N.E.K.O. 能力面（实现在 ``bridge/backends/hoshino.py``）。"""

    def send_text(
        self,
        plugin_id: str,
        text: str,
        *,
        source: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> Awaitable[bool]:
        """把一段文本送进 N.E.K.O. 对话，返回宿主是否接受。"""
        ...

    def log(self, level: str, message: str, *, plugin_id: str = "", source: str = "") -> None:
        """写一条日志到宿主日志。"""
        ...

    def data_dir(self, plugin_id: str) -> Path:
        """该插件在 N.E.K.O. 侧的数据目录（库作者负责创建）。"""
        ...

    def load_plugin_config(self, plugin_id: str) -> Awaitable[Mapping[str, Any]]:
        """读取该插件的用户配置（``HostServices.plugin_config``）。"""
        ...

    def save_plugin_config(self, plugin_id: str, data: Mapping[str, Any]) -> Awaitable[bool]:
        """写回该插件的用户配置。"""
        ...


class _Runtime:
    """进程级运行时状态。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._host: ShimHostBridge | None = None
        self._bots: dict[str, Any] = {}
        self._unsupported: dict[str, list[str]] = {}
        self._plugin_id: ContextVar[str] = ContextVar("neko_hoshino_plugin", default="")
        self._event: ContextVar[Any] = ContextVar("neko_hoshino_event", default=None)
        self._reply_sink: ContextVar[list[str] | None] = ContextVar("neko_hoshino_replies", default=None)
        self._extra_sink: ContextVar[list[dict[str, Any]] | None] = ContextVar("neko_hoshino_extras", default=None)
        self._fallback: Callable[[str, str, Mapping[str, Any]], Awaitable[bool]] | None = None

    # ------------------------------------------------------------ 宿主能力面

    def install_host(self, host: ShimHostBridge) -> None:
        """注入宿主能力面（重复注入覆盖，用于同进程内的多次桥接）。"""
        with self._lock:
            self._host = host

    def host_or_none(self) -> ShimHostBridge | None:
        with self._lock:
            return self._host

    def host(self) -> ShimHostBridge:
        with self._lock:
            host = self._host
        if host is None:
            raise RuntimeError(
                "the Hoshino compatibility shim is not attached to a host; "
                "it must be loaded through bot_bridge",
            )
        return host

    def set_reply_fallback(
        self,
        fallback: Callable[[str, str, Mapping[str, Any]], Awaitable[bool]],
    ) -> None:
        """登记"分发之外发言"的回落通道（转 ``HostServices.send_chat``）。"""
        with self._lock:
            self._fallback = fallback

    def log(self, level: str, message: str, *, plugin_id: str = "", source: str = "") -> None:
        """写日志；宿主未注入时退回标准库（不抛异常）。"""
        host = self.host_or_none()
        normalized = level if level in _LOG_LEVELS else _REPLY_LEVELS.get(level, "info")
        if host is not None:
            try:
                host.log(normalized, message, plugin_id=plugin_id or "", source=source)
                return
            except Exception as exc:  # noqa: BLE001 — 日志通道故障不影响插件逻辑
                _LOGGER.warning("hoshino shim: log sink failed: %r", exc)
        _LOGGER.log(getattr(logging, normalized.upper(), logging.INFO), message)

    # ------------------------------------------------------------------ Bot

    def bind_bot(self, plugin_id: str, bot: Any) -> None:
        with self._lock:
            if plugin_id:
                self._bots[plugin_id] = bot

    def bot_or_none(self, plugin_id: str = "") -> Any:
        pid = plugin_id or self.current_plugin_id()
        with self._lock:
            return self._bots.get(pid)

    def any_bot(self) -> Any:
        """任一已绑定的 bot（``hoshino.get_bot()`` 在没有插件上下文时的回落）。"""
        with self._lock:
            for bot in reversed(list(self._bots.values())):
                return bot
        return None

    # ------------------------------------------------------------ 插件上下文

    @contextmanager
    def scope(self, plugin_id: str) -> Iterator[str]:
        """进入某个插件的导入 / 运行上下文。"""
        token = self._plugin_id.set(plugin_id or "")
        try:
            yield plugin_id or ""
        finally:
            self._plugin_id.reset(token)

    def current_plugin_id(self) -> str:
        return self._plugin_id.get()

    @contextmanager
    def running_event(self, event: Any) -> Iterator[Any]:
        """登记"当前正在派发的事件"（``hoshino.util`` 的群成员查询要用）。"""
        token = self._event.set(event)
        try:
            yield event
        finally:
            self._event.reset(token)

    def current_event(self) -> Any:
        return self._event.get()

    # ------------------------------------------------------------ 输出收集

    @contextmanager
    def capture(self, replies: list[str], extras: list[dict[str, Any]]) -> Iterator[None]:
        """把 ``bot.send`` / ``sv.broadcast`` 的输出收进给定容器。"""
        reply_token = self._reply_sink.set(replies)
        extra_token = self._extra_sink.set(extras)
        try:
            yield
        finally:
            self._reply_sink.reset(reply_token)
            self._extra_sink.reset(extra_token)

    def emit_reply(self, text: str, source: str = "", metadata: Mapping[str, Any] | None = None) -> bool:
        """收集一段文本；``True`` = 进了当前分发的回复。"""
        return self.deliver(text, source, metadata) == DELIVERED_CAPTURED

    def deliver(self, text: str, source: str = "", metadata: Mapping[str, Any] | None = None) -> str:
        """把一段文本交出去，返回投递状态（``captured`` / ``host`` / ``dropped``）。"""
        body = str(text)
        if not body:
            return DELIVERED_DROPPED
        sink = self._reply_sink.get()
        extra = self._extra_sink.get()
        if sink is not None:
            sink.append(body)
            if extra is not None:
                extra.append({"text": body, "source": source, **(dict(metadata or {}))})
            return DELIVERED_CAPTURED
        return DELIVERED_HOST if self._fallback_send(body, source, metadata) else DELIVERED_DROPPED

    def emit_message(
        self,
        message: Any,
        source: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        """收集一条消息；``True`` = 进了当前分发的回复。"""
        return self.deliver_message(message, source, metadata) == DELIVERED_CAPTURED

    def deliver_message(
        self,
        message: Any,
        source: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """投递一条消息（``str`` / ``Message`` / ``MessageSegment``），返回投递状态。

        只有文本能进 N.E.K.O. 对话；非文本段（图片 / at / 表情…）记进结构化记录并写一条
        日志说明它们没被发出去 —— 绝不假装图片送到了。
        """
        from .message import cls_to_segments

        segments = [
            item
            for item in cls_to_segments(message)
            if item.type != "text" or str(item.data.get("text") or "")
        ]
        text = "".join(str(item.data.get("text") or "") for item in segments if item.type == "text")
        dropped = [{"kind": item.type, "data": dict(item.data)} for item in segments if item.type != "text"]
        record = dict(metadata or {})
        record["segments"] = dropped
        run_active = self._is_run_active()
        if text:
            # ``deliver`` 会把结构化记录挂到当前分发的 extras 上（非文本段也在 record["segments"] 里）。
            status = self.deliver(text, source, record)
        elif run_active:
            sink = self._extra_sink.get()
            if sink is not None:
                sink.append(record)
            status = DELIVERED_CAPTURED
        else:
            status = DELIVERED_DROPPED
        if dropped:
            level = "debug" if run_active else "warning"
            self.log(
                level,
                f"hoshino: 消息段 {[item['kind'] for item in dropped]} 只记录不发送（N.E.K.O. 对话只承载文本）",
                source=source,
            )
        return status

    def _is_run_active(self) -> bool:
        return self._reply_sink.get() is not None

    def _fallback_send(self, text: str, source: str, metadata: Mapping[str, Any] | None) -> bool:
        """分发之外的发言：优先用绑定好的回落函数，否则直接用宿主能力面。

        返回 ``True`` 表示已经交给宿主（异步投递），``False`` 表示真的没送出去。
        """
        fallback = self._fallback
        host = self.host_or_none()
        if fallback is None and host is None:
            _LOGGER.warning("hoshino shim: message dropped (no host bound): %r", text)
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _LOGGER.warning("hoshino shim: message dropped (no running loop): %r", text)
            return False
        payload = dict(metadata or {})
        if fallback is not None:
            loop.create_task(fallback(text, source, payload))
        else:
            plugin_id = self.current_plugin_id()
            loop.create_task(
                host.send_text(  # type: ignore[union-attr] — 上面已判过 None
                    plugin_id,
                    text,
                    source=source or plugin_id,
                    metadata=payload,
                ),
            )
        return True

    # -------------------------------------------------------- 未映射能力登记

    def register_unsupported_sink(self, plugin_id: str) -> list[str]:
        """返回一个**原地可变**的列表，兼容层把它挂到 ``LoadedPlugin.native`` 上。"""
        with self._lock:
            return self._unsupported.setdefault(plugin_id, [])

    def record_unsupported(self, plugin_id: str, capability: str) -> None:
        """登记一次能力缺失调用（去重，保持出现顺序）。"""
        if not plugin_id or not capability:
            return
        with self._lock:
            bucket = self._unsupported.setdefault(plugin_id, [])
            if capability not in bucket:
                bucket.append(capability)

    def unsupported_calls(self, plugin_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._unsupported.get(plugin_id, ()))

    def forget_plugin(self, plugin_id: str) -> None:
        """清掉某个插件的运行期痕迹（重复 load 前调用，保证幂等）。"""
        with self._lock:
            self._bots.pop(plugin_id, None)
            self._unsupported.pop(plugin_id, None)


RUNTIME = _Runtime()


# --------------------------------------------------------------------------- #
# 便捷入口（shim 内部与 ``hoshino`` 包都用这一组）
# --------------------------------------------------------------------------- #


def scope(plugin_id: str) -> Any:
    return RUNTIME.scope(plugin_id)


def current_plugin_id() -> str:
    return RUNTIME.current_plugin_id()


def current_event() -> Any:
    return RUNTIME.current_event()


def capture_output(replies: list[str], extras: list[dict[str, Any]]) -> Any:
    return RUNTIME.capture(replies, extras)


def emit_reply(text: str, source: str = "", metadata: Mapping[str, Any] | None = None) -> bool:
    return RUNTIME.emit_reply(text, source, metadata)


def emit_message(message: Any, source: str = "", metadata: Mapping[str, Any] | None = None) -> bool:
    return RUNTIME.emit_message(message, source, metadata)


def deliver(text: str, source: str = "", metadata: Mapping[str, Any] | None = None) -> str:
    """投递一段文本，返回 ``captured`` / ``host`` / ``dropped``。"""
    return RUNTIME.deliver(text, source, metadata)


def deliver_message(message: Any, source: str = "", metadata: Mapping[str, Any] | None = None) -> str:
    """投递一条消息，返回 ``captured`` / ``host`` / ``dropped``。"""
    return RUNTIME.deliver_message(message, source, metadata)


def emit_log(level: str, message: str, source: str = "", plugin_id: str = "") -> None:
    """转发插件日志（未注入宿主时退回标准库）。"""
    origin = source or current_plugin_id()
    RUNTIME.log(level, message, plugin_id=plugin_id, source=origin)


# 能力缺失一抛出就登记（插件在哪里抛的都算），兼容层据此把它们挂到 native 上。
install_recorder(lambda capability: RUNTIME.record_unsupported(RUNTIME.current_plugin_id(), capability))
