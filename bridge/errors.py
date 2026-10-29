"""bot_bridge 内部错误类型。

约定：所有错误都是结构化异常，禁止 ``except Exception: pass``。
外来插件调用失败必须向上抛，由桥接层转成结构化错误返回给调用方。
"""

from __future__ import annotations


class BridgeError(Exception):
    """桥接层错误的基类。"""

    code = "bridge_error"


class BackendUnavailableError(BridgeError):
    """某个框架的兼容层不可用（例如缺少运行时依赖）。"""

    code = "backend_unavailable"

    def __init__(self, framework: str, detail: str = "") -> None:
        super().__init__(f"framework backend '{framework}' is unavailable: {detail or 'unknown reason'}")
        self.framework = framework
        self.detail = detail


class PluginLoadError(BridgeError):
    """加载某个外来插件失败。"""

    code = "plugin_load_failed"

    def __init__(self, plugin_ref: str, detail: str = "") -> None:
        super().__init__(f"failed to load bot plugin '{plugin_ref}': {detail or 'unknown reason'}")
        self.plugin_ref = plugin_ref
        self.detail = detail


class PluginNotLoadedError(BridgeError):
    """调用了未加载的插件。"""

    code = "plugin_not_loaded"

    def __init__(self, plugin_id: str) -> None:
        super().__init__(f"bot plugin '{plugin_id}' is not loaded")
        self.plugin_id = plugin_id


class UnsupportedCapabilityError(BridgeError):
    """外来插件请求了一项 N.E.K.O 侧没有等价实现的能力。

    这是个诚实错误：绝不假装成功。调用方（外来插件）会收到结构化的失败结果。
    """

    code = "unsupported_capability"

    def __init__(self, capability: str, detail: str = "") -> None:
        super().__init__(f"capability '{capability}' has no N.E.K.O equivalent: {detail or 'not mapped'}")
        self.capability = capability
        self.detail = detail


class CommandTimeoutError(BridgeError):
    """外来插件命令/工具调用超时。"""

    code = "command_timeout"

    def __init__(self, target: str, timeout: float) -> None:
        super().__init__(f"bot plugin call '{target}' timed out after {timeout:.1f}s")
        self.target = target
        self.timeout = timeout


__all__ = [
    "BackendUnavailableError",
    "BridgeError",
    "CommandTimeoutError",
    "PluginLoadError",
    "PluginNotLoadedError",
    "UnsupportedCapabilityError",
]
