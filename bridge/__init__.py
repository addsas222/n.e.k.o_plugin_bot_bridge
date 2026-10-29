"""bot_bridge 内部实现包。

对外只暴露四样东西：契约、错误、宿主能力层、注册表与路由。
外来框架的兼容层在 :mod:`bridge.backends` 下，各占一个模块。
"""

from __future__ import annotations

from .contracts import (
    ApiSpec,
    CommandContext,
    CommandResult,
    CommandSpec,
    EventSpec,
    FrameworkBackend,
    HostServices,
    LoadedPlugin,
    PluginManifest,
    ToolSpec,
)
from .errors import (
    BackendUnavailableError,
    BridgeError,
    CommandTimeoutError,
    PluginLoadError,
    PluginNotLoadedError,
    UnsupportedCapabilityError,
)
from .host import NEKOHostServices
from .registry import BridgeRegistry, plugin_key
from .router import BridgeRouter

__all__ = [
    "ApiSpec",
    "BackendUnavailableError",
    "BridgeError",
    "BridgeRegistry",
    "BridgeRouter",
    "CommandContext",
    "CommandResult",
    "CommandSpec",
    "CommandTimeoutError",
    "EventSpec",
    "FrameworkBackend",
    "HostServices",
    "LoadedPlugin",
    "NEKOHostServices",
    "PluginLoadError",
    "PluginManifest",
    "PluginNotLoadedError",
    "ToolSpec",
    "UnsupportedCapabilityError",
    "plugin_key",
]
