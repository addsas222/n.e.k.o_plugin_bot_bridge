"""AstrBot 兼容层：把 AstrBot 插件（Star）加载进 N.E.K.O.。

插件目录布局（与上游一致）：``<plugin_dir>/metadata.yaml``（必需，字段
``name`` / ``desc`` / ``version`` / ``author``）+ 入口文件 ``main.py``（或与目录同名的
``<dir>.py``），可选 ``_conf_schema.json`` 配置 schema。上游
``star_manager._get_modules`` 与 ``updater.PLUGIN_METADATA_REQUIRED_FIELDS`` 是这两条
规则的出处。

插件模块用 ``sys.modules`` 里的私有包名加载（``_neko_bot_bridge_astrbot_<id>.<entry>``），
因此插件内部的 ``from .x import y`` 相对导入照常工作；插件里的 ``import astrbot.*``
由 ``bot_bridge/shims/astrbot``（只依赖标准库的 shim）满足。

能力映射（真实实现）：

=================================  =========================================================
AstrBot API                        桥到 N.E.K.O.
=================================  =========================================================
``context.send_message(umo, chain)`` / ``event.send(chain)``
                                   ``HostServices.send_chat``（只送文本；链里的非文本消息段
                                   记进 ``CommandResult.data["astrbot_results"]`` 并写警告日志）
``context.llm_generate`` / ``context.get_using_provider(_async)`` / ``provider.text_chat``
                                   ``HostServices.llm_generate``（N.E.K.O. 只有一个对话模型槽位；
                                   ``image_urls`` 编码成 OpenAI 多模态 content parts）
``provider.text_chat`` 的 ``contexts``  OpenAI 消息列表原样透传
``context.get_config`` / ``AstrBotConfig.save_config(_async)``
                                   插件配置（``_conf_schema.json`` 默认值 + ``HostServices.plugin_config``，
                                   写回走 ``save_plugin_config``）
``PluginKVStoreMixin``（``put/get/delete_kv_data``）
                                   ``<host.data_dir(plugin_id)>/plugin_kv.json``
``astrbot.api.logger``              ``HostServices.log``（source 为 ``astrbot.plugin.<id>``）
``context.get_registered_star`` / ``get_all_stars``
                                   shim 的 ``star_map`` / ``star_registry``
``StarTools.send_message`` / ``create_message``
                                   ``HostServices.send_chat``（``StarTools._context`` 由兼容层按插件注入）
``context.register_web_api``        登记为 ``ApiSpec``（N.E.K.O. 不承载插件的 HTTP 路由）
``@filter.llm_tool`` / ``context.register_llm_tool`` / 工具激活开关
                                   ``ToolSpec`` + ``call_tool``（本地工具注册表）
``@filter.command`` / ``command_group`` / ``regex`` / ``event_message_type`` /
``permission_type`` / ``platform_adapter_type``
                                   ``CommandSpec`` / ``EventSpec``
``@filter.on_*``（LLM 请求、Agent、发送前后、插件加载/卸载/报错等）
                                   ``EventSpec`` + ``dispatch_event``
``Star.initialize`` / ``Star.terminate``
                                   ``start()`` / ``stop()``
=================================  =========================================================

没有 N.E.K.O. 等价实现的能力**一律**抛
``astrbot.UnsupportedCapabilityError``（``code="unsupported_capability"``，带 ``to_payload()``），
并被登记到 ``LoadedPlugin.native["unsupported_calls"]``；绝不假装成功。清单：

* 渲染：``Star.text_to_image`` / ``Star.html_render``（``render.t2i`` / ``render.custom_template``）
* 图片处理：``Image.convert_to_file_path`` / ``convert_to_base64`` / ``register_to_file_service``
* 事件：``event.send_streaming`` / ``event.react`` / ``event.get_group``；
  ``StarTools.create_event``（需要平台适配器实例）
* 模型：``provider.text_chat_stream`` / ``provider.get_models`` / ``provider.set_model``；
  ``text_chat`` 收到 ``audio_urls`` / ``func_tool``（ToolSet）/ ``tool_calls_result`` /
  ``extra_user_content_parts`` / ``tool_choice != "auto"`` 时抛能力缺失
* 上下文的宿主设施：``get_db`` / ``get_platform(_inst)`` / ``get_event_queue`` /
  ``register_provider`` / ``register_task`` / ``tool_loop_agent`` / ``add_llm_tools`` /
  ``unregister_llm_tool`` 之外的 provider 查询（多 provider、TTS/STT/Embedding）/
  ``get_current_chat_provider_id`` / ``get_provider_by_id``
* 上游管理器对象（``provider_manager`` / ``platform_manager`` / ``conversation_manager`` /
  ``message_history_manager`` / ``persona_manager`` / ``astrbot_config_mgr`` / ``kb_manager`` /
  ``cron_manager`` / ``subagent_orchestrator``）是会在取用时抛错的占位对象
* shim 未提供（导入即 ``ImportError``）：``filter.agent`` / ``RegisteringAgent`` /
  ``astrbot.core.sp``（共享偏好）/ ``astrbot.core.config`` 全局配置 /
  ``FunctionTool`` 与 ``ToolSet`` / 平台适配器注册 API / ``astrbot.core.db`` 等
  宿主内部模块

有意的差异（不是能力缺失）：

* ``event.send_typing`` / ``stop_typing`` 只写一条 debug 日志（N.E.K.O. 没有"正在输入"
  展示位），有注释与文档说明。
* 唤醒判定按上游默认配置：私聊消息总是唤醒；群聊消息需要唤醒前缀（默认 ``/``，
  可用桥接配置的 ``astrbot_wake_prefix`` / ``wake_prefix`` / ``wake_prefixes``
  覆盖，配成空串表示群聊也不需要前缀），或在 ``CommandContext.raw`` 里带
  ``is_at_bot=True``（N.E.K.O. 知道这条消息 @ 了机器人时填）。
* 事件平台名固定为 ``neko``，因此按具体平台（如 ``AIOCQHTTP``）过滤的 Handler 不会
  命中，``PlatformAdapterType.ALL`` 仍命中。
* 命令别名的顺序按字典序稳定排序（上游是 set 迭代顺序）。
* ``load()`` 是同步接口（桥接契约），而上游在**异步**的 ``PluginManager.load``
  流程里实例化插件：少数插件在 ``__init__`` 里就会 ``asyncio.get_running_loop()`` /
  ``ensure_future(...)``（例如社区插件 code_executor）。这类插件需要从正在运行的
  事件循环里调用 ``load()``；否则会得到带提示的 ``PluginLoadError``。
* 外来插件的 ``requirements.txt`` 不会被安装（N.E.K.O. 只保证自己那份依赖）；
  缺第三方包时 ``PluginLoadError`` 会带上原始 ``ModuleNotFoundError``。
* ``filter.llm_tool`` 的 docstring 解析用标准库实现（``Args:`` 段落里的
  ``name(type): desc`` / ``name: type``），不依赖 ``docstring_parser``；解析不到的
  参数行会被跳过，而类型缺失/不支持仍然抛 ``ValueError``（与上游一致）。
"""

from __future__ import annotations

import functools
import importlib
import importlib.util
import inspect
import json
import logging
import sys
import threading
import time
import traceback
import types
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts import (
    ApiSpec,
    CommandContext,
    CommandResult,
    CommandSpec,
    EventSpec,
    HostServices,
    LoadedPlugin,
    PluginManifest,
    ToolSpec,
)
from ..errors import (
    BackendUnavailableError,
    BridgeError,
    PluginLoadError,
    PluginNotLoadedError,
    UnsupportedCapabilityError,
)

log = logging.getLogger(__name__)

_METADATA_FILENAMES = ("metadata.yaml", "metadata.yml")
"""上游 ``updater.PLUGIN_METADATA_FILENAMES`` 的顺序。"""

_REQUIRED_METADATA_FIELDS = ("name", "desc", "version", "author")
"""上游 ``updater.PLUGIN_METADATA_REQUIRED_FIELDS``。"""

_ENTRY_FALLBACK = "main.py"
"""上游优先用 ``main.py``，否则用 ``<目录名>.py``（``star_manager._get_modules``）。"""

_PRIVATE_MODULE_PREFIX = "_neko_bot_bridge_astrbot_"
"""插件模块的私有命名空间前缀，避免污染全局模块名。"""

_PLATFORM_NAME = "neko"
"""桥接层给 AstrBot 事件填的平台名。"""

_SHIM_ROOT = Path(__file__).resolve().parents[2] / "shims"
_SHIM_LOCK = threading.RLock()
_LOAD_LOCK = threading.RLock()
_PLUGIN_ID_LOCK = threading.RLock()

