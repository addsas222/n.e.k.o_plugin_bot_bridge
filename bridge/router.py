"""把外来插件的能力投影成 N.E.K.O. 的入口与对话期工具，并负责入站分发。

四个投影面：

* **对话期工具**（``register_llm_tool``）：猫娘在对话里可以直接调用的工具。
  固定三个桥接工具 + 逐个外来命令/工具。
* **插件入口**（``register_dynamic_entry``）：出现在插件管理器入口列表里，可手动触发。
* **入站分发**：一条消息交给外来插件匹配命令 / 触发事件处理器。
* **出站**：外来插件自己的 ``send`` 能力走 :class:`HostServices.send_chat`。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .contracts import CommandContext, CommandResult, CommandSpec, LoadedPlugin, ToolSpec
from .errors import BridgeError, CommandTimeoutError, PluginNotLoadedError

_NAME_SAFE = re.compile(r"[^A-Za-z0-9_.\-]")
_ENTRY_SAFE = re.compile(r"[^A-Za-z0-9_-]")

#: 对话期工具名的硬约束（见 plugin/sdk/plugin/llm_tool.py）。
TOOL_NAME_MAX = 64
#: 一次最多投影多少个外来工具，防止把模型工具列表灌爆。
DEFAULT_MAX_TOOLS = 40

#: 兜底的单次外来调用超时（秒）。必须**小于宿主给入口的预算**（真机实测 30s），
#: 否则超时会先被宿主砍成一次「跑失败」，我们就没机会返回结构化错误。
#: 真实取值来自 ``[bridge].call_timeout``（默认 25.0，见 config.example.toml）。
_DEFAULT_CALL_TIMEOUT = 25.0

#: 已经提醒过 clamp 的宿主（每个宿主一次，别刷屏）。
_CLAMP_WARNED: set[int] = set()


def _resolve_call_timeout(host: object) -> float:
    """取单次外来调用的超时，并**收敛到宿主入口预算以内**。

    宿主给入口的预算是 30s（真机实测；见 README 的入口超时表），所以配置里写 60 只会让
    外来命令在宿主侧被砍成一次「跑失败」——这里就地 clamp 到 25s 并提醒一次。
    """
    settings = getattr(host, "settings", None) or {}
    try:
        configured = float(settings.get("call_timeout", _DEFAULT_CALL_TIMEOUT) or _DEFAULT_CALL_TIMEOUT)
    except (TypeError, ValueError):
        configured = _DEFAULT_CALL_TIMEOUT
    if configured <= _DEFAULT_CALL_TIMEOUT:
        return configured
    key = id(host)
    if key not in _CLAMP_WARNED:
        _CLAMP_WARNED.add(key)
        log = getattr(host, "log", None)
        if callable(log):
            log(
                "warning",
                f"call_timeout={configured:g}s 超过宿主入口预算（≈30s），已按 {_DEFAULT_CALL_TIMEOUT:g}s 执行；"
                "要改请改 [bridge].call_timeout，或把长任务拆成多次调用",
                source="router",
            )
    return _DEFAULT_CALL_TIMEOUT


def sanitize_tool_name(raw: str, *, prefix: str = "bot_") -> str:
    """把任意名字压成宿主接受的工具名（``[A-Za-z0-9_.\\-]{1,64}``）。"""
    body = _NAME_SAFE.sub("_", str(raw or "").strip())
    body = body.strip("_.-") or "unnamed"
    room = max(1, TOOL_NAME_MAX - len(prefix))
    if len(body) > room:
        digest = hashlib.sha1(body.encode("utf-8")).hexdigest()[:6]
        body = f"{body[: room - 7]}_{digest}"
    return f"{prefix}{body}"


def sanitize_entry_id(raw: str, *, prefix: str = "bot_") -> str:
    """入口 id 只允许 ``[A-Za-z0-9_-]``。"""
    body = _ENTRY_SAFE.sub("_", str(raw or "").strip()).strip("_") or "unnamed"
    return f"{prefix}{body}"


class BridgeRouter:
    """N.E.K.O. 侧的能力投影与消息分发。"""

    def __init__(self, plugin: object, host: object, registry: object) -> None:
        self._plugin = plugin
        self._host = host
        self._registry = registry
        self._tools: dict[str, tuple[str, str, str]] = {}
        self._entries: dict[str, str] = {}

    # ------------------------------------------------------------------ 工具

    def _max_tools(self) -> int:
        settings = self._host.settings  # type: ignore[attr-defined]
        try:
            return max(0, int(settings.get("max_exposed_tools", DEFAULT_MAX_TOOLS) or DEFAULT_MAX_TOOLS))
        except (TypeError, ValueError):
            return DEFAULT_MAX_TOOLS

    def register_surfaces(self) -> dict[str, int]:
        """注册对话期工具与插件入口，返回统计。"""
        registered_tools = 0
        registered_entries = 0
        for key, tool_name, description, schema, handler in self._bridge_tools():
            if self._register_tool(tool_name, description, schema, handler):
                registered_tools += 1
        plugins = self._registry.plugins  # type: ignore[attr-defined]
        budget = self._max_tools()
        expose_commands = bool(self._host.settings.get("expose_commands_as_tools", True))  # type: ignore[attr-defined]
        expose_tools = bool(self._host.settings.get("expose_plugins_tools", True))  # type: ignore[attr-defined]
        for plugin_key, plugin in sorted(plugins.items()):
            if expose_commands:
                for spec in plugin.commands:
                    if spec.hidden:
                        continue
                    if budget <= 0:
                        break
                    # 名字里已经带了 `bot_` 前缀（见 _command_tool_name），这里不能再加一次，
                    # 否则会得到 `bot_bot_<框架>_…` 并与 README 的命名约定不符。
                    name = sanitize_tool_name(self._command_tool_name(plugin_key, spec), prefix="")
                    if name in self._tools:
                        continue
                    if self._register_tool(
                        name,
                        self._command_description(plugin, spec),
                        spec.parameters or _command_schema(spec),
                        self._make_command_handler(plugin_key, spec),
                    ):
                        registered_tools += 1
                        budget -= 1
                registered_entries += self._register_command_entries(plugin_key, plugin)
            if expose_tools:
                for spec in plugin.tools:
                    if budget <= 0:
                        break
                    name = sanitize_tool_name(self._tool_tool_name(plugin_key, spec), prefix="")
                    if name in self._tools:
                        continue
                    if self._register_tool(
                        name,
                        spec.description or f"{plugin.manifest.display_name} 的工具 {spec.name}",
                        spec.parameters or {"type": "object", "properties": {}},
                        self._make_tool_handler(plugin_key, spec),
                    ):
                        registered_tools += 1
                        budget -= 1
        self._host.log(  # type: ignore[attr-defined]
            "info",
            f"桥接面注册完成：对话期工具 {registered_tools} 个，插件入口 {registered_entries} 个",
            source="router",
        )
        return {"tools": registered_tools, "entries": registered_entries}

    def _bridge_tools(self) -> list[tuple[str, str, str, dict[str, Any], Any]]:
        text_schema = {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "要交给机器人插件处理的完整用户消息"},
                "plugin": {
                    "type": "string",
                    "description": "只让这个插件处理（插件键，形如 nonebot:hello_nonebot；留空 = 按加载顺序取第一个命中的）",
                },
            },
            "required": ["text"],
        }
        return [
            (
                "bridge_list_plugins",
                "bridge_list_plugins",
                "列出当前加载的机器人插件（MaiBot / NoneBot2 / AstrBot / Koishi 等）及其命令与可用工具。",
                {"type": "object", "properties": {}},
                self._handle_list_tool,
            ),
            (
                "bridge_run_command",
                "bridge_run_command",
                "让机器人插件（MaiBot / NoneBot2 / AstrBot / Koishi 等）处理一条消息：命中它们的命令就直接执行并把结果说给用户听。",
                text_schema,
                self._handle_run_tool,
            ),
            (
                "bridge_call_plugin_tool",
                "bridge_call_plugin_tool",
                "调用某个机器人插件注册的工具或命令。先可用 bridge_list_plugins 查看可用名字。",
                {
                    "type": "object",
                    "properties": {
                        "plugin": {"type": "string", "description": "插件键，形如 maibot:hello_maibot"},
                        "tool": {"type": "string", "description": "工具或命令名"},
                        "arguments": {
                            "type": "object",
                            "description": "传给工具的参数对象",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["plugin", "tool"],
                },
                self._handle_call_tool,
            ),
        ]

    def _register_tool(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        handler: Any,
    ) -> bool:
        register = getattr(self._plugin, "register_llm_tool", None)
        if not callable(register) or not name:
            return False
        try:
            ok = register(name=name, description=description or name, parameters=schema, handler=handler)
        except Exception as exc:  # noqa: BLE001 — 名字冲突等要记录下来
            self._host.log("warning", f"注册对话期工具 {name} 失败：{type(exc).__name__}: {exc}", source="router")  # type: ignore[attr-defined]
            return False
        if ok:
            self._tools[name] = (name, description, "")
        return bool(ok)

    def _register_command_entries(self, plugin_key: str, plugin: LoadedPlugin) -> int:
        register = getattr(self._plugin, "register_dynamic_entry", None)
        if not callable(register):
            return 0
        count = 0
        for spec in plugin.commands:
            if spec.hidden:
                continue
            entry_id = sanitize_entry_id(f"{plugin.framework}_{plugin.plugin_id}_{spec.id}", prefix="bot_cmd_")
            if entry_id in self._entries:
                continue
            handler = self._make_entry_handler(plugin_key, spec)
            try:
                ok = register(
                    entry_id=entry_id,
                    handler=handler,
                    name=f"{plugin.manifest.display_name} · {spec.display_name}",
                    description=spec.description or f"来自 {plugin.framework} 插件 {plugin.plugin_id} 的命令",
                    input_schema=spec.parameters or _command_schema(spec),
                    kind="action",
                    timeout=_resolve_call_timeout(self._host),  # type: ignore[attr-defined]
                    llm_result_fields=["summary", "reply", "handled"],
                )
            except Exception as exc:  # noqa: BLE001
                self._host.log("warning", f"注册入口 {entry_id} 失败：{type(exc).__name__}: {exc}", source="router")  # type: ignore[attr-defined]
                continue
            if ok:
                self._entries[entry_id] = plugin_key
                count += 1
        return count

    def unregister_surfaces(self) -> None:
        unregister_tool = getattr(self._plugin, "unregister_llm_tool", None)
        if callable(unregister_tool):
            for name in list(self._tools):
                try:
                    unregister_tool(name)
                except Exception as exc:  # noqa: BLE001 — 卸载失败只记录
                    self._host.log("debug", f"卸载工具 {name} 失败：{exc!r}", source="router")  # type: ignore[attr-defined]
        self._tools.clear()
        unregister_entry = getattr(self._plugin, "unregister_dynamic_entry", None)
        if callable(unregister_entry):
            for entry_id in list(self._entries):
                try:
                    unregister_entry(entry_id)
                except Exception as exc:  # noqa: BLE001
                    self._host.log("debug", f"卸载入口 {entry_id} 失败：{exc!r}", source="router")  # type: ignore[attr-defined]
        self._entries.clear()

    # -------------------------------------------------------------- 处理函数

    async def _handle_list_tool(self, **_kwargs: object) -> dict[str, Any]:
        return {"plugins": self.list_payload()}

    async def _handle_run_tool(
        self,
        text: str = "",
        plugin: str = "",
        **_kwargs: object,
    ) -> dict[str, Any]:
        context = CommandContext(text=str(text or ""), source="llm_tool", channel="neko")
        result = await self.handle_text(context, only=str(plugin or ""))
        return result.to_payload()

    async def _handle_call_tool(
        self,
        plugin: str = "",
        tool: str = "",
        arguments: Mapping[str, Any] | None = None,
        **_kwargs: object,
    ) -> dict[str, Any]:
        return await self.call_plugin_tool(str(plugin), str(tool), dict(arguments or {}))

    def _make_command_handler(self, plugin_key: str, spec: CommandSpec) -> Any:
        async def _handler(**_kwargs: object) -> dict[str, Any]:
            text = str(
                _kwargs.get("text")
                or _kwargs.get("message")
                or _kwargs.get("args")
                or _kwargs.get("command")
                or ""
            )
            if not text:
                text = spec.patterns[0] if spec.patterns else spec.id
            context = CommandContext(text=text, source="llm_tool", channel="neko")
            result = await self.handle_text(context, only=plugin_key)
            if not result.handled:
                return {"handled": False, "summary": f"{plugin_key} 没有匹配到命令 {spec.id}"}
            return result.to_payload() | {"summary": "\n".join(result.replies) or f"{spec.display_name} 已执行"}

        return _handler

    def _make_tool_handler(self, plugin_key: str, spec: ToolSpec) -> Any:
        async def _handler(**_kwargs: object) -> dict[str, Any]:
            arguments = dict(_kwargs)
            return await self.call_plugin_tool(plugin_key, spec.name, arguments)

        return _handler

    def _make_entry_handler(self, plugin_key: str, spec: CommandSpec) -> Any:
        async def _handler(**_kwargs: object) -> dict[str, Any]:
            text = str(_kwargs.get("text") or "")
            context = CommandContext(text=text or spec.id, source="plugin_entry", channel="neko")
            result = await self.handle_text(context, only=plugin_key)
            return result.to_payload() | {"summary": "\n".join(result.replies)}

        return _handler

    @staticmethod
    def _command_tool_name(plugin_key: str, spec: CommandSpec) -> str:
        framework, _, plugin_id = plugin_key.partition(":")
        return f"bot_{framework}_{plugin_id}_{spec.id}"

    @staticmethod
    def _tool_tool_name(plugin_key: str, spec: ToolSpec) -> str:
        framework, _, plugin_id = plugin_key.partition(":")
        return f"bot_{framework}_{plugin_id}_tool_{spec.name}"

    @staticmethod
    def _command_description(plugin: LoadedPlugin, spec: CommandSpec) -> str:
        parts = [f"[{plugin.framework}] {plugin.manifest.display_name}"]
        if spec.display_name:
            parts.append(spec.display_name)
        if spec.description:
            parts.append(spec.description)
        if spec.usage:
            parts.append(f"用法：{spec.usage}")
        return " · ".join(parts)

    # ------------------------------------------------------------------ 分发

    async def handle_text(self, context: CommandContext, *, only: str = "") -> CommandResult:
        """把一条消息交给外来插件处理；命中的第一个插件负责回复。

        事件处理器（``message.received``）始终先触发，让插件有机会做副作用。
        """
        await self.dispatch_events("message.received", _message_payload(context))
        timeout = _resolve_call_timeout(self._host)  # type: ignore[attr-defined]
        plugins = self._registry.plugins  # type: ignore[attr-defined]
        candidates: list[tuple[str, LoadedPlugin]] = sorted(
            ((key, plugin) for key, plugin in plugins.items()),
            key=lambda item: (-_specificity_of(item[1]), -_priority_of(item[1]), item[0]),
        )
        for key, plugin in candidates:
            if only and key != only and plugin.plugin_id != only:
                continue
            handler = plugin.handle_message
            if handler is None or not plugin.commands:
                continue
            try:
                result = await asyncio.wait_for(handler(context), timeout=timeout)
            except asyncio.TimeoutError:
                error = CommandTimeoutError(f"{key}", timeout)
                self._host.log("error", str(error), source="router")  # type: ignore[attr-defined]
                return CommandResult(handled=False, plugin_id=plugin.plugin_id, framework=plugin.framework, error=str(error))
            except BridgeError as exc:
                self._host.log("error", f"{key} 处理消息失败：{exc}", source="router")  # type: ignore[attr-defined]
                return CommandResult(
                    handled=False,
                    plugin_id=plugin.plugin_id,
                    framework=plugin.framework,
                    error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001 — 外来插件的异常不能掀翻桥接层
                detail = f"{type(exc).__name__}: {exc}"
                self._host.log("error", f"{key} 处理消息抛错：{detail}", source="router")  # type: ignore[attr-defined]
                return CommandResult(
                    handled=False,
                    plugin_id=plugin.plugin_id,
                    framework=plugin.framework,
                    error=detail,
                )
            if isinstance(result, CommandResult) and result.handled:
                if not result.framework:
                    result.framework = plugin.framework
                if not result.plugin_id:
                    result.plugin_id = plugin.plugin_id
                await self._speak(result)
                return result
        return CommandResult(handled=False, error="" if plugins else "no bot plugins loaded")

    async def _speak(self, result: CommandResult) -> None:
        """把命中的命令回复推进 N.E.K.O. 对话（外来插件自己 push 过的不会再推）。"""
        if not result.replies:
            return
        text = "\n".join(item for item in result.replies if item)
        if not text.strip():
            return
        try:
            await self._host.send_chat(  # type: ignore[attr-defined]
                text,
                source=f"{result.framework}:{result.plugin_id}",
                metadata={"bridge_command": result.command, "bridge_plugin": result.plugin_id},
            )
        except Exception as exc:  # noqa: BLE001 — 投递失败要记录，命令本身已执行
            self._host.log("error", f"推送命令结果失败：{type(exc).__name__}: {exc}", source="router")  # type: ignore[attr-defined]

    async def dispatch_events(self, event_type: str, payload: Mapping[str, Any]) -> int:
        """把一条事件广播给所有订阅了它的外来插件，返回处理者数量。"""
        delivered = 0
        timeout = _resolve_call_timeout(self._host)  # type: ignore[attr-defined]
        for key, plugin in self._registry.plugins.items():  # type: ignore[attr-defined]
            handler = plugin.dispatch_event
            if handler is None:
                continue
            subscribed = {spec.event_type for spec in plugin.events}
            if event_type not in subscribed and "*" not in subscribed:
                continue
            try:
                await asyncio.wait_for(handler(event_type, dict(payload)), timeout=timeout)
                delivered += 1
            except asyncio.TimeoutError:
                self._host.log("warning", f"{key}.{event_type} 事件超时", source="router")  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 — 一个插件的事件失败不影响其它插件
                self._host.log(  # type: ignore[attr-defined]
                    "warning",
                    f"{key}.{event_type} 事件失败：{type(exc).__name__}: {exc}",
                    source="router",
                )
        return delivered

    async def call_plugin_tool(self, plugin: str, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        loaded = self._registry.get(plugin)  # type: ignore[attr-defined]
        if loaded is None:
            raise PluginNotLoadedError(plugin)
        handler = loaded.call_tool
        if handler is None:
            return {"success": False, "error": f"{plugin} 没有实现工具调用", "code": "unsupported_capability"}
        timeout = _resolve_call_timeout(self._host)  # type: ignore[attr-defined]
        try:
            result = await asyncio.wait_for(handler(tool, dict(arguments)), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise CommandTimeoutError(f"{plugin}:{tool}", timeout) from exc
        return _as_tool_payload(result)

    async def call_plugin_api(self, plugin: str, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        loaded = self._registry.get(plugin)  # type: ignore[attr-defined]
        if loaded is None:
            raise PluginNotLoadedError(plugin)
        handler = loaded.call_api
        if handler is None:
            return {"success": False, "error": f"{plugin} 没有实现 API 调用", "code": "unsupported_capability"}
        timeout = _resolve_call_timeout(self._host)  # type: ignore[attr-defined]
        try:
            result = await asyncio.wait_for(handler(name, dict(arguments)), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise CommandTimeoutError(f"{plugin}:{name}", timeout) from exc
        return _as_tool_payload(result)

    # ------------------------------------------------------------------ 视图

    def list_payload(self) -> list[dict[str, Any]]:
        payload: list[dict[str, Any]] = []
        for key, plugin in self._registry.plugins.items():  # type: ignore[attr-defined]
            payload.append(
                {
                    "plugin": key,
                    "framework": plugin.framework,
                    "name": plugin.manifest.display_name,
                    "version": plugin.manifest.version,
                    "commands": [
                        {"id": spec.id, "name": spec.display_name, "description": spec.description, "patterns": list(spec.patterns)}
                        for spec in plugin.commands
                    ],
                    "tools": [
                        {"name": spec.name, "description": spec.description} for spec in plugin.tools
                    ],
                    "events": [spec.event_type for spec in plugin.events],
                    "apis": [spec.name for spec in plugin.apis],
                    "exposed_tools": sorted(name for name, meta in self._tools.items() if meta[0].startswith("bot_")),
                }
            )
        return payload

    def status(self) -> dict[str, Any]:
        return {"tools": sorted(self._tools), "entries": sorted(self._entries)}


def _priority_of(plugin: LoadedPlugin) -> int:
    return max((spec.priority for spec in plugin.commands), default=0)


def _specificity_of(plugin: LoadedPlugin) -> int:
    """有明确触发模式的命令（正则 / 前缀 / 关键词）优先于「什么都收」的监听器。

    否则一个框架里的 catch-all 监听器会抢走另一个框架里精确命令的消息 —— 顺序就成了
    加载顺序的副作用。这里给出一个确定的、可解释的排序依据；需要点名时用
    ``bridge_run_command`` 的 ``plugin`` 参数。
    """
    return sum(1 for spec in plugin.commands if spec.patterns)


def _command_schema(spec: CommandSpec) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": f"触发「{spec.display_name}」的用户消息原文"
                + (f"（形如 {spec.usage}）" if spec.usage else ""),
            }
        },
        "required": ["text"],
    }


def _message_payload(context: CommandContext) -> dict[str, Any]:
    return {
        "text": context.text,
        "user_id": context.user_id,
        "user_name": context.user_name,
        "group_id": context.group_id,
        "channel": context.channel,
        "source": context.source,
        "is_group": context.is_group,
        "raw": dict(context.raw),
    }


def _as_tool_payload(result: object) -> dict[str, Any]:
    if isinstance(result, Mapping):
        return dict(result)
    if isinstance(result, (str, int, float, bool)) or result is None:
        return {"success": True, "result": result}
    if isinstance(result, Sequence):
        return {"success": True, "result": list(result)}
    return {"success": True, "result": str(result)}


__all__ = ["BridgeRouter", "DEFAULT_MAX_TOOLS", "TOOL_NAME_MAX", "sanitize_entry_id", "sanitize_tool_name"]
