"""bot_bridge 的离线测试夹具。

两条原则：

1. **用真 SDK**：``plugin.sdk.plugin`` / ``plugin.sdk.adapter`` 从 N.E.K.O. 源码树
   （``NEKO_SOURCE_ROOT`` 指定的目录）导入，而不是手写桩 —— 这样装饰器、Result
   类型、PluginConfig、适配器基类的行为都是真的。做法见 :func:`install_import_shims`：
   在临时目录里放一个 ``plugin`` 包壳，把 ``__path__`` 同时指向源码树的 ``plugin``
   和本插件的父目录。
2. **假宿主**：只有宿主 ``ctx`` 是假的（:class:`MockHostContext`），它记录所有
   ``push_message`` 调用、可注入模型响应、可注入总线记录 —— 与 N.E.K.O. 仓库自带
   测试（``plugin/tests/unit/plugins/test_mcp_adapter_runtime.py``）的做法一致。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import types
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 本插件目录（tests/ 的上一级）。
PLUGIN_DIR = Path(__file__).resolve().parent.parent


def _resolve_source_root() -> str:
    """定位 N.E.K.O. 源码树（必须含 ``plugin/sdk/``）。

    优先级：``NEKO_SOURCE_ROOT`` 环境变量 → 常见本机布局 → 报一段能照做的错误。
    以前这里写死了一个临时目录 ``Temp/neko-files``，那个目录一旦不存在，
    ``pytest tests -q`` 就只剩一句 ``ModuleNotFoundError: No module named 'plugin.sdk'``，
    看不出该去设哪个变量 —— 所以改成「找不到就指名道姓」。
    """
    env = os.environ.get("NEKO_SOURCE_ROOT")
    if env:
        root = Path(env)
        if (root / "plugin" / "sdk").is_dir():
            return str(root)
        raise RuntimeError(
            f"NEKO_SOURCE_ROOT={env} 里没有 plugin/sdk/，不是 N.E.K.O. 源码树。"
        )

    local_appdata = os.environ.get("LOCALAPPDATA") or ""
    candidates = [
        Path(local_appdata) / "N.E.K.O" / "plugins" / "_host_sdk" if local_appdata else None,
        Path(local_appdata) / "N.E.K.O" / "plugins" / "_host" / "N.E.K.O-main" if local_appdata else None,
        Path(local_appdata) / "N.E.K.O" / "plugins" / "_host_official" / "N.E.K.O-main" if local_appdata else None,
        PLUGIN_DIR.parent / "_host_sdk",
        PLUGIN_DIR.parent / "_host" / "N.E.K.O-main",
    ]
    for cand in candidates:
        if cand and (cand / "plugin" / "sdk").is_dir():
            return str(cand)

    raise RuntimeError(
        "找不到 N.E.K.O. 源码树（需要其中存在 plugin/sdk/）。\n"
        "设置环境变量后再跑：\n"
        '  NEKO_SOURCE_ROOT="C:/path/to/N.E.K.O" python -m pytest tests -q\n'
        f"已尝试：{', '.join(str(c) for c in candidates if c)}"
    )


#: N.E.K.O. 源码树。可用环境变量覆盖（CI / 别的机器）。
NEKO_SOURCE_ROOT = _resolve_source_root()


def install_import_shims() -> Path:
    """把本插件挂成 ``plugin.plugins.bot_bridge``，并让 ``plugin.sdk`` 指向真 SDK。

    Returns:
        生成的 shim 根目录（调用方需要把它插到 ``sys.path`` 最前面）。
    """
    shim_root = Path(tempfile.gettempdir()) / "bot_bridge_test_shim"
    plugin_pkg = shim_root / "plugin"
    plugins_pkg = plugin_pkg / "plugins"
    plugins_pkg.mkdir(parents=True, exist_ok=True)
    source_plugin_dir = Path(NEKO_SOURCE_ROOT) / "plugin"
    (plugin_pkg / "__init__.py").write_text(
        "import os\n"
        "_H = os.path.dirname(os.path.abspath(__file__))\n"
        "__path__ = [_H, %r]\n" % str(source_plugin_dir),
        encoding="utf-8",
    )
    (plugins_pkg / "__init__.py").write_text(
        "import os\n"
        "_H = os.path.dirname(os.path.abspath(__file__))\n"
        "__path__ = [_H, %r]\n" % str(PLUGIN_DIR.parent),
        encoding="utf-8",
    )
    # 顺序很重要：shim 必须排在源码树前面，否则真的 plugin/__init__.py 会先被执行，
    # 把整个宿主运行时（zmq / ormsgpack / …）拖进测试进程。
    for entry in (str(NEKO_SOURCE_ROOT), str(shim_root)):
        if entry not in sys.path:
            sys.path.insert(0, entry)
    return shim_root


# --------------------------------------------------------------------------- #
# 假宿主
# --------------------------------------------------------------------------- #


@dataclass
class PushRecord:
    source: str
    visibility: list[str]
    ai_behavior: str
    parts: list[dict[str, Any]]
    priority: int
    metadata: dict[str, Any]


@dataclass
class MockLogger:
    lines: list[tuple[str, str]] = field(default_factory=list)

    def _record(self, level: str, message: object, *args: object, **kwargs: object) -> None:
        text = str(message)
        if args:
            text = f"{text} {args}"
        self.lines.append((level, text))

    def debug(self, message: object, *args: object, **kwargs: object) -> None:
        self._record("debug", message, *args)

    def info(self, message: object, *args: object, **kwargs: object) -> None:
        self._record("info", message, *args)

    def warning(self, message: object, *args: object, **kwargs: object) -> None:
        self._record("warning", message, *args)

    def error(self, message: object, *args: object, **kwargs: object) -> None:
        self._record("error", message, *args)

    def exception(self, message: object, *args: object, **kwargs: object) -> None:
        self._record("exception", message, *args)

    def text(self) -> str:
        return "\n".join(f"{level}: {message}" for level, message in self.lines)


@dataclass
class MockBusRecord:
    payload: dict[str, Any]

    def dump(self) -> dict[str, Any]:
        return dict(self.payload)


class _MockBusNamespace:
    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._records = records
        self.calls: list[dict[str, Any]] = []

    def get(self, **kwargs: Any) -> list[MockBusRecord]:
        self.calls.append(dict(kwargs))
        since = kwargs.get("since_ts")
        selected = [
            item
            for item in self._records
            if since is None or float(item.get("timestamp") or 0.0) > float(since)
        ]
        limit = int(kwargs.get("max_count") or len(selected) or 1)
        return [MockBusRecord(item) for item in selected[-limit:]]

    def feed(self, record: dict[str, Any]) -> None:
        self._records.append(record)


class MockBus:
    def __init__(self) -> None:
        self.messages = _MockBusNamespace([])
        self.conversations = _MockBusNamespace([])
        self.events = _MockBusNamespace([])
        self.lifecycle = _MockBusNamespace([])
        self.frames = _MockBusNamespace([])
        self.memory = _MockBusNamespace([])


class _MockCompletionMessage:
    def __init__(self, content: str, tool_calls: list[Any] | None = None) -> None:
        self.content = content
        self.reasoning_content = ""
        self.tool_calls = tool_calls or []


class _MockChoice:
    def __init__(self, content: str) -> None:
        self.message = _MockCompletionMessage(content)


class _MockCompletion:
    def __init__(self, content: str, model: str) -> None:
        self.choices = [_MockChoice(content)]
        self.model = model


class _MockEmbeddingItem:
    def __init__(self, vector: list[float]) -> None:
        self.embedding = vector


class _MockEmbeddingResponse:
    def __init__(self, count: int) -> None:
        self.data = [_MockEmbeddingItem([0.1, 0.2, 0.3]) for _ in range(count)]


class _MockChatCompletions:
    def __init__(self, owner: "MockLLMClient") -> None:
        self._owner = owner

    async def create(self, **kwargs: Any) -> _MockCompletion:
        self._owner.calls.append(dict(kwargs))
        if self._owner.fail_with is not None:
            raise self._owner.fail_with
        return _MockCompletion(self._owner.reply, str(kwargs.get("model") or "mock"))


class _MockEmbeddings:
    def __init__(self, owner: "MockLLMClient") -> None:
        self._owner = owner

    async def create(self, **kwargs: Any) -> _MockEmbeddingResponse:
        self._owner.calls.append(dict(kwargs))
        if self._owner.fail_with is not None:
            raise self._owner.fail_with
        return _MockEmbeddingResponse(len(list(kwargs.get("input") or [])))


class MockLLMClient:
    """最小可用的 OpenAI 兼容客户端（只实现桥接层用到的那两个方法）。"""

    def __init__(self, reply: str = "mock-reply", fail_with: BaseException | None = None) -> None:
        self.reply = reply
        self.fail_with = fail_with
        self.calls: list[dict[str, Any]] = []
        self.chat = types.SimpleNamespace(completions=_MockChatCompletions(self))
        self.embeddings = _MockEmbeddings(self)


class MockModels:
    def __init__(self, client: MockLLMClient | None = None) -> None:
        self.client = client or MockLLMClient()
        self.get_client_calls = 0

    def get_client(self) -> MockLLMClient:
        self.get_client_calls += 1
        return self.client


class MockPlugins:
    def __init__(self) -> None:
        self.list_calls = 0
        self.entry_calls: list[tuple[str, dict[str, Any]]] = []
        self.entry_result: Any = {"success": True}

    async def list(self, timeout: float = 5.0) -> list[dict[str, Any]]:
        self.list_calls += 1
        return [{"id": "web_search", "type": "plugin"}, {"id": "bot_bridge", "type": "adapter"}]

    async def call_entry(self, reference: str, params: Mapping[str, Any] | None = None, *, timeout: float = 30.0) -> Any:
        self.entry_calls.append((reference, dict(params or {})))
        return self.entry_result


class MockHostContext:
    """宿主 ``ctx`` 的假实现。

    方法名必须覆盖 ``plugin/sdk/shared/core/context.py::_SDK_CONTEXT_METHOD_NAMES``，
    否则真 SDK 会把 ctx 包进 ``SdkContext``（那也能跑，但断言会绕一层）。
    """

    plugin_id = "bot_bridge"
    metadata: dict[str, Any] = {}
    images = None
    message_queue = None
    handler_ctx = None
    current_entry_id = None
    run_id = None

    def __init__(self, effective_config: dict[str, Any] | None = None) -> None:
        self.logger = MockLogger()
        self.bus = MockBus()
        self.models = MockModels()
        self.plugins = MockPlugins()
        self.pushes: list[PushRecord] = []
        self.status_updates: list[dict[str, Any]] = []
        self.tmpdir = Path(tempfile.mkdtemp(prefix="bot_bridge_ctx_"))
        self.config_path = self.tmpdir / "plugin.toml"
        self._effective_config = effective_config or {
            "plugin": {"store": {"enabled": False}, "database": {"enabled": False}},
            "plugin_state": {"backend": "memory"},
        }

    # ---- 能力面 ---------------------------------------------------------- #

    def push_message(self, **kwargs: Any) -> dict[str, Any]:
        record = PushRecord(
            source=str(kwargs.get("source") or ""),
            visibility=list(kwargs.get("visibility") or []),
            ai_behavior=str(kwargs.get("ai_behavior") or ""),
            parts=list(kwargs.get("parts") or []),
            priority=int(kwargs.get("priority") or 0),
            metadata=dict(kwargs.get("metadata") or {}),
        )
        joined = " ".join(str(part.get("text") or "") for part in record.parts)
        if len(joined.encode("utf-8")) > 512 * 1024:
            return {"submitted": False, "reason": "payload_too_large"}
        self.pushes.append(record)
        return {"submitted": True, "reason": "", "message_id": f"m{len(self.pushes)}"}

    def update_status(self, status: Mapping[str, Any]) -> None:
        self.status_updates.append(dict(status))

    def get_current_entry_meta(self) -> None:
        return None

    def require_run_id(self) -> str:
        return "run-test"

    async def get_own_config(self, timeout: float = 5.0) -> dict[str, Any]:
        return {"config": dict(self._effective_config)}

    async def get_own_base_config(self, timeout: float = 5.0) -> dict[str, Any]:
        return {"config": dict(self._effective_config)}

    async def get_own_profiles_state(self, timeout: float = 5.0) -> dict[str, Any]:
        return {"profiles": {}, "active": None}

    async def get_own_profile_config(self, profile_name: str, timeout: float = 5.0) -> dict[str, Any]:
        return {"config": dict(self._effective_config)}

    async def get_own_effective_config(
        self,
        profile_name: str | None = None,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        return dict(self._effective_config)

    async def update_own_config(self, updates: Mapping[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        # 真机是**深合并**（`config.update({"bridge": {...}})` 不会把 bridge 段其它键抹掉）——
        # 夹具必须同语义，否则测试会给出「plugins_root/data_root 消失」这种假故障。
        for key, value in updates.items():
            name = str(key)
            current = self._effective_config.get(name)
            if isinstance(current, dict) and isinstance(value, Mapping):
                merged = dict(current)
                merged.update(dict(value))
                self._effective_config[name] = merged
            else:
                self._effective_config[name] = value
        return {"config": dict(self._effective_config)}

    async def replace_own_config(self, config: Mapping[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        self._effective_config = dict(config)
        return {"config": dict(self._effective_config)}

    async def upsert_own_profile_config(
        self,
        profile_name: str,
        config: Mapping[str, Any],
        *,
        make_active: bool = False,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        return {"config": dict(config)}

    async def delete_own_profile_config(self, profile_name: str, timeout: float = 10.0) -> dict[str, Any]:
        return {"deleted": True}

    async def set_own_active_profile(self, profile_name: str, timeout: float = 10.0) -> dict[str, Any]:
        return {"active": profile_name}

    async def query_plugins(self, filters: Mapping[str, Any] | None = None, timeout: float = 5.0) -> dict[str, Any]:
        return {"plugins": await self.plugins.list()}

    async def trigger_plugin_event(self, **kwargs: Any) -> dict[str, Any]:
        self.plugins.entry_calls.append((str(kwargs.get("target_plugin_id") or ""), dict(kwargs)))
        return dict(self.plugins.entry_result) if isinstance(self.plugins.entry_result, Mapping) else {"success": True}

    async def get_system_config(self, timeout: float = 5.0) -> dict[str, Any]:
        return {}

    async def query_memory(self, bucket_id: str, query: str, timeout: float = 5.0) -> list[dict[str, Any]]:
        return []

    async def run_update(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "kwargs": kwargs}

    async def run_update_async(self, **kwargs: Any) -> dict[str, Any]:
        return await self.run_update(**kwargs)

    async def export_push(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "kwargs": kwargs}

    async def export_push_async(self, **kwargs: Any) -> dict[str, Any]:
        return await self.export_push(**kwargs)

    async def finish(self, **kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "kwargs": kwargs}

    async def create_card(self, **kwargs: Any) -> None:
        return None

    def get_card(self, card_id: str, **kwargs: Any) -> None:
        return None

    async def create_view(self, **kwargs: Any) -> None:
        return None

    def get_view(self, view_id: str, **kwargs: Any) -> None:
        return None

    def set_own_active_profile_sync(self, profile_name: str) -> None:
        return None

    # ---- 断言辅助 -------------------------------------------------------- #

    def pushed_texts(self) -> list[str]:
        texts: list[str] = []
        for record in self.pushes:
            for part in record.parts:
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
        return texts


# --------------------------------------------------------------------------- #
# 插件加载
# --------------------------------------------------------------------------- #


async def instantiate_plugin(
    *,
    config: Mapping[str, Any] | None = None,
    plugins_root: Path | None = None,
    framework_settings: Mapping[str, Any] | None = None,
) -> tuple[Any, MockHostContext]:
    """构造 ``BotBridgePlugin``（不跑 lifecycle）并返回实例与假 ctx。"""
    install_import_shims()
    effective: dict[str, Any] = {
        "plugin": {"store": {"enabled": False}, "database": {"enabled": False}},
        "plugin_state": {"backend": "memory"},
    }
    context = MockHostContext(effective)
    # 离线测试绝不许写进宿主真实数据目录（否则会在装好的插件目录里留下状态目录）。
    # 放在最后、且用赋值而不是 setdefault：测试常常整体替换 bridge 段，注入要能兜住。
    bridge_section = effective.get("bridge")
    if not isinstance(bridge_section, dict):
        bridge_section = {}
        effective["bridge"] = bridge_section
    bridge_section["data_root"] = str(context.tmpdir / "bridge_data")
    if config:
        effective["bridge"] = dict(config)
    context._effective_config = effective
    if plugins_root is not None:
        bridge = dict(effective.get("bridge") or {})
        bridge["plugins_root"] = str(plugins_root)
        bridge.setdefault("continue_on_error", True)
        if framework_settings:
            bridge.update(dict(framework_settings))
        effective["bridge"] = bridge
        context._effective_config = effective

    module = _import_plugin_package()
    plugin = module.BotBridgePlugin(context)
    plugin.logger = context.logger
    return plugin, context


def _import_plugin_package() -> types.ModuleType:
    import importlib

    return importlib.import_module("plugin.plugins.bot_bridge")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


__all__ = [
    "DEFAULT_NEKO_SOURCE_ROOT",
    "MockBus",
    "MockHostContext",
    "MockLLMClient",
    "MockLogger",
    "MockPlugins",
    "NEKO_SOURCE_ROOT",
    "PLUGIN_DIR",
    "PushRecord",
    "install_import_shims",
    "instantiate_plugin",
    "run",
    "write_json",
]

_ = Sequence