PY_TO_JSON_TYPE = {
    "int": "number",
    "float": "number",
    "bool": "boolean",
    "str": "string",
    "dict": "object",
    "list": "array",
    "tuple": "array",
    "set": "array",
}


# --------------------------------------------------------------------------- #
# shim 加载
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Shim:
    """已导入的 shim 对象集合（缓存，避免反复 getattr）。"""

    astrbot: types.ModuleType
    runtime: Any
    star_map: dict[str, Any]
    star_registry: list[Any]
    handlers: Any
    llm_tools: Any
    event_type: Any
    adapter_message_event: Any
    calling_func_tool_event: Any
    star_cls: type
    star_tools_cls: type
    context_cls: type
    config_cls: type
    event_cls: type
    plain_cls: type
    message_type: Any
    message_member_cls: type
    group_cls: type
    astrbot_message_cls: type
    platform_metadata_cls: type
    provider_cls: type
    provider_request_cls: type
    llm_response_cls: type
    provider_error_cls: type
    unsupported_error: type
    command_filter_cls: type
    command_group_filter_cls: type
    greedy_str_cls: type
    regex_filter_cls: type
    permission_filter_cls: type
    event_message_type_filter_cls: type
    platform_adapter_filter_cls: type


_shim_cache: _Shim | None = None


def _is_our_shim(module: object) -> bool:
    """判断 ``sys.modules['astrbot']`` 是不是本项目的 shim。"""
    return bool(getattr(module, "_NEKO_BRIDGE_SHIM", False))


def _ensure_shim() -> _Shim:
    """把 ``bot_bridge/shims`` 放进 ``sys.path`` 并导入兼容 shim（幂等、线程安全）。"""
    global _shim_cache
    with _SHIM_LOCK:
        if _shim_cache is not None:
            return _shim_cache
        package_init = _SHIM_ROOT / "astrbot" / "__init__.py"
        if not package_init.is_file():
            raise BackendUnavailableError(
                "astrbot",
                f"compatibility shim not found at {package_init}",
            )
        existing = sys.modules.get("astrbot")
        if existing is not None and not _is_our_shim(existing):
            raise BackendUnavailableError(
                "astrbot",
                "another 'astrbot' package is already imported in this process; "
                "the compatibility shim cannot take over",
            )
        root_str = str(_SHIM_ROOT)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        astrbot = importlib.import_module("astrbot")
        if not _is_our_shim(astrbot):
            raise BackendUnavailableError(
                "astrbot",
                f"imported 'astrbot' is not the bot_bridge shim: {getattr(astrbot, '__file__', '?')}",
            )
        core_star = importlib.import_module("astrbot.core.star")
        core_star_star = importlib.import_module("astrbot.core.star.star")
        core_star_handler = importlib.import_module("astrbot.core.star.star_handler")
        core_star_llm_tools = importlib.import_module("astrbot.core.star.llm_tools")
        core_star_filter_command = importlib.import_module("astrbot.core.star.filter.command")
        core_star_filter_command_group = importlib.import_module(
            "astrbot.core.star.filter.command_group",
        )
        core_star_filter_regex = importlib.import_module("astrbot.core.star.filter.regex")
        core_star_filter_permission = importlib.import_module(
            "astrbot.core.star.filter.permission",
        )
        core_star_filter_emt = importlib.import_module(
            "astrbot.core.star.filter.event_message_type",
        )
        core_star_filter_pat = importlib.import_module(
            "astrbot.core.star.filter.platform_adapter_type",
        )
        core_message = importlib.import_module("astrbot.core.message.components")
        core_platform = importlib.import_module("astrbot.core.platform")
        core_provider = importlib.import_module("astrbot.core.provider")
        core_config = importlib.import_module("astrbot.core.config")
        _shim_cache = _Shim(
            astrbot=astrbot,
            runtime=importlib.import_module("astrbot._runtime"),
            star_map=core_star_star.star_map,
            star_registry=core_star_star.star_registry,
            handlers=core_star_handler.star_handlers_registry,
            llm_tools=core_star_llm_tools.llm_tools,
            event_type=core_star_handler.EventType,
            adapter_message_event=core_star_handler.EventType.AdapterMessageEvent,
            calling_func_tool_event=core_star_handler.EventType.OnCallingFuncToolEvent,
            star_cls=core_star.Star,
            star_tools_cls=core_star.StarTools,
            context_cls=core_star.Context,
            config_cls=core_config.AstrBotConfig,
            event_cls=core_platform.AstrMessageEvent,
            plain_cls=core_message.Plain,
            message_type=core_platform.MessageType,
            message_member_cls=core_platform.MessageMember,
            group_cls=core_platform.Group,
            astrbot_message_cls=core_platform.AstrBotMessage,
            platform_metadata_cls=core_platform.PlatformMetadata,
            provider_cls=core_provider.Provider,
            provider_request_cls=core_provider.ProviderRequest,
            llm_response_cls=core_provider.LLMResponse,
            provider_error_cls=core_provider.ProviderError,
            unsupported_error=astrbot.UnsupportedCapabilityError,
            command_filter_cls=core_star_filter_command.CommandFilter,
            command_group_filter_cls=core_star_filter_command_group.CommandGroupFilter,
            greedy_str_cls=core_star_filter_command.GreedyStr,
            regex_filter_cls=core_star_filter_regex.RegexFilter,
            permission_filter_cls=core_star_filter_permission.PermissionTypeFilter,
            event_message_type_filter_cls=core_star_filter_emt.EventMessageTypeFilter,
            platform_adapter_filter_cls=core_star_filter_pat.PlatformAdapterTypeFilter,
        )
        return _shim_cache


# --------------------------------------------------------------------------- #
# 宿主能力面适配（shim → HostServices）
# --------------------------------------------------------------------------- #


async def _shim_unsupported(awaitable: Any) -> Any:
    """能力开关关闭时宿主抛的桥接错误换成 shim 的 ``astrbot.UnsupportedCapabilityError``（插件能 catch，handler 结果里能结构化）。"""
    try:
        return await awaitable
    except UnsupportedCapabilityError as exc:
        raise _ensure_shim().unsupported_error(exc.capability, exc.detail) from exc


