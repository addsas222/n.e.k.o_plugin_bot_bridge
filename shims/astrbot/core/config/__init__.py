"""AstrBot 插件配置对象（N.E.K.O. shim 子集）。

``AstrBotConfig`` 是 ``dict`` 子类，支持点号访问根配置项（与上游一致）。差异：

* 落盘走 N.E.K.O. 的 ``save_plugin_config``：``save_config_async()`` 直接 await；
  ``save_config()``（同步）在当前事件循环里排一个后台任务，没有运行中的循环时抛
  :class:`astrbot.UnsupportedCapabilityError`（不假装保存成功）。
* 配置项默认值由 ``_conf_schema.json`` 推导（``schema_to_default_config``），与上游
  的 ``_config_schema_to_default_config`` 行为一致。
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from ..._runtime import RUNTIME, UnsupportedCapabilityError

DEFAULT_VALUE_MAP: dict[str, Any] = {
    "int": 0,
    "float": 0.0,
    "bool": False,
    "string": "",
    "text": "",
    "list": [],
    "file": [],
    "object": {},
    "template_list": [],
    "dict": {},
}
"""schema 类型 → 缺少 ``default`` 时的默认值（与上游 ``astrbot.core.config.default`` 一致）。"""


def schema_to_default_config(schema: Mapping[str, Any]) -> dict[str, Any]:
    """把 ``_conf_schema.json`` 的 schema 转成默认配置字典。

    Raises:
        TypeError: schema 里出现了上游不支持的配置类型。
    """
    config: dict[str, Any] = {}
    _parse_schema(schema, config)
    return config


def _parse_schema(schema: Mapping[str, Any], config: dict[str, Any]) -> None:
    for key, item in schema.items():
        if not isinstance(item, Mapping):
            raise TypeError(f"配置项 {key!r} 的 schema 必须是对象。")
        item_type = item.get("type")
        if item_type not in DEFAULT_VALUE_MAP:
            raise TypeError(
                f"不受支持的配置类型 {item_type!r}。支持的类型有：{list(DEFAULT_VALUE_MAP)}",
            )
        default = item["default"] if "default" in item else DEFAULT_VALUE_MAP[item_type]
        if item_type == "object":
            config[key] = {}
            items = item.get("items")
            if not isinstance(items, Mapping):
                raise TypeError(f"配置项 {key!r} 是 object，但缺少 items schema。")
            _parse_schema(items, config[key])
        else:
            config[key] = default


class AstrBotConfig(dict):
    """插件配置（dict 子类，支持 ``config.key`` 访问）。"""

    def __init__(
        self,
        config_path: str = "",
        default_config: Mapping[str, Any] | None = None,
        schema: Mapping[str, Any] | None = None,
        *,
        host: Any = None,
        plugin_id: str = "",
    ) -> None:
        data = (
            schema_to_default_config(schema)
            if schema is not None
            else dict(default_config or {})
        )
        super().__init__(data)
        object.__setattr__(self, "_neko_config_path", config_path)
        object.__setattr__(self, "_neko_host", host)
        object.__setattr__(self, "_neko_plugin_id", plugin_id)
        object.__setattr__(self, "_neko_schema", schema)

    # -- 与上游一致的访问语义 --------------------------------------------- #

    def __getattr__(self, item: str) -> Any:
        return self.get(item)

    def __setattr__(self, key: str, value: Any) -> None:
        self[key] = value

    def __delattr__(self, key: str) -> None:
        try:
            del self[key]
        except KeyError as exc:
            raise AttributeError(f"没有找到 Key: '{key}'") from exc

    @property
    def config_path(self) -> str:
        """上游记录配置文件路径；这里记录桥接层的逻辑标识（可为空）。"""
        return str(self._neko_config_path)

    @property
    def schema(self) -> Mapping[str, Any] | None:
        """该插件 ``_conf_schema.json`` 的内容。"""
        return self._neko_schema

    def check_exist(self) -> bool:
        """配置是否存在（桥接层没有独立配置文件，固定为 True）。"""
        return True

    # -- 落盘 -------------------------------------------------------------- #

    def save_config(
        self,
        replace_config: Mapping[str, Any] | None = None,
        *,
        indent: int = 2,
    ) -> None:
        """保存配置。

        同步接口：在正在运行的事件循环里排一个后台任务；没有运行中的循环时抛
        :class:`astrbot.UnsupportedCapabilityError`。
        """
        del indent  # N.E.K.O. 侧由宿主决定序列化格式
        if replace_config:
            self.update(replace_config)
        host = self._neko_host
        if host is None:
            raise UnsupportedCapabilityError(
                "config.save",
                "该配置对象未绑定宿主，无法写回",
            )
        try:
            asyncio.get_running_loop()
        except RuntimeError as exc:
            raise UnsupportedCapabilityError(
                "config.save",
                "当前没有运行中的事件循环；请改用 save_config_async()",
            ) from exc
        task = asyncio.ensure_future(host.save_plugin_config(self._neko_plugin_id, dict(self)))
        task.add_done_callback(_log_save_failure)

    async def save_config_async(
        self,
        replace_config: Mapping[str, Any] | None = None,
        *,
        indent: int = 2,
    ) -> bool:
        """异步保存配置，返回宿主是否写入成功。"""
        del indent
        if replace_config:
            self.update(replace_config)
        host = self._neko_host
        if host is None:
            raise UnsupportedCapabilityError(
                "config.save",
                "该配置对象未绑定宿主，无法写回",
            )
        return bool(await host.save_plugin_config(self._neko_plugin_id, dict(self)))


def _log_save_failure(task: asyncio.Future[bool]) -> None:
    """后台保存任务失败时写一条宿主日志（同步接口无法把异常抛回调用方）。"""
    if task.cancelled():
        return
    error = task.exception()
    if error is not None:
        RUNTIME.log(
            "error",
            f"asynchronous config.save failed: {error!r}",
            source="astrbot.config",
        )


__all__ = ["DEFAULT_VALUE_MAP", "AstrBotConfig", "schema_to_default_config"]
