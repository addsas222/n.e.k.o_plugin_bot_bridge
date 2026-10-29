"""兼容桥的冻结契约。

三层结构：

* :class:`HostServices` —— 桥接层向**外来插件**暴露的 N.E.K.O 能力面
  （发送消息、大模型、存储、配置、插件互调、消息查询）。
* :class:`FrameworkBackend` —— 一个机器人框架的兼容层：识别目录、加载插件、
  启停插件。MaiBot / NoneBot2 / AstrBot / Koishi 各实现一份。
* :class:`LoadedPlugin` —— 加载完成后的统一句柄。兼容层必须把框架原生的
  命令 / 工具 / 事件整理成 :class:`CommandSpec` / :class:`ToolSpec` /
  :class:`EventSpec` / :class:`ApiSpec`，并把调用能力绑定成下面的回调。

桥接核心（registry / router / 适配器入口）只依赖本模块，不 import 任何框架。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


def framework_dirs_from_settings(settings: Mapping[str, Any]) -> dict[str, Path]:
    """解析 ``[bridge.framework_dirs]``：``框架名 -> 该框架自己的插件源目录``。

    只做格式规范化（字符串非空、``~`` 展开）；目录是否存在、框架是否启用由调用方
    决定（registry 要跳过不存在的，plugin_admin 只用它生成更准确的拒绝信息）。
    放在契约层是因为 registry 与 plugin_admin 都要用它，而后者不能 import registry
    （registry 才 import plugin_admin，会成环）。
    """
    raw = settings.get("framework_dirs")
    if not isinstance(raw, Mapping):
        return {}
    result: dict[str, Path] = {}
    for name, value in raw.items():
        text = str(value or "").strip()
        if text:
            result[str(name)] = Path(text).expanduser()
    return result

# --------------------------------------------------------------------------- #
# 插件清单 / 能力描述
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class PluginManifest:
    """一个外来插件的身份信息（由兼容层从框架原生清单里读出）。"""

    path: Path
    plugin_id: str
    framework: str
    name: str = ""
    version: str = ""
    description: str = ""
    entry: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.plugin_id


@dataclass(slots=True)
class CommandSpec:
    """外来插件的一条文本命令。

    ``patterns`` 是框架原生的触发形式（正则或前缀），仅用于展示与适配器侧
    的自检；真正的匹配逻辑由兼容层负责。
    """

    id: str
    name: str = ""
    description: str = ""
    patterns: tuple[str, ...] = ()
    usage: str = ""
    aliases: tuple[str, ...] = ()
    priority: int = 0
    hidden: bool = False
    parameters: dict[str, Any] | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.id


@dataclass(slots=True)
class ToolSpec:
    """外来插件注册的 LLM 工具。"""

    name: str
    description: str = ""
    parameters: dict[str, Any] | None = None
    timeout: float = 30.0
    origin: str = ""


@dataclass(slots=True)
class EventSpec:
    """外来插件订阅的事件处理器。"""

    event_type: str
    description: str = ""
    blocking: bool = False
    priority: int = 0


@dataclass(slots=True)
class ApiSpec:
    """外来插件对外暴露的 API（供其它插件互调）。"""

    name: str
    description: str = ""
    version: str = ""
    parameters: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# 消息 / 调用上下文
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CommandContext:
    """一次入站消息的上下文。"""

    text: str
    user_id: str = ""
    user_name: str = ""
    group_id: str = ""
    channel: str = "neko"
    source: str = "bot_bridge"
    is_group: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CommandResult:
    """一次入站消息的处理结果。"""

    handled: bool = False
    plugin_id: str = ""
    framework: str = ""
    command: str = ""
    replies: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_payload(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "plugin_id": self.plugin_id,
            "framework": self.framework,
            "command": self.command,
            "replies": list(self.replies),
            "data": dict(self.data),
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# N.E.K.O 侧能力面
# --------------------------------------------------------------------------- #


@runtime_checkable
class HostServices(Protocol):
    """桥接层提供给外来插件的 N.E.K.O 能力。

    实现见 ``bridge/host.py``。兼容层只依赖这个 Protocol，方便离线测试。
    """

    def log(self, level: str, message: str, *, source: str = "") -> None:
        """写一条日志。``level`` 取 ``debug`` / ``info`` / ``warning`` / ``error``。"""
        ...

    def data_dir(self, plugin_id: str) -> Path:
        """该外来插件的持久数据目录（自动创建）。"""
        ...

    def cache_dir(self, plugin_id: str) -> Path:
        """该外来插件的缓存目录（自动创建）。"""
        ...

    @property
    def settings(self) -> Mapping[str, Any]:
        """桥接配置（``[bridge]`` 段）。"""
        ...

    async def send_chat(
        self,
        text: str,
        *,
        source: str,
        visibility: Sequence[str] | None = None,
        ai_behavior: str = "",
        priority: int = 5,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        """把外来插件产生的一段文本送进 N.E.K.O 对话。"""
        ...

    async def llm_generate(
        self,
        prompt: str | Sequence[Mapping[str, Any]],
        *,
        model: str = "",
        system: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """调用 N.E.K.O 的大模型。

        返回统一字典：``{"success": bool, "response": str, "reasoning": str,
        "model": str, "tool_calls": list, "error": str}``。
        """
        ...

    async def llm_embed(self, texts: Sequence[str]) -> list[list[float]]:
        """文本向量化。不可用时抛 :class:`UnsupportedCapabilityError`。"""
        ...

    async def call_plugin_entry(
        self,
        plugin_id: str,
        entry_id: str,
        params: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        """调用另一个 N.E.K.O 插件的入口。"""
        ...

    async def list_plugins(self) -> list[dict[str, Any]]:
        """列出 N.E.K.O 已加载插件。"""
        ...

    async def recent_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        """最近的对话消息（来自 N.E.K.O 总线消息快照）。"""
        ...

    async def plugin_config(self, plugin_id: str) -> dict[str, Any]:
        """读取该外来插件的用户配置（TOML 字典）。"""
        ...

    async def save_plugin_config(self, plugin_id: str, data: Mapping[str, Any]) -> bool:
        """写回该外来插件的用户配置。"""
        ...


# --------------------------------------------------------------------------- #
# 兼容层
# --------------------------------------------------------------------------- #

MessageHandler = Callable[[CommandContext], Awaitable["CommandResult"]]
ToolHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]
ApiHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]
EventHandler = Callable[[str, dict[str, Any]], Awaitable[Any]]


@dataclass(slots=True)
class LoadedPlugin:
    """加载完成的外来插件句柄。

    兼容层负责填写清单、能力描述与四个回调；桥接核心只按这些字段工作。
    """

    manifest: PluginManifest
    instance: object | None = None
    commands: list[CommandSpec] = field(default_factory=list)
    tools: list[ToolSpec] = field(default_factory=list)
    events: list[EventSpec] = field(default_factory=list)
    apis: list[ApiSpec] = field(default_factory=list)
    handle_message: MessageHandler | None = None
    call_tool: ToolHandler | None = None
    call_api: ApiHandler | None = None
    dispatch_event: EventHandler | None = None
    native: object | None = None
    started: bool = False
    loaded_at: float = 0.0

    @property
    def plugin_id(self) -> str:
        return self.manifest.plugin_id

    @property
    def framework(self) -> str:
        return self.manifest.framework

    def summary(self) -> dict[str, Any]:
        return {
            # 跨框架同名插件靠这个键区分；面板/模型调用启停、删除都用它。
            "key": f"{self.framework}:{self.plugin_id}",
            "plugin_id": self.plugin_id,
            "framework": self.framework,
            "name": self.manifest.display_name,
            "version": self.manifest.version,
            "path": str(self.manifest.path),
            "started": self.started,
            "commands": [item.id for item in self.commands],
            "tools": [item.name for item in self.tools],
            "events": [item.event_type for item in self.events],
            "apis": [item.name for item in self.apis],
        }


@runtime_checkable
class FrameworkBackend(Protocol):
    """一个机器人框架的兼容层。"""

    name: str
    display_name: str
    requires: tuple[str, ...]

    def detect(self, path: Path) -> PluginManifest | None:
        """判断 ``path`` 是不是本框架的插件目录；不是则返回 ``None``。"""
        ...

    def load(self, manifest: PluginManifest, host: HostServices) -> LoadedPlugin:
        """导入并实例化插件（同步）。

        失败必须抛 :class:`PluginLoadError` 或 :class:`BackendUnavailableError`。
        """
        ...

    async def start(self, plugin: LoadedPlugin) -> None:
        """执行插件的启动生命周期（on_load / startup hook）。"""
        ...

    async def stop(self, plugin: LoadedPlugin) -> None:
        """执行插件的停止生命周期；必须容忍插件已经半启动。"""
        ...


__all__ = [
    "ApiHandler",
    "ApiSpec",
    "CommandContext",
    "CommandResult",
    "CommandSpec",
    "EventSpec",
    "EventHandler",
    "FrameworkBackend",
    "HostServices",
    "LoadedPlugin",
    "MessageHandler",
    "PluginManifest",
    "ToolHandler",
    "ToolSpec",
]