class _HostBridge:
    """把 N.E.K.O. 的 :class:`HostServices` 适配成 shim 期望的接口。"""

    def __init__(self, host: HostServices) -> None:
        self._host = host

    def capability_enabled(self, capability: str) -> bool:
        """``[bridge.capabilities]`` 开关（KV 存储据此拦 storage）。"""
        check = getattr(self._host, "capability_enabled", None)
        return bool(check(capability)) if callable(check) else True

    async def send_text(
        self,
        plugin_id: str,
        text: str,
        *,
        umo: str = "",
        source: str = "",
        dropped: tuple[str, ...] = (),
    ) -> bool:
        """把一段文本送进 N.E.K.O. 对话。"""
        return await _shim_unsupported(self._host.send_chat(
            text,
            source=source or f"astrbot:{plugin_id}",
            metadata={
                "framework": "astrbot",
                "plugin_id": plugin_id,
                "umo": umo,
                "dropped_components": list(dropped),
            },
        ))

    async def llm_generate(
        self,
        plugin_id: str,
        *,
        prompt: str | list[dict[str, Any]] | None,
        system_prompt: str = "",
        model: str = "",
        image_urls: tuple[str, ...] = (),
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """调用 N.E.K.O. 大模型。"""
        return await self._host.llm_generate(
            prompt or "",
            model=model,
            system=system_prompt,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    async def plugin_config(self, plugin_id: str) -> Mapping[str, Any]:
        """读取插件配置。"""
        return await _shim_unsupported(self._host.plugin_config(plugin_id))

    async def save_plugin_config(self, plugin_id: str, data: Mapping[str, Any]) -> bool:
        """写回插件配置。"""
        return await _shim_unsupported(self._host.save_plugin_config(plugin_id, data))

    def data_dir(self, plugin_id: str) -> Path:
        """插件数据目录。"""
        return self._host.data_dir(plugin_id)

    def log(self, level: str, message: str, *, plugin_id: str = "", source: str = "") -> None:
        """写宿主日志。"""
        self._host.log(level, message, source=source or f"astrbot:{plugin_id}")


_host_bridge: _HostBridge | None = None
_host_bridge_lock = threading.RLock()


def _ensure_host_bridge(host: HostServices, shim: _Shim) -> _HostBridge:
    """把宿主能力面注入 shim（幂等；换了 ``HostServices`` 实例则重新注入）。"""
    global _host_bridge
    with _host_bridge_lock:
        if _host_bridge is None or _host_bridge._host is not host:
            _host_bridge = _HostBridge(host)
            shim.runtime.RUNTIME.install_host(_host_bridge)
        return _host_bridge


# --------------------------------------------------------------------------- #
# 插件状态
# --------------------------------------------------------------------------- #


@dataclass
class _PluginState:
    """一个已加载 AstrBot 插件的运行状态。"""

    manifest: PluginManifest
    host: HostServices
    shim: _Shim
    plugin_id: str
    native_plugin_id: str
    module_name: str
    module: types.ModuleType
    plugin_dir: Path
    entry: Path
    metadata_file: Path
    instance: Any
    context: Any
    config: Any
    handlers: list[Any]
    tools: list[Any]
    native: dict[str, Any] = field(default_factory=dict)
    start_error: str = ""


_states: dict[str, _PluginState] = {}
_claimed_plugin_ids: dict[str, Path] = {}


def _claim_plugin_id(dir_name: str, plugin_dir: Path) -> str:
    """占用一个桥接层插件 id（目录名）；重名时加序号并记日志。"""
    with _PLUGIN_ID_LOCK:
        owner = _claimed_plugin_ids.get(dir_name)
        if owner is None or owner == plugin_dir:
            _claimed_plugin_ids[dir_name] = plugin_dir
            return dir_name
        suffix = 2
        while f"{dir_name}-{suffix}" in _claimed_plugin_ids:
            suffix += 1
        claimed = f"{dir_name}-{suffix}"
        _claimed_plugin_ids[claimed] = plugin_dir
        log.warning(
            "AstrBot plugin dir %s already claimed by %s; registering %s as %s",
            dir_name,
            owner,
            plugin_dir,
            claimed,
        )
        return claimed


# --------------------------------------------------------------------------- #
# 清单解析
# --------------------------------------------------------------------------- #


def _require_yaml() -> Any:
    """导入 PyYAML；缺失时抛 :class:`BackendUnavailableError`。"""
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的显式失败
        raise BackendUnavailableError(
            "astrbot",
            "PyYAML is required to read AstrBot metadata.yaml",
        ) from exc
    return yaml


def _find_metadata_file(plugin_dir: Path) -> Path | None:
    """按上游顺序找 metadata 文件。"""
    for name in _METADATA_FILENAMES:
        candidate = plugin_dir / name
        if candidate.is_file():
            return candidate
    return None


def _find_entry_module(plugin_dir: Path) -> Path | None:
    """按上游规则找入口文件：``main.py`` 优先，否则 ``<目录名>.py``。"""
    main = plugin_dir / _ENTRY_FALLBACK
    if main.is_file():
        return main
    same_name = plugin_dir / f"{plugin_dir.name}.py"
    if same_name.is_file():
        return same_name
    return None


def _load_metadata(metadata_file: Path) -> dict[str, Any]:
    """读取并校验 metadata（字段类型与上游 ``validate_plugin_metadata`` 一致）。"""
    yaml = _require_yaml()
    try:
        raw = yaml.safe_load(metadata_file.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise PluginLoadError(metadata_file.parent.name, f"metadata 解析失败：{exc}") from exc
    if not isinstance(raw, dict):
        raise PluginLoadError(metadata_file.parent.name, f"{metadata_file.name} 格式错误。")
    metadata = dict(raw)
    if "desc" not in metadata and "description" in metadata:
        metadata["desc"] = metadata["description"]
    missing = [name for name in _REQUIRED_METADATA_FIELDS if name not in metadata]
    if missing:
        raise PluginLoadError(
            metadata_file.parent.name,
            f"{metadata_file.name} 中缺少必需字段: {', '.join(missing)}。",
        )
    invalid = [
        name
        for name in _REQUIRED_METADATA_FIELDS
        if not isinstance(metadata[name], str) or not metadata[name].strip()
    ]
    if invalid:
        raise PluginLoadError(
            metadata_file.parent.name,
            f"{metadata_file.name} 中字段 {', '.join(invalid)} 必须是非空字符串。",
        )
    return metadata


def _read_metadata_lenient(metadata_file: Path) -> dict[str, Any] | None:
    """``detect()`` 用的宽松读取：任何问题都只返回 ``None``，不抛异常。"""
    try:
        return _load_metadata(metadata_file)
    except BridgeError as exc:
        log.warning("AstrBot detection: %s", exc)
        return None


def _load_conf_schema(plugin_dir: Path) -> dict[str, Any] | None:
    """读取 ``_conf_schema.json``（不存在返回 ``None``）。"""
    schema_path = plugin_dir / "_conf_schema.json"
    if not schema_path.is_file():
        return None
    try:
        raw = json.loads(schema_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("AstrBot plugin %s: invalid _conf_schema.json (%s)", plugin_dir, exc)
        return None
    if not isinstance(raw, dict):
        log.warning("AstrBot plugin %s: _conf_schema.json must be an object", plugin_dir)
        return None
    return raw


# --------------------------------------------------------------------------- #
# 事件类型 / 事件名
# --------------------------------------------------------------------------- #

_EVENT_NAME_BY_MEMBER = {
    "OnAstrBotLoadedEvent": "on_astrbot_loaded",
    "OnPlatformLoadedEvent": "on_platform_loaded",
    "OnPluginLoadedEvent": "on_plugin_loaded",
    "OnPluginUnloadedEvent": "on_plugin_unloaded",
    "OnPluginErrorEvent": "on_plugin_error",
    "OnWaitingLLMRequestEvent": "on_waiting_llm_request",
    "OnLLMRequestEvent": "on_llm_request",
    "OnLLMResponseEvent": "on_llm_response",
    "OnAgentBeginEvent": "on_agent_begin",
    "OnAgentDoneEvent": "on_agent_done",
    "OnDecoratingResultEvent": "on_decorating_result",
    "OnAfterMessageSentEvent": "after_message_sent",
    "OnUsingLLMToolEvent": "on_using_llm_tool",
    "OnLLMToolRespondEvent": "on_llm_tool_respond",
    "AdapterMessageEvent": "message",
    "OnCallingFuncToolEvent": "calling_func_tool",
}
"""事件类型成员名 → 桥接层事件名（钩子用装饰器名，消息事件用 ``message``）。"""

_HOOK_ARGS = {
    "OnAstrBotLoadedEvent": (),
    "OnPlatformLoadedEvent": (),
    "OnPluginLoadedEvent": ("metadata",),
    "OnPluginUnloadedEvent": ("metadata",),
    "OnPluginErrorEvent": ("event", "plugin_name", "handler_name", "error", "traceback_text"),
    "OnWaitingLLMRequestEvent": ("event",),
    "OnLLMRequestEvent": ("event", "req"),
    "OnLLMResponseEvent": ("event", "response"),
    "OnAgentBeginEvent": ("event", "run_context"),
    "OnAgentDoneEvent": ("event", "run_context", "response"),
    "OnDecoratingResultEvent": ("event",),
    "OnAfterMessageSentEvent": ("event",),
    "OnUsingLLMToolEvent": ("event", "tool", "tool_args"),
    "OnLLMToolRespondEvent": ("event", "tool", "tool_args", "tool_result"),
    "AdapterMessageEvent": ("event",),
}
"""各钩子处理器期望的位置参数名（取自上游 register/star_handler.py 的文档字符串）。"""


def _normalise_event_name(event_type: str) -> str:
    """归一化事件名：大小写、``on_`` 前缀、``_event`` 后缀都不敏感。"""
    text = "".join(ch for ch in str(event_type).lower() if ch.isalnum())
    if text.startswith("on"):
        text = text[2:]
    if text.endswith("event"):
        text = text[: -len("event")]
    return text


_EVENT_LOOKUP: dict[str, str] = {}
for _member, _name in _EVENT_NAME_BY_MEMBER.items():
    _EVENT_LOOKUP[_normalise_event_name(_member)] = _member
    _EVENT_LOOKUP[_normalise_event_name(_name)] = _member
_EVENT_LOOKUP[_normalise_event_name("message_event")] = "AdapterMessageEvent"


# --------------------------------------------------------------------------- #
# 兼容层
# --------------------------------------------------------------------------- #


class AstrBotBackend:
    """AstrBot 框架兼容层。"""

    name = "astrbot"
    display_name = "AstrBot"
    requires = ("yaml",)

    # -- 识别 -------------------------------------------------------------- #

    def detect(self, path: Path) -> PluginManifest | None:
        """识别 AstrBot 插件目录（``metadata.yaml`` + 入口文件）。

        只做只读检查：不是 AstrBot 插件（或元数据不可用时）返回 ``None``，不抛异常。
        """
        try:
            return self._detect(path)
        except Exception as exc:  # noqa: BLE001 - detect 契约要求永不抛异常
            log.warning("AstrBot detect(%s) failed: %r", path, exc)
            return None

    def _detect(self, path: Path) -> PluginManifest | None:
        plugin_dir = Path(path)
        if not plugin_dir.is_dir():
            return None
        metadata_file = _find_metadata_file(plugin_dir)
        if metadata_file is None:
            return None
        entry = _find_entry_module(plugin_dir)
        if entry is None:
            log.debug(
                "AstrBot detection: %s has no %s nor %s.py",
                plugin_dir,
                _ENTRY_FALLBACK,
                plugin_dir.name,
            )
            return None
        metadata = _read_metadata_lenient(metadata_file)
        if metadata is None:
            return None
        name = str(metadata["name"])
        author = str(metadata["author"])
        return PluginManifest(
            path=plugin_dir,
            plugin_id=plugin_dir.name,
            framework=self.name,
            name=name,
            version=str(metadata["version"]),
            description=str(metadata["desc"]),
            entry=entry.name,
            extra={
                "author": author,
                "astrbot_plugin_id": f"{author.lower().replace('/', '_')}/{name.lower().replace('/', '_')}",
                "short_desc": metadata.get("short_desc") or "",
                "display_name": metadata.get("display_name") or "",
                "repo": metadata.get("repo") or "",
                "astrbot_version": metadata.get("astrbot_version") or "",
                "support_platforms": list(metadata.get("support_platforms") or []),
                "metadata_file": metadata_file.name,
                "module": entry.stem,
            },
        )

    # -- 加载 -------------------------------------------------------------- #

    def load(self, manifest: PluginManifest, host: HostServices) -> LoadedPlugin:
        """导入并实例化插件，整理出四类能力描述与四个回调。"""
        plugin_dir = Path(manifest.path)
        if not plugin_dir.is_dir():
            raise PluginLoadError(manifest.plugin_id, f"插件目录不存在：{plugin_dir}")
        metadata_file = _find_metadata_file(plugin_dir)
        if metadata_file is None:
            raise PluginLoadError(manifest.plugin_id, "找不到 metadata.yaml / metadata.yml")
        entry = _find_entry_module(plugin_dir)
        if entry is None:
            raise PluginLoadError(
                manifest.plugin_id,
                f"既没有 {_ENTRY_FALLBACK} 也没有 {plugin_dir.name}.py",
            )
        metadata = _load_metadata(metadata_file)
        shim = _ensure_shim()
        _ensure_host_bridge(host, shim)
        plugin_id = _claim_plugin_id(manifest.plugin_id, plugin_dir)
        manifest.plugin_id = plugin_id
        module_name = f"{_PRIVATE_MODULE_PREFIX}{_safe_module_part(plugin_id)}.{entry.stem}"
        native_plugin_id = f"{str(metadata['author']).lower().replace('/', '_')}/{str(metadata['name']).lower().replace('/', '_')}"
        with _LOAD_LOCK:
            state = self._load_plugin(
                manifest=manifest,
                host=host,
                shim=shim,
                plugin_dir=plugin_dir,
                metadata_file=metadata_file,
                entry=entry,
                metadata=metadata,
                plugin_id=plugin_id,
                native_plugin_id=native_plugin_id,
                module_name=module_name,
            )
            _states[plugin_id] = state
        loaded = self._build_loaded_plugin(state)
        log.info(
            "AstrBot plugin %s loaded (module=%s, commands=%d, tools=%d, events=%d, apis=%d)",
            plugin_id,
            module_name,
            len(loaded.commands),
            len(loaded.tools),
            len(loaded.events),
            len(loaded.apis),
        )
        return loaded

    def _load_plugin(
        self,
        *,
        manifest: PluginManifest,
        host: HostServices,
        shim: _Shim,
        plugin_dir: Path,
        metadata_file: Path,
        entry: Path,
        metadata: Mapping[str, Any],
        plugin_id: str,
        native_plugin_id: str,
        module_name: str,
    ) -> _PluginState:
        """导入模块、实例化插件类、绑定 Handler（持锁执行）。"""
        shim.handlers.unload_module(module_name)
        shim.llm_tools.unload_module(module_name)
        shim.star_map.pop(module_name, None)
        shim.star_registry[:] = [
            item for item in shim.star_registry if item.module_path != module_name
        ]
        shim.runtime.RUNTIME.unbind_plugin(native_plugin_id)

        module = _import_plugin_module(plugin_dir, entry, module_name, plugin_id)
        try:
            plugin_cls = _find_plugin_class(shim, module, module_name, plugin_id)
        except PluginLoadError:
            _purge_plugin(shim, module_name, native_plugin_id)
            raise

        author = str(metadata["author"])
        name = str(metadata["name"])
        lower_author, lower_name = native_plugin_id.split("/", 1)
        for target in (plugin_cls,):
            setattr(target, "name", lower_name)
            setattr(target, "author", lower_author)
            setattr(target, "plugin_id", native_plugin_id)

        star_metadata = shim.star_map.get(module_name)
        if star_metadata is not None:
            star_metadata.name = name
            star_metadata.author = author
            star_metadata.desc = str(metadata["desc"])
            star_metadata.short_desc = metadata.get("short_desc") or None
            star_metadata.version = str(metadata["version"])
            star_metadata.repo = metadata.get("repo") or None
            star_metadata.display_name = metadata.get("display_name") or None
            star_metadata.astrbot_version = metadata.get("astrbot_version") or None
            star_metadata.support_platforms = list(metadata.get("support_platforms") or [])
            star_metadata.root_dir_name = plugin_dir.name
            star_metadata.reserved = False
            star_metadata.activated = True
            star_metadata.module = module
            star_metadata.star_cls_type = plugin_cls

        schema = _load_conf_schema(plugin_dir)
        config = shim.config_cls(
            config_path=f"{plugin_id}/config",
            schema=schema,
            host=_host_bridge,
            plugin_id=plugin_id,
        )
        data_dir = host.data_dir(plugin_id)
        if data_dir.is_dir():
            shim.runtime.RUNTIME.bind_plugin(native_plugin_id, plugin_id, data_dir)
        else:
            log.warning("AstrBot plugin %s: data dir %s is missing", plugin_id, data_dir)

        context = shim.context_cls(
            bridge_plugin_id=plugin_id,
            native_plugin_id=native_plugin_id,
            host=_host_bridge,
            config=config,
            provider_factory=lambda *, provider_id="neko": shim.provider_cls(
                _host_bridge,
                plugin_id,
                provider_id=provider_id,
            ),
            plugin_name=name,
        )
        # 上游由宿主注入全局 Context；这里按插件注入（StarTools._context 是类级单例）
        shim.star_tools_cls.initialize(context)

        try:
            instance = _instantiate_plugin(plugin_cls, context, config)
        except Exception as exc:
            _purge_plugin(shim, module_name, native_plugin_id)
            raise PluginLoadError(
                plugin_id,
                f"实例化插件类 {plugin_cls.__name__} 失败：{exc!r}{_loop_hint(exc)}",
            ) from exc
        setattr(instance, "name", lower_name)
        setattr(instance, "author", lower_author)
        setattr(instance, "plugin_id", native_plugin_id)
        if star_metadata is not None:
            star_metadata.star_cls = instance

        handlers = list(shim.handlers.bind_module(module_name, instance))
        tools = list(shim.llm_tools.bind_module(module_name, instance))
        unsupported_sink = shim.runtime.RUNTIME.register_unsupported_sink(native_plugin_id)
        native: dict[str, Any] = {
            "framework": "astrbot",
            "plugin_id": plugin_id,
            "native_plugin_id": native_plugin_id,
            "module": module_name,
            "module_file": str(entry),
            "class": plugin_cls.__name__,
            "metadata_file": metadata_file.name,
            "metadata": {
                "name": name,
                "author": author,
                "version": str(metadata["version"]),
                "desc": str(metadata["desc"]),
                "repo": metadata.get("repo") or "",
                "display_name": metadata.get("display_name") or "",
            },
            "conf_schema": sorted(schema) if schema else [],
            "wake_prefixes": list(_wake_prefixes(host.settings)),
            "config_keys": sorted(str(key) for key in config),
            "handlers": {
                "message": len(
                    [
                        item
                        for item in handlers
                        if item.event_type is shim.adapter_message_event
                    ],
                ),
                "hooks": len(
                    [
                        item
                        for item in handlers
                        if item.event_type not in (shim.adapter_message_event,)
                    ],
                ),
                "tools": len(tools),
            },
            "unsupported_calls": unsupported_sink,
            "lifecycle": ["loaded"],
            "start_error": "",
            "shim_version": getattr(shim.astrbot, "_NEKO_SHIM_VERSION", ""),
            "astrbot_version": getattr(shim.astrbot, "__version__", ""),
        }
        return _PluginState(
            manifest=manifest,
            host=host,
            shim=shim,
            plugin_id=plugin_id,
            native_plugin_id=native_plugin_id,
            module_name=module_name,
            module=module,
            plugin_dir=plugin_dir,
            entry=entry,
            metadata_file=metadata_file,
            instance=instance,
            context=context,
            config=config,
            handlers=handlers,
            tools=tools,
            native=native,
        )

    def _build_loaded_plugin(self, state: _PluginState) -> LoadedPlugin:
        """把插件状态整理成 :class:`LoadedPlugin`。"""
        commands, tools, events, apis = _build_specs(state)
        loaded = LoadedPlugin(
            manifest=state.manifest,
            instance=state.instance,
            commands=commands,
            tools=tools,
            events=events,
            apis=apis,
            handle_message=functools.partial(self._handle_message, state),
            call_tool=functools.partial(self._call_tool, state),
            call_api=functools.partial(self._call_api, state),
            dispatch_event=functools.partial(self.dispatch_event, state),
            native=state.native,
            started=False,
            loaded_at=time.time(),
        )
        state.native["specs"] = {
            "commands": [item.id for item in commands],
            "tools": [item.name for item in tools],
            "events": [item.event_type for item in events],
            "apis": [item.name for item in apis],
        }
        return loaded

    # -- 生命周期 ---------------------------------------------------------- #

    async def start(self, plugin: LoadedPlugin) -> None:
        """执行 ``initialize()`` 与 ``on_astrbot_loaded`` 钩子。

        插件初始化失败时只记录（日志 + ``native["start_error"]``），不抛异常。
        """
        state = _states.get(plugin.plugin_id)
        if state is None:
            raise PluginNotLoadedError(plugin.plugin_id)
        try:
            await self._refresh_config(state)
            initialize = getattr(state.instance, "initialize", None)
            if callable(initialize):
                await _maybe_await(initialize())
            state.native["lifecycle"].append("initialized")
            await self.dispatch_event(state, "on_astrbot_loaded", {})
            self._refresh_specs(state, plugin)
            plugin.started = True
            state.native["started"] = True
        except Exception as exc:  # noqa: BLE001 - 单个插件失败不能拖垮桥接
            state.start_error = repr(exc)
            state.native["start_error"] = state.start_error
            state.native["lifecycle"].append("start_failed")
            plugin.started = False
            plugin.native = state.native
            log.exception("AstrBot plugin %s failed to start", plugin.plugin_id)
            state.host.log(
                "error",
                f"AstrBot plugin {plugin.plugin_id} failed to start: {exc!r}",
                source=f"astrbot:{plugin.plugin_id}",
            )

    async def stop(self, plugin: LoadedPlugin) -> None:
        """执行 ``terminate()``（或插件的 ``__del__``）并注销注册表记录。"""
        state = _states.get(plugin.plugin_id)
        if state is None:
            log.warning("AstrBot plugin %s is not loaded; nothing to stop", plugin.plugin_id)
            return
        shim = state.shim
        try:
            await self._dispatch_plugin_unloaded(state)
            await _terminate_instance(state)
        except Exception as exc:  # noqa: BLE001 - 停用失败同样只记录
            state.start_error = repr(exc)
            state.native["start_error"] = state.start_error
            log.exception("AstrBot plugin %s failed to stop cleanly", plugin.plugin_id)
            state.host.log(
                "error",
                f"AstrBot plugin {plugin.plugin_id} failed to stop cleanly: {exc!r}",
                source=f"astrbot:{plugin.plugin_id}",
            )
        finally:
            _purge_plugin(shim, state.module_name, state.native_plugin_id)
            star_metadata = shim.star_map.get(state.module_name)
            if star_metadata is not None:
                star_metadata.activated = False
                star_metadata.star_cls = None
            state.native["lifecycle"].append("terminated")
            plugin.started = False
            _states.pop(plugin.plugin_id, None)

    async def _dispatch_plugin_unloaded(self, state: _PluginState) -> None:
        """触发该插件自己的 ``on_plugin_unloaded`` 钩子（卸载前）。"""
        await self.dispatch_event(state, "on_plugin_unloaded", {"metadata": state.native.get("metadata", {})})

    async def _refresh_config(self, state: _PluginState) -> None:
        """把宿主里保存的插件配置合并进配置对象（``_conf_schema.json`` 提供默认值）。"""
        stored = await state.host.plugin_config(state.plugin_id)
        if not stored:
            return
        for key, value in stored.items():
            state.config[str(key)] = value
        state.native["config_keys"] = sorted(str(key) for key in state.config)

    def _refresh_specs(self, state: _PluginState, plugin: LoadedPlugin) -> None:
        """把 ``initialize()`` 里新注册的指令 / 工具补进能力列表（原地追加）。"""
        commands, tools, events, apis = _build_specs(state)
        known_commands = {item.id for item in plugin.commands}
        known_tools = {item.name for item in plugin.tools}
        known_events = {(item.event_type, item.description) for item in plugin.events}
        known_apis = {item.name for item in plugin.apis}
        added = {"commands": 0, "tools": 0, "events": 0, "apis": 0}
        for spec in commands:
            if spec.id not in known_commands:
                plugin.commands.append(spec)
                added["commands"] += 1
        for tool in tools:
            if tool.name not in known_tools:
                plugin.tools.append(tool)
                added["tools"] += 1
        for event in events:
            if (event.event_type, event.description) not in known_events:
                plugin.events.append(event)
                added["events"] += 1
        for api in apis:
            if api.name not in known_apis:
                plugin.apis.append(api)
                added["apis"] += 1
        state.native["specs"] = {
            "commands": [item.id for item in plugin.commands],
            "tools": [item.name for item in plugin.tools],
            "events": [item.event_type for item in plugin.events],
            "apis": [item.name for item in plugin.apis],
        }
        if any(added.values()):
            log.info("AstrBot plugin %s registered late specs: %s", state.plugin_id, added)

    # -- 入站消息 ---------------------------------------------------------- #

    async def _handle_message(self, state: _PluginState, ctx: CommandContext) -> CommandResult:
        """按 AstrBot 的唤醒 / 过滤 / 执行流程跑一遍该插件的 Handler。"""
        shim = state.shim
        result = CommandResult(plugin_id=state.plugin_id, framework=self.name)
        prefixes = _wake_prefixes(state.host.settings)
        text = ctx.text or ""
        raw_text = text
        wake_prefix = ""
        for prefix in prefixes:
            if prefix and text.startswith(prefix):
                text = text[len(prefix):].strip()
                wake_prefix = prefix
                break
        # 唤醒判定与上游 WakingCheckStage 的默认配置一致：
        # 私聊总是唤醒；群聊需要唤醒前缀、或宿主标记的 @ 机器人；配置成空前缀时群聊也唤醒。
        woke = (
            not ctx.is_group
            or bool(wake_prefix)
            or bool(ctx.raw.get("is_at_bot"))
            or not prefixes
        )
        event = self._build_event(
            state,
            ctx,
            message_str=text,
            is_wake=woke,
            raw_text=raw_text,
        )
        data = result.data
        data["umo"] = event.unified_msg_origin
        data["wake_prefix"] = wake_prefix

        activated: list[Any] = []
        parsed_params: dict[str, dict[str, Any]] = {}
        permission_denied: list[str] = []
        filter_error = ""
        for metadata in shim.handlers.get_handlers_by_event_type(
            shim.adapter_message_event,
        ):
            if metadata.handler_module_path != state.module_name:
                continue
            if not metadata.event_filters:
                continue
            if any(
                isinstance(item, shim.command_group_filter_cls)
                for item in metadata.event_filters
            ):
                # 指令组函数本身不执行（与上游 WakingCheckStage 一致）
                continue
            passed = True
            permission_not_pass = False
            permission_raise = False
            for handler_filter in metadata.event_filters:
                try:
                    if isinstance(handler_filter, shim.permission_filter_cls):
                        if not handler_filter.filter(event, state.config):
                            permission_not_pass = True
                            permission_raise = permission_raise or handler_filter.raise_error
                    elif not handler_filter.filter(event, state.config):
                        passed = False
                        break
                except Exception as exc:  # noqa: BLE001 - 过滤器报错按上游直接回给用户
                    filter_error = f"插件 {state.manifest.display_name}: {exc}"
                    passed = False
                    break
            if filter_error:
                result.handled = True
                result.command = metadata.handler_name
                result.replies.append(filter_error)
                data["handlers"] = [metadata.handler_full_name]
                data["filter_error"] = True
                return result
            if not passed:
                continue
            if permission_not_pass:
                if not permission_raise:
                    continue
                permission_denied.append(metadata.handler_name)
                continue
            activated.append(metadata)
            # 与上游一样：有 Handler 命中即视为唤醒了机器人
            event.is_wake = True
            params = event.get_extra("parsed_params")
            if params is not None:
                parsed_params[metadata.handler_full_name] = params
            event._extras.pop("parsed_params", None)

        if permission_denied and not activated:
            result.handled = True
            result.command = permission_denied[0]
            data["permission_denied"] = permission_denied
            if bool(state.host.settings.get("no_permission_reply", True)):
                result.replies.append(
                    f"您(ID: {event.get_sender_id()})的权限不足以使用此指令。"
                    "通过 /sid 获取 ID 并请管理员添加。",
                )
            return result

        executed: list[str] = []
        results: list[dict[str, Any]] = []
        for metadata in activated:
            if event.is_stopped():
                break
            params = parsed_params.get(metadata.handler_full_name) or {}
            try:
                produced_any = False
                async for produced in _iterate_handler(event, metadata.handler, params):
                    if not produced_any:
                        produced_any = True
                        executed.append(metadata.handler_full_name)
                    if produced is not None:
                        results.extend(_collect_result(produced, result))
                    if event.is_stopped():
                        break
                if not produced_any:
                    executed.append(metadata.handler_full_name)
            except Exception as exc:  # noqa: BLE001 - 单个 Handler 异常不能吞
                if metadata.handler_full_name not in executed:
                    executed.append(metadata.handler_full_name)
                result.error = f"{metadata.handler_full_name}: {exc!r}"
                data["error_type"] = type(exc).__name__
                if isinstance(exc, shim.unsupported_error):
                    data["unsupported_capability"] = exc.capability
                if isinstance(exc, shim.provider_error_cls):
                    data["provider_error_code"] = exc.error_code
                log.warning(
                    "AstrBot plugin %s handler %s failed: %r",
                    state.plugin_id,
                    metadata.handler_full_name,
                    exc,
                )
                state.host.log(
                    "error",
                    f"plugin handler {metadata.handler_full_name} failed: {exc!r}",
                    source=f"astrbot:{state.plugin_id}",
                )
                # 与上游 StarRequestSubStage 一致：触发 on_plugin_error 钩子，然后终止事件传播
                await self.dispatch_event(
                    state,
                    "on_plugin_error",
                    {
                        "event": event,
                        "plugin_name": state.manifest.display_name,
                        "handler_name": metadata.handler_name,
                        "error": exc,
                        "traceback_text": traceback.format_exc(),
                    },
                )
                event.stop_event()
                break
        if not executed:
            # 没有任何 Handler 命中：交回桥接层
            return result
        result.handled = True
        if not result.command:
            result.command = _primary_command_name(shim, activated[0]) or activated[0].handler_name
        data["handlers"] = executed
        data["stopped"] = event.is_stopped()
        data["is_wake"] = event.is_wake
        if results:
            data["astrbot_results"] = results
        return result

    # -- 工具 / API / 事件 ------------------------------------------------- #

    async def _call_tool(self, state: _PluginState, name: str, args: dict[str, Any]) -> Any:
        """调用插件注册的 LLM 工具（与上游 ``FuncTool.call`` 同样的调用形式）。"""
        shim = state.shim
        tool = shim.llm_tools.get_func(name)
        if tool is None or tool.handler_module_path != state.module_name:
            raise BridgeError(f"AstrBot plugin '{state.plugin_id}' has no LLM tool '{name}'")
        event = self._build_bare_event(state)
        try:
            outcome = tool.handler(event, **args)
        except TypeError as exc:
            raise BridgeError(
                f"AstrBot tool '{name}' rejected arguments {sorted(args)}: {exc}",
            ) from exc
        except Exception as exc:  # noqa: BLE001 - 工具异常必须结构化上报
            raise _wrap_handler_error(state, f"tool:{name}", exc) from exc
        if inspect.isasyncgen(outcome):
            last: Any = None
            async for item in outcome:
                last = item
            outcome = last
        elif inspect.isawaitable(outcome):
            outcome = await outcome
        return _tool_result(outcome)

    async def _call_api(self, state: _PluginState, name: str, args: dict[str, Any]) -> Any:
        """AstrBot 插件没有可被桥接层调用的 API 面（``register_web_api`` 只是 HTTP 路由）。"""
        del args
        shim = state.shim
        shim.runtime.RUNTIME.record_unsupported(state.native_plugin_id, f"api.call:{name}")
        raise UnsupportedCapabilityError(
            f"astrbot.api:{name}",
            "AstrBot 插件只注册 HTTP 路由（register_web_api），N.E.K.O. 不承载这些路由",
        )

    async def dispatch_event(
        self,
        state: _PluginState,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """触发插件注册的事件钩子。"""
        shim = state.shim
        member = _EVENT_LOOKUP.get(_normalise_event_name(event_type))
        if member is None:
            raise UnsupportedCapabilityError(
                f"astrbot.event:{event_type}",
                "不是 AstrBot 的事件类型",
            )
        enum_value = getattr(shim.event_type, member)
        handlers = [
            item
            for item in state.handlers
            if item.event_type is enum_value and item.handler_module_path == state.module_name
        ]
        payload = dict(payload or {})
        results: list[dict[str, Any]] = []
        if not handlers:
            return {
                "event_type": _EVENT_NAME_BY_MEMBER[member],
                "handlers": [],
                "results": [],
            }
        available = self._hook_arguments(state, payload)
        for metadata in handlers:
            arguments = _hook_call_arguments(metadata, available, _HOOK_ARGS[member])
            try:
                outcome = metadata.handler(*arguments)
                if inspect.isawaitable(outcome):
                    outcome = await outcome
            except Exception as exc:  # noqa: BLE001 - 钩子异常不能打断桥接
                results.append({"handler": metadata.handler_name, "error": repr(exc)})
                state.host.log(
                    "error",
                    f"hook {metadata.handler_name} failed: {exc!r}",
                    source=f"astrbot:{state.plugin_id}",
                )
                continue
            results.append({"handler": metadata.handler_name, "result": _short_repr(outcome)})
        return {
            "event_type": _EVENT_NAME_BY_MEMBER[member],
            "handlers": [item.handler_name for item in handlers],
            "results": results,
        }

    def _hook_arguments(
        self,
        state: _PluginState,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        """准备钩子可能需要的对象（缺失的用 ``None``）。"""
        shim = state.shim
        context = payload.get("context")
        if isinstance(context, CommandContext):
            event = self._build_event(state, context, message_str=context.text, is_wake=True, raw_text=context.text)
        elif payload.get("event") is not None:
            event = payload["event"]
        else:
            event = self._build_bare_event(state)
        metadata = payload.get("metadata")
        if metadata is None:
            metadata = state.native.get("metadata", {})
        request_payload = payload.get("request")
        if isinstance(request_payload, Mapping):
            request = shim.provider_request_cls(**request_payload)
        elif request_payload is not None:
            request = request_payload
        else:
            request = shim.provider_request_cls()
        return {
            "event": event,
            "req": request,
            "response": _llm_response(shim, payload.get("response")),
            "metadata": metadata,
            "plugin_name": payload.get("plugin_name", state.manifest.display_name),
            "handler_name": payload.get("handler_name", ""),
            "error": payload.get("error"),
            "traceback_text": payload.get("traceback_text", ""),
            "tool": payload.get("tool"),
            "tool_args": payload.get("tool_args"),
            "tool_result": payload.get("tool_result"),
            "run_context": payload.get("run_context"),
        }

    # -- 事件构造 ---------------------------------------------------------- #

    def _build_event(
        self,
        state: _PluginState,
        ctx: CommandContext,
        *,
        message_str: str,
        is_wake: bool,
        raw_text: str,
    ) -> Any:
        """从桥接层上下文构造 AstrBot 事件。"""
        shim = state.shim
        message_obj = shim.astrbot_message_cls()
        message_obj.type = (
            shim.message_type.GROUP_MESSAGE if ctx.is_group else shim.message_type.FRIEND_MESSAGE
        )
        message_obj.self_id = str(ctx.raw.get("self_id", ""))
        session_id = (ctx.group_id if ctx.is_group else ctx.user_id) or "default"
        message_obj.session_id = session_id
        message_obj.message_id = str(ctx.raw.get("message_id", ""))
        message_obj.group = (
            shim.group_cls(group_id=ctx.group_id)
            if ctx.is_group and ctx.group_id
            else None
        )
        message_obj.sender = shim.message_member_cls(
            user_id=ctx.user_id or "",
            nickname=ctx.user_name or None,
        )
        message_obj.message = [shim.plain_cls(raw_text)]
        message_obj.message_str = raw_text
        message_obj.raw_message = dict(ctx.raw)
        event = shim.event_cls(
            message_str,
            message_obj,
            shim.platform_metadata_cls(
                name=_PLATFORM_NAME,
                description="N.E.K.O. bot_bridge",
                id=_PLATFORM_NAME,
            ),
            session_id,
        )
        event.role = "admin" if ctx.raw.get("is_admin") else "member"
        event.is_wake = is_wake
        event.is_at_or_wake_command = is_wake
        event.set_extra("neko_channel", ctx.channel)
        event.set_extra("neko_source", ctx.source)
        event._neko_attach_sender(_make_sender(state))
        return event

    def _build_bare_event(self, state: _PluginState) -> Any:
        """构造一个没有入站消息的事件（工具调用 / 无上下文钩子用）。"""
        shim = state.shim
        message_obj = shim.astrbot_message_cls()
        message_obj.type = shim.message_type.OTHER_MESSAGE
        message_obj.self_id = ""
        message_obj.session_id = ""
        message_obj.message_id = ""
        message_obj.group = None
        message_obj.sender = shim.message_member_cls(user_id="", nickname=None)
        message_obj.message = []
        message_obj.message_str = ""
        message_obj.raw_message = {}
        event = shim.event_cls(
            "",
            message_obj,
            shim.platform_metadata_cls(
                name=_PLATFORM_NAME,
                description="N.E.K.O. bot_bridge",
                id=_PLATFORM_NAME,
            ),
            "",
        )
        event.is_wake = True
        event.is_at_or_wake_command = True
        event._neko_attach_sender(_make_sender(state))
        return event


BACKEND = AstrBotBackend()


# --------------------------------------------------------------------------- #
# 模块级辅助函数
# --------------------------------------------------------------------------- #


def _safe_module_part(value: str) -> str:
    """把插件 id 变成合法的模块名片段。"""
    return "".join(ch if ch.isalnum() else "_" for ch in value) or "plugin"


def _purge_module(module_name: str) -> None:
    """从 ``sys.modules`` 移除插件模块（含子模块）。"""
    prefix = f"{module_name.split('.')[0]}"
    for name in [
        name for name in sys.modules if name == prefix or name.startswith(prefix + ".")
    ]:
        sys.modules.pop(name, None)


def _purge_plugin(shim: _Shim, module_name: str, native_plugin_id: str) -> None:
    """注销插件在 shim 注册表里的所有痕迹。"""
    shim.handlers.unload_module(module_name)
    shim.llm_tools.unload_module(module_name)
    shim.runtime.RUNTIME.unbind_plugin(native_plugin_id)
    _purge_module(module_name)


def _import_plugin_module(
    plugin_dir: Path,
    entry: Path,
    module_name: str,
    plugin_id: str,
) -> types.ModuleType:
    """把插件入口文件作为包内模块导入，使插件内部的相对导入可用。"""
    package_name = module_name.rsplit(".", 1)[0]
    package = types.ModuleType(package_name)
    package.__path__ = [str(plugin_dir)]  # type: ignore[attr-defined]
    package.__package__ = package_name
    package.__file__ = str(entry)
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(module_name, entry)
    if spec is None or spec.loader is None:
        raise PluginLoadError(plugin_id, f"无法为 {entry} 创建 import spec")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        _purge_module(module_name)
        raise PluginLoadError(plugin_id, f"导入插件模块失败：{exc!r}") from exc
    return module


def _find_plugin_class(
    shim: _Shim,
    module: types.ModuleType,
    module_name: str,
    plugin_id: str,
) -> type:
    """找到插件主类：优先 ``Star`` 子类，其次上游旧版的类名约定。"""
    metadata = shim.star_map.get(module_name)
    if metadata is not None and isinstance(metadata.star_cls_type, type):
        return metadata.star_cls_type
    candidates = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and getattr(value, "__module__", None) == module_name
        and issubclass(value, shim.star_cls)
    ]
    if candidates:
        preferred = [
            value
            for value in candidates
            if value.__name__.lower().endswith("plugin") or value.__name__.lower() == "main"
        ]
        return preferred[0] if preferred else candidates[0]
    legacy = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and getattr(value, "__module__", None) == module_name
        and (value.__name__.lower().endswith("plugin") or value.__name__.lower() == "main")
    ]
    if legacy:
        return legacy[0]
    raise PluginLoadError(
        plugin_id,
        "未找到插件类：请让主类继承 astrbot.api.star.Star，"
        "或让类名以 Plugin 结尾 / 命名为 Main",
    )


def _loop_hint(exc: BaseException) -> str:
    """插件在 ``__init__`` 里需要事件循环时，给调用方一句可执行的提示。"""
    if "no running event loop" not in str(exc):
        return ""
    return (
        "（上游在异步的 PluginManager.load 流程里实例化插件，"
        "这类插件需要从正在运行的事件循环里调用 load()）"
    )


def _instantiate_plugin(plugin_cls: type, context: Any, config: Any) -> Any:
    """实例化插件类。

    上游用 ``try: cls(context=..., config=...) except TypeError: cls(context=...)``；
    这里先看签名，避免把构造器内部的 ``TypeError`` 误当成"不接受 config"。
    """
    try:
        signature = inspect.signature(plugin_cls.__init__)
    except (TypeError, ValueError):
        signature = None
    accepts_config = False
    if signature is not None:
        accepts_config = "config" in signature.parameters or any(
            parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        )
    if accepts_config:
        return plugin_cls(context=context, config=config)
    return plugin_cls(context=context)


async def _terminate_instance(state: _PluginState) -> None:
    """按上游顺序执行卸载：类自己定义了 ``__del__`` 就调它，否则调 ``terminate``。"""
    instance = state.instance
    klass = type(instance)
    if "__del__" in klass.__dict__:
        del_hook = klass.__dict__["__del__"]
        outcome = del_hook(instance)
        if inspect.isawaitable(outcome):
            await outcome
        return
    if "terminate" in klass.__dict__:
        outcome = instance.terminate()
        if inspect.isawaitable(outcome):
            await outcome


def _wake_prefixes(settings: Mapping[str, Any] | None) -> tuple[str, ...]:
    """取唤醒前缀（默认 ``/``，与上游 ``wake_prefix: ["/"]`` 一致）。

    显式配置空字符串（或空列表）表示"群聊也不需要唤醒前缀"。
    """
    if not settings:
        return ("/",)
    value: Any = None
    for key in ("astrbot_wake_prefix", "wake_prefix", "wake_prefixes"):
        if key in settings:
            value = settings[key]
            break
    if value is None:
        return ("/",)
    if isinstance(value, str):
        return (value,) if value else ()
    if isinstance(value, Sequence):
        return tuple(str(item) for item in value if str(item))
    log.warning("AstrBot wake prefix setting %r is not a string or list; using '/'", value)
    return ("/",)


def _make_sender(state: _PluginState) -> Callable[[str, str, tuple[str, ...]], Awaitable[bool]]:
    """构造 ``event.send`` / ``context.send_message`` 用的立即发送回调。"""

    async def sender(text: str, umo: str, dropped: tuple[str, ...]) -> bool:
        return await state.host.send_chat(
            text,
            source=f"astrbot:{state.plugin_id}",
            metadata={
                "framework": "astrbot",
                "plugin_id": state.plugin_id,
                "umo": umo,
                "dropped_components": list(dropped),
                "immediate": True,
            },
        )

    return sender


async def _maybe_await(value: Any) -> Any:
    """``await`` 一个可能是协程的结果。"""
    if inspect.isawaitable(value):
        return await value
    return value


async def _iterate_handler(
    event: Any,
    handler: Callable[..., Any],
    params: Mapping[str, Any],
) -> Any:
    """执行 Handler，逐个产出它的结果（协程只产出一次）。"""
    outcome = handler(event, **dict(params))
    if inspect.isasyncgen(outcome):
        async for item in outcome:
            yield item
        return
    if inspect.isawaitable(outcome):
        yield await outcome
        return
    yield outcome


def _collect_result(produced: Any, result: CommandResult) -> list[dict[str, Any]]:
    """把 Handler 产出的结果收进 ``CommandResult``。

    文本进 ``replies``；消息链里的所有消息段（图片 / At / 表情等）以字典形式返回，
    由调用方放进 ``CommandResult.data``——N.E.K.O. 的 ``replies`` 只承载文本，非文本
    消息段不能假装发送成功，所以在这里如实保留结构。
    """
    chain = getattr(produced, "chain", None)
    if chain is None:
        text = produced if isinstance(produced, str) else str(produced)
        if text:
            result.replies.append(text)
        return []
    get_plain_text = getattr(produced, "get_plain_text", None)
    text = str(get_plain_text()) if callable(get_plain_text) else ""
    if text and text.strip():
        result.replies.append(text)
    payloads = [
        component.toDict() for component in chain if hasattr(component, "toDict")
    ]
    if payloads:
        return [{"chain": payloads}]
    return []


def _tool_result(outcome: Any) -> Any:
    """把工具产出归一成上游的约定：``str``（进 prompt）或 ``None``。"""
    if outcome is None:
        return None
    if isinstance(outcome, str):
        return outcome
    get_plain_text = getattr(outcome, "get_plain_text", None)
    if callable(get_plain_text):
        text = str(get_plain_text())
        return text or None
    return str(outcome)


def _wrap_handler_error(state: _PluginState, target: str, exc: BaseException) -> BridgeError:
    """把插件抛出的异常翻译成桥接层错误。"""
    shim = state.shim
    if isinstance(exc, shim.unsupported_error):
        return UnsupportedCapabilityError(exc.capability, exc.detail)
    return BridgeError(f"AstrBot plugin '{state.plugin_id}' {target} failed: {exc!r}")


def _hook_call_arguments(
    metadata: Any,
    available: Mapping[str, Any],
    names: tuple[str, ...],
) -> list[Any]:
    """按 Handler 的签名决定传几个参数（上游钩子的参数个数是固定的）。"""
    try:
        signature = inspect.signature(metadata.handler)
    except (TypeError, ValueError):
        positional = len(names)
        has_varargs = False
    else:
        positional = 0
        has_varargs = False
        for parameter in signature.parameters.values():
            if parameter.kind in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
            ):
                positional += 1
            elif parameter.kind is inspect.Parameter.VAR_POSITIONAL:
                has_varargs = True
    count = len(names) if has_varargs else min(positional, len(names))
    return [available.get(name) for name in names[:count]]


def _short_repr(value: Any) -> str:
    """给返回值一个短的可读表示（写进 ``dispatch_event`` 的结果）。"""
    if value is None:
        return "None"
    text = repr(value)
    return text if len(text) <= 200 else f"{text[:200]}...<{len(text)} chars>"


def _spec_description(desc: str, constraints: Sequence[str]) -> str:
    """能力描述 = Handler docstring 首行（+ 过滤器约束）。"""
    first_line = desc.splitlines()[0].strip() if desc else ""
    if not constraints:
        return first_line
    suffix = "[" + "; ".join(constraints) + "]"
    return f"{first_line} {suffix}".strip()


def _llm_response(shim: _Shim, value: Any) -> Any:
    """把 ``dispatch_event`` 负载里的 ``response`` 变成 ``LLMResponse``。"""
    if not isinstance(value, Mapping):
        return value
    text = str(value.get("text") or value.get("response") or value.get("completion_text") or "")
    reasoning = value.get("reasoning") or value.get("reasoning_content") or None
    return shim.llm_response_cls(
        role=str(value.get("role") or "assistant"),
        completion_text=text,
        reasoning_content=str(reasoning) if reasoning else None,
        raw_completion=dict(value),
    )


def _filter_constraints(shim: _Shim, filters: Iterable[Any]) -> list[str]:
    """把 Handler 上的过滤器描述成 ``key=value`` 文本（给 ``EventSpec.description``）。"""
    described: list[str] = []
    for handler_filter in filters:
        if isinstance(handler_filter, shim.event_message_type_filter_cls):
            described.append(f"message_type={handler_filter.event_message_type.name}")
        elif isinstance(handler_filter, shim.platform_adapter_filter_cls):
            platform = handler_filter.platform_type
            described.append(f"platform={platform.name if platform is not None else 'unknown'}")
        elif isinstance(handler_filter, shim.permission_filter_cls):
            described.append(f"permission={handler_filter.permission_type.name}")
    return described


def _primary_command_name(shim: _Shim, metadata: Any) -> str:
    """取 Handler 的首个完整指令名（``math add`` 这种）；正则 Handler 返回空串。"""
    for handler_filter in metadata.event_filters:
        if isinstance(handler_filter, shim.command_filter_cls):
            names = handler_filter.get_complete_command_names()
            if names:
                return names[0]
    return ""


def _command_parameters(shim: _Shim, handler_filter: Any) -> dict[str, Any] | None:
    """把指令参数解析结果整理成 JSON 风格的描述。"""
    params = getattr(handler_filter, "handler_params", None)
    if not params:
        return None
    described: dict[str, Any] = {}
    for name, declared in params.items():
        if declared is shim.greedy_str_cls:
            described[name] = {"type": "string", "required": True, "greedy": True}
        elif isinstance(declared, type):
            json_type = PY_TO_JSON_TYPE.get(declared.__name__, declared.__name__)
            described[name] = {"type": json_type, "required": True}
        elif declared is inspect.Parameter.empty:
            described[name] = {"type": "string", "required": True}
        elif declared is None:
            described[name] = {"type": "string", "required": False}
        else:
            described[name] = {
                "type": PY_TO_JSON_TYPE.get(type(declared).__name__, "string"),
                "required": False,
                "default": declared if isinstance(declared, (str, int, float, bool)) else str(declared),
            }
    return described


def _build_specs(
    state: _PluginState,
) -> tuple[list[CommandSpec], list[ToolSpec], list[EventSpec], list[ApiSpec]]:
    """从 shim 注册表整理出四类能力描述。"""
    shim = state.shim
    prefixes = _wake_prefixes(state.host.settings)
    default_prefix = prefixes[0] if prefixes else ""
    commands: list[CommandSpec] = []
    events: list[EventSpec] = []
    for metadata in state.handlers:
        if metadata.handler_module_path != state.module_name:
            continue
        if metadata.event_type is shim.calling_func_tool_event:
            continue
        description = _spec_description(
            metadata.desc,
            _filter_constraints(shim, metadata.event_filters),
        )
        priority = int(metadata.extras_configs.get("priority", 0))
        if metadata.event_type is not shim.adapter_message_event:
            events.append(
                EventSpec(
                    event_type=_EVENT_NAME_BY_MEMBER[metadata.event_type.name],
                    description=description,
                    blocking=False,
                    priority=priority,
                ),
            )
            continue
        if any(
            isinstance(item, shim.command_group_filter_cls)
            for item in metadata.event_filters
        ):
            # 指令组函数本身不对外暴露成指令（它只在缺子指令时抛错展示指令树）
            continue
        command_filter = next(
            (
                item
                for item in metadata.event_filters
                if isinstance(item, shim.command_filter_cls)
            ),
            None,
        )
        if command_filter is not None:
            names = command_filter.get_complete_command_names()
            commands.append(
                CommandSpec(
                    id=metadata.handler_name,
                    name=names[0] if names else metadata.handler_name,
                    description=description,
                    patterns=tuple(names) or (metadata.handler_name,),
                    usage=f"{default_prefix}{names[0]}" if names else "",
                    aliases=tuple(names[1:]),
                    priority=priority,
                    parameters=_command_parameters(shim, command_filter),
                ),
            )
            continue
        regex_filter = next(
            (
                item
                for item in metadata.event_filters
                if isinstance(item, shim.regex_filter_cls)
            ),
            None,
        )
        if regex_filter is not None:
            commands.append(
                CommandSpec(
                    id=metadata.handler_name,
                    name=metadata.handler_name,
                    description=description,
                    patterns=(regex_filter.regex_str,),
                    priority=priority,
                ),
            )
            continue
        # 纯消息监听器（只有 event_message_type / permission / platform 过滤）
        events.append(
            EventSpec(
                event_type=_EVENT_NAME_BY_MEMBER["AdapterMessageEvent"],
                description=description,
                blocking=False,
                priority=priority,
            ),
        )

    tools: list[ToolSpec] = []
    for registration in shim.llm_tools.get_by_module(state.module_name):
        if not getattr(registration, "active", True):
            continue
        tools.append(
            ToolSpec(
                name=registration.name,
                description=registration.description,
                parameters=registration.parameters,
                timeout=30.0,
                origin=f"astrbot:{state.plugin_id}",
            ),
        )

    apis: list[ApiSpec] = []
    for route, _view, methods, desc in getattr(state.context, "registered_web_apis", []):
        apis.append(
            ApiSpec(
                name=str(route),
                description=str(desc),
                version=str(state.manifest.version),
                parameters={"methods": list(methods)},
            ),
        )
    return commands, tools, events, apis


__all__ = ["BACKEND", "AstrBotBackend"]
