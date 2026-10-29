"""``hoshino.config`` 的兼容实现：**只读**配置快照。

上游的 ``hoshino/config/`` 是一个包，``__init__.py`` 会按需 import ``<模块名>.py``，
于是插件里到处是 ``hoshino.config.SUPERUSERS`` / ``hoshino.config.RES_DIR`` /
``hoshino.config.hourcall.HOUR_CALLS`` / ``hoshino.config.groupmaster.join_approve``。

N.E.K.O. 侧没有"用户自己写 Python 配置文件"这回事，所以这里提供的是**桥接配置的快照**：

* 桥接配置（``[bridge]`` 段）里的 ``hoshino_*`` 键会映射进快照（``SUPERUSERS`` /
  ``COMMAND_START`` / ``NICKNAME`` / ``RES_DIR`` / ``BLACK_LIST`` / ``WHITE_LIST`` /
  ``DEBUG`` / ``MODULES_ON`` …），另外 ``hoshino_config`` 这个 dict 可以直接覆盖任意键；
* 模块级配置（``hoshino.config.<模块>.<键>``）没有等价物：取值会抛
  :class:`UnsupportedCapabilityError`（见 :meth:`MissingConfigModule`），而不是给个空壳；
* :func:`plugin_config`（shim 扩展）是**异步**读 ``HostServices.plugin_config`` 的入口 ——
  插件想读"N.E.K.O. 面板里配的这一项"时用它。
"""

from __future__ import annotations

from typing import Any

from aiocqhttp._runtime import RUNTIME, UnsupportedCapabilityError

#: 快照里一定有这些键（值来自桥接配置或下面的默认值）。
DEFAULTS: dict[str, Any] = {
    "DEBUG": True,
    "SUPERUSERS": (),
    "NICKNAME": (),
    "COMMAND_START": ("/",),
    "RES_DIR": "",
    "RES_URL": "",
    "RES_PROTOCOL": "file",
    "BLACK_LIST": (),
    "WHITE_LIST": (),
    "MODULES_ON": (),
    "GROUP_BROADCAST": {},
}
"""上游 ``hoshino/config/__bot__.py`` 里最常用的键（值由兼容层从桥接配置填）。"""

_MISSING_MODULE_HINT = (
    "hoshino.config 在 N.E.K.O. 桥接里是只读快照，没有「按模块 import Python 配置」这回事；"
    "请在桥接配置的 [hoshino_config] 段里提供这个键，或用 hoshino.config.plugin_config() 读面板配置"
)


class _MissingConfigModule:
    """占位：``hoshino.config.<模块名>`` 在快照里不存在时的对象，取值即抛能力缺失。"""

    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, key: str) -> Any:
        if key.startswith("_"):
            raise AttributeError(key)
        raise UnsupportedCapabilityError(
            f"hoshino.config.{self._name}.{key}",
            _MISSING_MODULE_HINT,
        )

    def __repr__(self) -> str:
        return f"<missing hoshino config module {self._name!r}>"


def get_group_broadcast(group_id: Any) -> Any:
    """上游的「这个群的播报设置」 —— N.E.K.O. 侧没有分组配置，显式不支持。"""
    raise UnsupportedCapabilityError(
        "hoshino.config.get_group_broadcast",
        "N.E.K.O. 的对话没有「群」这个维度：一条对话就是一个上下文，没有分群配置可读",
    )


def get_group_notice(group_id: Any = None) -> Any:
    """上游的"群公告"读取 —— 同上，显式不支持。"""
    raise UnsupportedCapabilityError(
        "hoshino.config.get_group_notice",
        "N.E.K.O. 没有群公告接口",
    )


async def plugin_config(plugin_id: str = "") -> dict[str, Any]:
    """读该插件在 N.E.K.O. 侧的配置（``HostServices.plugin_config``）。

    这是 shim 扩展（上游没有）：``hoshino`` 原生的 ``load_config`` 是同步读 JSON 文件的，
    而 N.E.K.O. 的面板配置只能异步取，所以单独给一个异步入口。
    """
    from hoshino._runtime import current_plugin_id

    target = plugin_id or current_plugin_id()
    host = RUNTIME.host_or_none()
    if host is None or not target:
        raise UnsupportedCapabilityError(
            "hoshino.config.plugin_config",
            "没有绑定宿主或插件上下文；这个入口只能在被 bot_bridge 加载的插件里用",
        )
    return dict(await host.load_plugin_config(target))


async def save_plugin_config(data: dict[str, Any], plugin_id: str = "") -> bool:
    """写回该插件在 N.E.K.O. 侧的配置（shim 扩展，同上）。"""
    from hoshino._runtime import current_plugin_id

    target = plugin_id or current_plugin_id()
    host = RUNTIME.host_or_none()
    if host is None or not target:
        raise UnsupportedCapabilityError(
            "hoshino.config.save_plugin_config",
            "没有绑定宿主或插件上下文；这个入口只能在被 bot_bridge 加载的插件里用",
        )
    return bool(await host.save_plugin_config(target, dict(data)))


def snapshot() -> dict[str, Any]:
    """当前快照（只读副本）。"""
    return _snapshot()


def __getattr__(name: str) -> Any:
    """模块级兜底：``hoshino.config.WHATEVER``。

    快照里有就返回；没有则：常见模块名（``groupmaster`` / ``hourcall`` / ``priconne`` …）
    返回一个取值即抛 :class:`UnsupportedCapabilityError` 的占位对象，其它名字按正常
    Python 语义抛 ``AttributeError``（带提示）。
    """
    values = _snapshot()
    if name in values:
        return values[name]
    if name in _MODULE_CONFIG_NAMES:
        return _MissingConfigModule(name)
    raise AttributeError(f"module 'hoshino.config' has no attribute {name!r}; {_MISSING_MODULE_HINT}")


#: 上游常见配置模块名（不区分大小写匹配调用方写的那几个）。
_MODULE_CONFIG_NAMES = frozenset(
    {
        "deepchat",
        "groupmaster",
        "hourcall",
        "mikan",
        "pcrclanbattle",
        "picfinder",
        "priconne",
        "twitter",
    }
)


def _snapshot() -> dict[str, Any]:
    from hoshino._runtime import config_snapshot

    return config_snapshot()


__all__ = [
    "DEFAULTS",
    "get_group_broadcast",
    "get_group_notice",
    "plugin_config",
    "save_plugin_config",
    "snapshot",
]
