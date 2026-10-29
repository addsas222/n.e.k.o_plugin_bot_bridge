"""AstrBot 兼容 shim 的进程级运行时。

只有 shim 自身和 bot_bridge 的 AstrBot 兼容层会导入本模块；第三方插件不应直接
使用它。它负责三件事：

* 保存桥接层注入的 N.E.K.O. 宿主能力面（:class:`ShimHostBridge`）；
* 提供 :class:`UnsupportedCapabilityError`：N.E.K.O. 侧没有等价实现的 AstrBot API
  一律抛这个错误，并把 capability 名字登记下来，**绝不假装成功**；
* 登记每个插件的原生 id / 桥接 id / 数据目录，供 ``PluginKVStoreMixin`` 等使用。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Awaitable, Mapping
from pathlib import Path
from typing import Any, Protocol

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "exception": logging.ERROR,
    "critical": logging.CRITICAL,
}


class UnsupportedCapabilityError(NotImplementedError):
    """AstrBot 插件用到了 N.E.K.O. 侧没有等价实现的能力。

    字段与 ``bot_bridge.bridge.errors.UnsupportedCapabilityError`` 同形，方便桥接层
    直接把异常翻译成结构化失败结果。
    """

    code = "unsupported_capability"

    def __init__(self, capability: str, detail: str = "") -> None:
        message = f"{capability} is not mapped to N.E.K.O."
        if detail:
            message = f"{message} ({detail})"
        super().__init__(message)
        self.capability = capability
        self.detail = detail

    def to_payload(self) -> dict[str, Any]:
        """结构化失败负载。"""
        return {
            "success": False,
            "code": self.code,
            "capability": self.capability,
            "error": str(self),
        }


class ShimHostBridge(Protocol):
    """桥接层注入的能力面（duck-typed，实现在 ``bridge/backends/astrbot.py``）。

    所有方法都按插件 id 隔离；``send_text`` / ``llm_generate`` 等到 N.E.K.O. 宿主
    的调用会原样透传。
    """

    def send_text(
        self,
        plugin_id: str,
        text: str,
        *,
        umo: str = "",
        source: str = "",
        dropped: tuple[str, ...] = (),
    ) -> Awaitable[bool]:
        """把一段文本送进 N.E.K.O. 对话，返回宿主是否接受。"""
        ...

    def llm_generate(
        self,
        plugin_id: str,
        *,
        prompt: str | list[dict[str, Any]] | None,
        system_prompt: str = "",
        model: str = "",
        image_urls: tuple[str, ...] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> Awaitable[dict[str, Any]]:
        """调用 N.E.K.O. 大模型，返回 ``HostServices.llm_generate`` 的统一字典。"""
        ...

    def plugin_config(self, plugin_id: str) -> Awaitable[Mapping[str, Any]]:
        """读取该插件的用户配置。"""
        ...

    def save_plugin_config(self, plugin_id: str, data: Mapping[str, Any]) -> Awaitable[bool]:
        """写回该插件的用户配置。"""
        ...

    def data_dir(self, plugin_id: str) -> Path:
        """该插件的数据目录。"""
        ...

    def log(self, level: str, message: str, *, plugin_id: str = "", source: str = "") -> None:
        """写一条日志到宿主日志。"""
        ...


class _Runtime:
    """进程级运行时状态（多实例安全：所有写入都在锁内且幂等）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._host: ShimHostBridge | None = None
        self._plugin_dirs: dict[str, Path] = {}
        self._plugin_keys: dict[str, str] = {}
        self._unsupported: dict[str, list[str]] = {}

    # -- 宿主能力面 -------------------------------------------------------- #

    def install_host(self, host: ShimHostBridge) -> None:
        """注入宿主能力面（重复注入会覆盖，用于同一进程内的多次桥接）。"""
        with self._lock:
            self._host = host

    def has_host(self) -> bool:
        with self._lock:
            return self._host is not None

    def host(self) -> ShimHostBridge:
        """取宿主能力面；未安装时抛 ``RuntimeError``（这是编码错误，不是能力缺失）。"""
        with self._lock:
            host = self._host
        if host is None:
            raise RuntimeError(
                "AstrBot compatibility shim is not attached to a host; "
                "the shim must be loaded through bot_bridge.",
            )
        return host

    def log(self, level: str, message: str, *, plugin_id: str = "", source: str = "") -> None:
        """写日志；宿主未安装时退回 ``logging.getLogger("astrbot")``（不抛异常）。"""
        with self._lock:
            host = self._host
        if host is not None:
            host.log(level, message, plugin_id=plugin_id, source=source)
            return
        logging.getLogger("astrbot").log(_LEVELS.get(level, logging.INFO), message)

    # -- 插件登记 ---------------------------------------------------------- #

    def bind_plugin(self, native_plugin_id: str, bridge_plugin_id: str, data_dir: Path) -> None:
        """登记插件的数据目录（key 用 AstrBot 原生 ``author/name`` id）。"""
        with self._lock:
            self._plugin_dirs[native_plugin_id] = Path(data_dir)
            self._plugin_keys[native_plugin_id] = bridge_plugin_id

    def unbind_plugin(self, native_plugin_id: str) -> None:
        with self._lock:
            self._plugin_dirs.pop(native_plugin_id, None)
            self._plugin_keys.pop(native_plugin_id, None)
            self._unsupported.pop(native_plugin_id, None)

    def plugin_data_dir(self, native_plugin_id: str) -> Path | None:
        with self._lock:
            return self._plugin_dirs.get(native_plugin_id)

    def bridge_plugin_id(self, native_plugin_id: str) -> str:
        with self._lock:
            return self._plugin_keys.get(native_plugin_id, native_plugin_id)

    # -- 未映射能力登记 ---------------------------------------------------- #

    def register_unsupported_sink(self, native_plugin_id: str) -> list[str]:
        """返回一个**原地可变**的列表，兼容层把它挂到 ``LoadedPlugin.native`` 上。

        这样 ``bridge_status`` 永远看到的是实时值，而不用刷新。
        """
        with self._lock:
            return self._unsupported.setdefault(native_plugin_id, [])

    def record_unsupported(self, native_plugin_id: str, capability: str) -> None:
        """登记一次未映射能力调用（去重，保持出现顺序）。"""
        if not native_plugin_id or not capability:
            return
        with self._lock:
            bucket = self._unsupported.setdefault(native_plugin_id, [])
            if capability not in bucket:
                bucket.append(capability)

    def unsupported_calls(self, native_plugin_id: str) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._unsupported.get(native_plugin_id, ()))


RUNTIME = _Runtime()

__all__ = [
    "RUNTIME",
    "ShimHostBridge",
    "UnsupportedCapabilityError",
]
