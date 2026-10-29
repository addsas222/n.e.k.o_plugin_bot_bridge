"""插件键值存储 Mixin（N.E.K.O. shim 子集）。

上游把 KV 存进 AstrBot 的共享偏好存储（``sp``，key 为原生 ``author/name``）。
shim 把它落到该插件的 N.E.K.O. 数据目录下的 ``plugin_kv.json``：

* key 用原生插件 id（与上游一致）；
* ``bytes`` 值以 ``{"__neko_bytes__": "<base64>"}`` 包装存储，读回时还原；
* 读写加同一把进程锁，并先写临时文件再 ``replace``，避免半截文件。
"""

from __future__ import annotations

import base64
import json
import threading
from pathlib import Path
from typing import Any, TypeVar

from ..._runtime import RUNTIME, UnsupportedCapabilityError

SUPPORTED_VALUE_TYPES = int | float | str | bytes | bool | dict | list | None
_VT = TypeVar("_VT")

_LOCK = threading.RLock()
_KV_FILENAME = "plugin_kv.json"
_BYTES_MARKER = "__neko_bytes__"


class PluginKVStoreMixin:
    """为插件提供键值存储能力。"""

    plugin_id: str

    async def put_kv_data(self, key: str, value: SUPPORTED_VALUE_TYPES) -> None:
        """写入一个键值对。"""
        path = _store_path(self.plugin_id)
        with _LOCK:
            data = _read(path)
            data[str(key)] = _encode(value)
            _write(path, data)

    async def get_kv_data(self, key: str, default: _VT) -> _VT | None:
        """读取一个键值对，不存在时返回 ``default``。"""
        path = _store_path(self.plugin_id)
        with _LOCK:
            data = _read(path)
        if str(key) not in data:
            return default
        return _decode(data[str(key)])

    async def delete_kv_data(self, key: str) -> None:
        """删除一个键值对。"""
        path = _store_path(self.plugin_id)
        with _LOCK:
            data = _read(path)
            data.pop(str(key), None)
            _write(path, data)


def _store_path(native_plugin_id: str) -> Path:
    data_dir = RUNTIME.plugin_data_dir(native_plugin_id)
    if data_dir is None:
        raise UnsupportedCapabilityError(
            "kv_store",
            f"插件 {native_plugin_id!r} 没有绑定数据目录，无法使用键值存储",
        )
    check = getattr(RUNTIME.host(), "capability_enabled", None) if RUNTIME.has_host() else None
    if callable(check) and not check("storage"):
        raise UnsupportedCapabilityError("kv_store", "已在 [bridge.capabilities].storage 关闭")
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / _KV_FILENAME


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # 文件损坏时不能假装读到空数据：抛错让插件/宿主知道存储坏了。
        raise UnsupportedCapabilityError(
            "kv_store",
            f"键值存储文件损坏（{path}）：{exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise UnsupportedCapabilityError("kv_store", f"键值存储文件格式错误（{path}）")
    return raw


def _write(path: Path, data: dict[str, Any]) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _encode(value: Any) -> Any:
    if isinstance(value, bytes):
        return {_BYTES_MARKER: base64.b64encode(value).decode("ascii")}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict) and set(value) == {_BYTES_MARKER}:
        return base64.b64decode(str(value[_BYTES_MARKER]))
    return value


__all__ = ["SUPPORTED_VALUE_TYPES", "PluginKVStoreMixin"]
