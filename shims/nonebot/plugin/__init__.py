"""插件管理（上游 ``nonebot.plugin`` 的子集）。

覆盖：``PluginMetadata`` / ``Plugin`` / ``get_plugin`` / ``get_plugin_by_module_name`` /
``get_loaded_plugins`` / ``require`` / ``get_plugin_config``，并透传 ``on_*`` 系列。

未实现（明确报错，绝不假装成功）：``load_plugin`` / ``load_plugins`` / ``load_all_plugins`` /
``load_from_json`` / ``load_from_toml`` / 内置插件加载 —— 桥接层自己负责把插件目录导入进程，
插件运行期动态加载另一个插件在 N.E.K.O. 里没有等价路径。
"""

from __future__ import annotations

from types import ModuleType

from nonebot import _runtime
from nonebot.log import logger
from nonebot.plugin.model import Plugin as Plugin
from nonebot.plugin.model import PluginMetadata as PluginMetadata
from nonebot.plugin.on import CommandGroup as CommandGroup
from nonebot.plugin.on import MatcherGroup as MatcherGroup
from nonebot.plugin.on import get_matcher_source as get_matcher_source
from nonebot.plugin.on import on as on
from nonebot.plugin.on import on_command as on_command
from nonebot.plugin.on import on_endswith as on_endswith
from nonebot.plugin.on import on_fullmatch as on_fullmatch
from nonebot.plugin.on import on_keyword as on_keyword
from nonebot.plugin.on import on_message as on_message
from nonebot.plugin.on import on_metaevent as on_metaevent
from nonebot.plugin.on import on_notice as on_notice
from nonebot.plugin.on import on_regex as on_regex
from nonebot.plugin.on import on_request as on_request
from nonebot.plugin.on import on_shell_command as on_shell_command
from nonebot.plugin.on import on_startswith as on_startswith
from nonebot.plugin.on import on_type as on_type

__all__ = [
    "CommandGroup",
    "MatcherGroup",
    "Plugin",
    "PluginMetadata",
    "get_loaded_plugins",
    "get_plugin",
    "get_plugin_by_module_name",
    "get_plugin_config",
    "load_all_plugins",
    "load_plugin",
    "load_plugins",
    "on",
    "on_command",
    "on_endswith",
    "on_fullmatch",
    "on_keyword",
    "on_message",
    "on_metaevent",
    "on_notice",
    "on_regex",
    "on_request",
    "on_shell_command",
    "on_startswith",
    "on_type",
    "require",
]


def get_plugin(plugin_id: str) -> Plugin | None:
    """获取已经导入的某个插件。"""
    record = _runtime.plugin_record(plugin_id)
    if record is None or record.module is None:
        return None
    return _to_plugin(record)


def get_plugin_by_module_name(module_name: str) -> Plugin | None:
    """通过模块名获取已导入的某个插件。"""
    plugin_id = _runtime.plugin_id_for_module(module_name)
    return get_plugin(plugin_id) if plugin_id else None


def get_loaded_plugins() -> set[Plugin]:
    """获取所有已加载的插件。"""
    result: set[Plugin] = set()
    for plugin_id, record in _runtime.all_plugins().items():
        if record.module is not None:
            result.add(_to_plugin(record))
    return result


def _to_plugin(record: _runtime.PluginRecord) -> Plugin:
    plugin = Plugin(
        name=record.plugin_id,
        module=record.module,  # type: ignore[arg-type]
        module_name=record.module_name,
        manager=record,
        matcher=set(record.matchers),
        metadata=record.metadata,  # type: ignore[arg-type]
    )
    return plugin


def require(name: str) -> ModuleType:
    """声明依赖插件（桥接层只能解析本桥内已加载的插件）。"""
    module = _runtime.resolve_plugin_module(name)
    if module is None:
        raise RuntimeError(f'Cannot load plugin "{name}"!')
    return module


def get_plugin_config(model: object) -> object:
    """获取插件配置。

    shim 的行为：用桥接层注入的配置快照实例化 ``model``；
    快照缺失时退回模型默认值并打印一条警告（绝不假装读到了用户配置）。
    """
    snapshot = _runtime.plugin_config_snapshot()
    if not callable(model):
        raise TypeError(f"plugin config model {model!r} is not callable")
    if not snapshot:
        logger.warning(
            "plugin config snapshot is empty; falling back to model defaults "
            "(the bridge could not read the user config synchronously)"
        )
        return model()
    try:
        return model(**snapshot)
    except TypeError as exc:
        logger.warning(f"plugin config snapshot does not match the model ({exc!r}); using defaults")
        return model()


def _unsupported_dynamic_loading(name: str) -> None:
    raise RuntimeError(
        f"{name}() is not supported inside the N.E.K.O. bridge: "
        "declare the plugin as a bridge plugin instead of loading it at runtime"
    )


def load_plugin(module_path: object) -> Plugin | None:
    """未实现：桥接层不支持运行期动态加载插件。"""
    _unsupported_dynamic_loading("load_plugin")
    return None


def load_plugins(*plugin_dir: str) -> set[Plugin]:
    """未实现：桥接层不支持运行期动态加载插件。"""
    _unsupported_dynamic_loading("load_plugins")
    return set()


def load_all_plugins(module_path: object, plugin_dir: object) -> set[Plugin]:
    """未实现：桥接层不支持运行期动态加载插件。"""
    _unsupported_dynamic_loading("load_all_plugins")
    return set()
