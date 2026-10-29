"""bot_bridge AstrBot 兼容层测试。

全部是同步测试 + ``asyncio.run``：这样不依赖 ``pytest-asyncio``（N.E.K.O. 的冻结
解释器里不保证有它），同时仍然真实地驱动协程。没有网络、没有 sleep。

覆盖：``detect`` 的识别与拒绝、``load`` 的能力抽取、消息处理（指令 / 指令组 /
正则 / 权限 / 自定义过滤器 / 监听器 / 事件传播）、主动发送与模型调用到
``HostServices`` 的映射、LLM 工具调用、钩子分发、生命周期、键值存储、结构化
"能力缺失"失败、以及旧版插件路径。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
import warnings
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = PLUGIN_ROOT / "bridge"
BACKENDS_DIR = PLUGIN_DIR / "backends"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "astrbot"


def _import_backend_module() -> Any:
    """导入 ``bridge.backends.astrbot``（与其它兼容层测试同一套引导方式）。

    先按正常包路径导入；若 ``bridge/__init__.py``（桥接核心维护，会 import host /
    registry / router）当前不可导入，就注册最小命名空间包后重试 —— 测试只关心后端
    模块本身，不触发 ``load_backends()``。
    """
    root = str(PLUGIN_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        return importlib.import_module("bridge.backends.astrbot")
    except ImportError:
        for name, path in (("bridge", PLUGIN_DIR), ("bridge.backends", BACKENDS_DIR)):
            if name in sys.modules:
                continue
            namespace = types.ModuleType(name)
            namespace.__path__ = [str(path)]  # type: ignore[attr-defined]
            sys.modules[name] = namespace
        return importlib.import_module("bridge.backends.astrbot")


backend_module = _import_backend_module()
_BRIDGE_PACKAGE = str(backend_module.__package__).rsplit(".", 1)[0]
contracts = importlib.import_module(f"{_BRIDGE_PACKAGE}.contracts")
errors = importlib.import_module(f"{_BRIDGE_PACKAGE}.errors")

BACKEND = backend_module.BACKEND
CommandContext = contracts.CommandContext
HostServices = contracts.HostServices
PluginManifest = contracts.PluginManifest
BridgeError = errors.BridgeError
PluginLoadError = errors.PluginLoadError
PluginNotLoadedError = errors.PluginNotLoadedError
UnsupportedCapabilityError = errors.UnsupportedCapabilityError

LLM_REPLY = "echo:{prompt}"


@pytest.fixture(autouse=True)
def _ignore_deprecation_warnings() -> Any:
    """旧版插件 fixture 会触发 ``register_star`` 的 DeprecationWarning。

    在这里统一压制，测试结果不受 pytest 的 ``-W`` 配置影响。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        yield


class FakeHost:
    """测试用的 HostServices 实现（记录所有调用）。"""

    def __init__(
        self,
        root: Path,
        *,
        settings: Mapping[str, Any] | None = None,
        configs: Mapping[str, Mapping[str, Any]] | None = None,
        fail_llm: bool = False,
    ) -> None:
        self.root = Path(root)
        self._settings = dict(settings or {})
        self._configs = {key: dict(value) for key, value in (configs or {}).items()}
        self.fail_llm = fail_llm
        self.sent: list[dict[str, Any]] = []
        self.llm_calls: list[dict[str, Any]] = []
        self.logs: list[tuple[str, str, str]] = []
        self.saved: dict[str, dict[str, Any]] = []

    # -- HostServices ------------------------------------------------------ #

    def log(self, level: str, message: str, *, source: str = "") -> None:
        self.logs.append((level, message, source))

    def data_dir(self, plugin_id: str) -> Path:
        path = self.root / "data" / plugin_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cache_dir(self, plugin_id: str) -> Path:
        path = self.root / "cache" / plugin_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def settings(self) -> Mapping[str, Any]:
        return dict(self._settings)

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
        self.sent.append(
            {
                "text": text,
                "source": source,
                "metadata": dict(metadata or {}),
                "priority": priority,
                "ai_behavior": ai_behavior,
                "visibility": list(visibility or []),
            },
        )
        return True

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
        self.llm_calls.append(
            {
                "prompt": prompt,
                "model": model,
                "system": system,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "tools": tools,
            },
        )
        if self.fail_llm:
            return {
                "success": False,
                "response": "",
                "reasoning": "",
                "model": "",
                "tool_calls": [],
                "error": "model_usage_not_bound",
                "code": "model_usage_not_bound",
            }
        text = prompt if isinstance(prompt, str) else json.dumps(prompt, ensure_ascii=False)
        return {
            "success": True,
            "response": LLM_REPLY.format(prompt=text),
            "reasoning": "because",
            "model": model or "fake-model",
            "tool_calls": [],
            "error": "",
        }

    async def llm_embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise UnsupportedCapabilityError("llm.embed", "fake host has no embedding model")

    async def call_plugin_entry(
        self,
        plugin_id: str,
        entry_id: str,
        params: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        return {"plugin_id": plugin_id, "entry_id": entry_id, "params": dict(params or {})}

    async def list_plugins(self) -> list[dict[str, Any]]:
        return []

    async def recent_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        del limit
        return []

    async def plugin_config(self, plugin_id: str) -> dict[str, Any]:
        return dict(self._configs.get(plugin_id, {}))

    async def save_plugin_config(self, plugin_id: str, data: Mapping[str, Any]) -> bool:
        self.saved.append({"plugin_id": plugin_id, "data": dict(data)})
        self._configs[plugin_id] = dict(data)
        return True


# --------------------------------------------------------------------------- #
# 帮助函数
# --------------------------------------------------------------------------- #


def _run(coro: Any) -> Any:
    """同步驱动一个协程（不依赖 pytest-asyncio）。"""
    return asyncio.run(coro)


def _detect(name: str) -> PluginManifest:
    manifest = BACKEND.detect(FIXTURES / name)
    assert manifest is not None, f"{name} should be detected as an AstrBot plugin"
    return manifest


def _make_host(tmp_path: Path, **kwargs: Any) -> FakeHost:
    return FakeHost(tmp_path, **kwargs)


def _load(
    name: str,
    tmp_path: Path,
    **host_kwargs: Any,
) -> tuple[FakeHost, Any]:
    host = _make_host(tmp_path, **host_kwargs)
    plugin = BACKEND.load(_detect(name), host)
    return host, plugin


def _context(
    text: str,
    *,
    user_id: str = "u1",
    user_name: str = "Neko",
    group_id: str = "",
    is_group: bool = False,
    raw: Mapping[str, Any] | None = None,
) -> CommandContext:
    return CommandContext(
        text=text,
        user_id=user_id,
        user_name=user_name,
        group_id=group_id,
        is_group=is_group,
        raw=dict(raw or {}),
    )


def _handler_names(result: Any) -> list[str]:
    return [str(name) for name in result.data.get("handlers", [])]


# --------------------------------------------------------------------------- #
# detect
# --------------------------------------------------------------------------- #


def test_detect_parses_real_metadata_layout() -> None:
    manifest = _detect("hello_astrbot")
    assert manifest.framework == "astrbot"
    assert manifest.plugin_id == "hello_astrbot"
    assert manifest.name == "hello_astrbot"
    assert manifest.version == "1.2.3"
    assert manifest.description.startswith("bot_bridge AstrBot 兼容层")
    assert manifest.entry == "main.py"
    assert manifest.path == FIXTURES / "hello_astrbot"
    assert manifest.extra["author"] == "neko-tests"
    assert manifest.extra["astrbot_plugin_id"] == "neko-tests/hello_astrbot"
    assert manifest.extra["metadata_file"] == "metadata.yaml"
    assert manifest.extra["support_platforms"] == ["aiocqhttp", "telegram"]


def test_detect_rejects_non_astrbot_directories() -> None:
    assert BACKEND.detect(FIXTURES / "not_astrbot") is None
    assert BACKEND.detect(FIXTURES / "broken_astrbot") is None
    assert BACKEND.detect(FIXTURES / "bad_metadata_astrbot") is None
    assert BACKEND.detect(FIXTURES) is None
    assert BACKEND.detect(FIXTURES / "missing_plugin_dir") is None
    assert BACKEND.detect(Path(__file__)) is None


def test_detect_has_no_side_effects() -> None:
    path_before = list(sys.path)
    first = BACKEND.detect(FIXTURES / "hello_astrbot")
    second = BACKEND.detect(FIXTURES / "hello_astrbot")
    assert sys.path == path_before
    assert first is not None and second is not None
    assert first.plugin_id == second.plugin_id
    assert first.version == second.version


# --------------------------------------------------------------------------- #
# 加载与能力抽取
# --------------------------------------------------------------------------- #


def test_load_extracts_specs(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    commands = {spec.id: spec for spec in plugin.commands}
    assert {"hello", "echo", "add", "ping", "notify", "ask", "kv", "quit", "emoji"} <= set(
        commands,
    )
    # 指令组函数本身不对外暴露
    assert "math" not in commands
    assert commands["add"].patterns == ("math add",)
    assert commands["add"].name == "math add"
    assert commands["hello"].patterns == ("hello",)
    assert commands["hello"].usage == "/hello"
    assert commands["hello"].parameters == {
        "name": {"type": "string", "required": False, "default": "world"},
    }
    assert commands["echo"].patterns == ("echo", "echoes")
    assert commands["echo"].aliases == ("echoes",)
    assert commands["echo"].parameters == {
        "message": {"type": "string", "required": True, "greedy": True},
    }
    assert commands["ping"].patterns == ("^ping$",)
    assert "permission=ADMIN" in commands["admin_only"].description

    tools = {spec.name: spec for spec in plugin.tools}
    assert set(tools) == {"get_weather"}
    weather = tools["get_weather"]
    assert weather.description == "获取天气信息。"
    assert weather.origin == "astrbot:hello_astrbot"
    assert weather.timeout == 30.0
    assert weather.parameters == {
        "type": "object",
        "properties": {"location": {"type": "string", "description": "地点"}},
    }

    events = {spec.event_type: spec for spec in plugin.events}
    assert "on_astrbot_loaded" in events
    assert "on_llm_request" in events
    assert "message" in events
    assert "message_type=GROUP_MESSAGE" in events["message"].description

    assert plugin.apis == []
    assert plugin.started is False
    assert plugin.loaded_at > 0


def test_load_is_idempotent(tmp_path: Path) -> None:
    host = _make_host(tmp_path)
    first = BACKEND.load(_detect("hello_astrbot"), host)
    first_count = len(first.commands)
    second = BACKEND.load(_detect("hello_astrbot"), host)
    assert len(second.commands) == first_count
    registry = backend_module._ensure_shim().handlers
    hello_handlers = [
        item
        for item in registry.get_handlers_by_module_name(second.native["module"])
        if item.handler_name == "hello"
    ]
    assert len(hello_handlers) == 1
    result = _run(second.handle_message(_context("/hello neko")))
    assert result.replies == ["Hello, neko!"]
    assert first.instance is not second.instance


def test_fake_host_satisfies_host_services(tmp_path: Path) -> None:
    assert isinstance(_make_host(tmp_path), HostServices)


# --------------------------------------------------------------------------- #
# 入站消息
# --------------------------------------------------------------------------- #


def test_handle_message_runs_command(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("/hello neko")))
    assert result.handled is True
    assert result.plugin_id == "hello_astrbot"
    assert result.framework == "astrbot"
    assert result.command == "hello"
    assert result.replies == ["Hello, neko!"]
    assert any(name.endswith("_hello") for name in _handler_names(result))
    assert result.data["umo"] == "neko:FriendMessage:u1"
    assert result.data["wake_prefix"] == "/"
    assert result.error == ""


def test_wake_prefix_and_default_param(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    assert _run(plugin.handle_message(_context("/hello"))).replies == ["Hello, world!"]
    # 私聊：上游默认 friend_message_needs_wake_prefix=False，没有前缀也算唤醒
    assert _run(plugin.handle_message(_context("hello"))).replies == ["Hello, world!"]
    # 群聊：需要唤醒前缀（或 raw 里标记被 @ 机器人）
    no_prefix = _run(plugin.handle_message(_context("hello", is_group=True, group_id="g1")))
    assert no_prefix.replies == []
    assert all(name.endswith("_on_group_message") for name in _handler_names(no_prefix))
    assert _run(
        plugin.handle_message(_context("/hello", is_group=True, group_id="g1")),
    ).replies == ["Hello, world!"]
    assert _run(
        plugin.handle_message(
            _context("hello", is_group=True, group_id="g1", raw={"is_at_bot": True}),
        ),
    ).replies == ["Hello, world!"]


def test_empty_wake_prefix_makes_group_chat_always_awake(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path, settings={"astrbot_wake_prefix": ""})
    result = _run(plugin.handle_message(_context("hello", is_group=True, group_id="g1")))
    assert result.replies == ["Hello, world!"]
    assert result.data["wake_prefix"] == ""


def test_handle_message_without_match_is_not_handled(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("随便聊聊天")))
    assert result.handled is False
    assert result.replies == []
    assert result.data["umo"] == "neko:FriendMessage:u1"


def test_wake_prefix_setting(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path, settings={"astrbot_wake_prefix": "!"})
    assert _run(plugin.handle_message(_context("!hello neko"))).replies == ["Hello, neko!"]
    assert _run(plugin.handle_message(_context("/hello neko"))).handled is False


def test_command_group_parses_typed_params(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("/math add 2 3")))
    assert result.handled is True
    assert result.replies == ["结果是: 5"]
    assert result.command == "math add"


def test_greedy_str_and_alias(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    assert _run(plugin.handle_message(_context("/echo a b c"))).replies == ["你发了: a b c"]
    assert _run(plugin.handle_message(_context("/echoes hi"))).replies == ["你发了: hi"]


def test_regex_command(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("/ping")))
    assert result.replies == ["pong"]
    assert result.command == "ping"


def test_message_listener_only_fires_for_matching_type(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    group = _run(
        plugin.handle_message(_context("闲聊", is_group=True, group_id="g7")),
    )
    assert group.handled is True
    assert group.replies == []
    assert any(name.endswith("_on_group_message") for name in _handler_names(group))
    assert plugin.instance.handled_events == ["group:g7"]

    private = _run(plugin.handle_message(_context("闲聊")))
    assert private.handled is False


def test_permission_filter(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    denied = _run(plugin.handle_message(_context("/admin_only")))
    assert denied.handled is True
    assert "权限不足" in denied.replies[0]
    assert denied.data["permission_denied"] == ["admin_only"]

    allowed = _run(plugin.handle_message(_context("/admin_only", raw={"is_admin": True})))
    assert allowed.replies == ["admin ok"]


def test_custom_filter_blocks_handler(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    assert _run(plugin.handle_message(_context("/blocked"))).replies == ["blocked ok"]
    assert _run(plugin.handle_message(_context("/blocked neko"))).handled is False


def test_stop_event_stops_propagation(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("/quit")))
    assert result.replies == ["bye"]
    assert result.data["stopped"] is True
    assert plugin.instance.stopped_handlers == ["quit"]


def test_non_text_components_are_reported_not_dropped_silently(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("/emoji")))
    assert result.replies == ["你好"]
    assert result.data["astrbot_results"] == [
        {
            "chain": [
                {"type": "face", "data": {"id": 21}},
                {"type": "text", "data": {"text": "你好"}},
            ],
        },
    ]


# --------------------------------------------------------------------------- #
# 能力映射
# --------------------------------------------------------------------------- #


def test_context_send_message_reaches_host_send_chat(tmp_path: Path) -> None:
    host, plugin = _load("hello_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("/notify")))
    assert result.replies == ["sent=True"]
    assert len(host.sent) == 1
    sent = host.sent[0]
    assert sent["text"] == "主动消息"
    assert sent["source"] == "astrbot:hello_astrbot"
    assert sent["metadata"]["umo"] == "neko:FriendMessage:u1"
    assert sent["metadata"]["framework"] == "astrbot"


def test_event_send_reaches_host_send_chat(tmp_path: Path) -> None:
    host, plugin = _load("hello_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("/push")))
    assert result.replies == ["pushed"]
    assert len(host.sent) == 1
    assert host.sent[0]["text"] == "via event.send"
    assert host.sent[0]["metadata"]["immediate"] is True


def test_provider_maps_to_host_llm_generate(tmp_path: Path) -> None:
    host, plugin = _load("hello_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("/ask 你好")))
    assert result.replies == [LLM_REPLY.format(prompt="你好")]
    assert len(host.llm_calls) == 1
    call = host.llm_calls[0]
    assert call["prompt"] == "你好"
    assert call["system"] == "You are terse."


def test_llm_failure_becomes_structured_error(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path, fail_llm=True)
    result = _run(plugin.handle_message(_context("/ask 你好")))
    assert result.handled is True
    assert "ProviderError" in result.error
    assert result.data["provider_error_code"] == "model_usage_not_bound"
    assert result.data["stopped"] is True


def test_call_tool_invokes_fixture_tool(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    assert _run(plugin.call_tool("get_weather", {"location": "北京"})) == "北京 晴 25℃"
    with pytest.raises(BridgeError):
        _run(plugin.call_tool("no_such_tool", {}))


def test_llm_tool_missing_argument_is_wrapped(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    with pytest.raises(BridgeError):
        _run(plugin.call_tool("get_weather", {"wrong": "北京"}))


def test_unmapped_capability_is_structured_not_faked(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("/unsupported")))
    assert result.handled is True
    assert "context.get_db" in result.error
    assert result.data["unsupported_capability"] == "context.get_db"
    assert "context.get_db" in plugin.native["unsupported_calls"]

    shim_error = importlib.import_module("astrbot").UnsupportedCapabilityError("emoji.send")
    assert shim_error.to_payload() == {
        "success": False,
        "code": "unsupported_capability",
        "capability": "emoji.send",
        "error": "emoji.send is not mapped to N.E.K.O.",
    }


def test_api_specs_and_call_api_is_unsupported(tmp_path: Path) -> None:
    _, plugin = _load("hooked_astrbot", tmp_path)
    apis = {spec.name: spec for spec in plugin.apis}
    assert "/hooked/ping" in apis
    assert apis["/hooked/ping"].description == "健康检查"
    assert apis["/hooked/ping"].parameters == {"methods": ["GET"]}
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        _run(plugin.call_api("/hooked/ping", {}))
    assert excinfo.value.code == "unsupported_capability"


def test_legacy_llm_tool_registration(tmp_path: Path) -> None:
    _, plugin = _load("hooked_astrbot", tmp_path)
    names = {spec.name for spec in plugin.tools}
    assert names == {"legacy_echo"}
    assert _run(plugin.call_tool("legacy_echo", {"text": "hi"})) == "legacy:hi"


# --------------------------------------------------------------------------- #
# 钩子分发
# --------------------------------------------------------------------------- #

_HOOK_PAYLOADS = (
    ("on_astrbot_loaded", {}),
    ("on_platform_loaded", {}),
    ("on_plugin_loaded", {"metadata": {"name": "some_plugin"}}),
    ("on_plugin_unloaded", {"metadata": {"name": "some_plugin"}}),
    ("on_waiting_llm_request", {}),
    ("on_llm_request", {"request": {"system_prompt": "sys"}}),
    ("on_llm_response", {"response": {"text": "hi", "role": "assistant"}}),
    ("on_agent_begin", {"run_context": {"id": 1}}),
    ("on_agent_done", {"run_context": {"id": 1}, "response": {"text": "hi"}}),
    ("on_using_llm_tool", {"tool": {"name": "t"}, "tool_args": {"a": 1}}),
    ("on_llm_tool_respond", {"tool": {"name": "t"}, "tool_args": {}, "tool_result": "r"}),
    ("on_decorating_result", {}),
    ("after_message_sent", {}),
    (
        "on_plugin_error",
        {
            "plugin_name": "some_plugin",
            "handler_name": "some_handler",
            "error": RuntimeError("kaboom"),
            "traceback_text": "Traceback (most recent call last):\nRuntimeError: kaboom",
        },
    ),
)


def test_event_specs_match_hook_table(tmp_path: Path) -> None:
    _, plugin = _load("hooked_astrbot", tmp_path)
    events = {spec.event_type for spec in plugin.events}
    assert events == {name for name, _ in _HOOK_PAYLOADS} | {"message"}
    assert all(spec.description for spec in plugin.events if spec.event_type != "message")


def test_all_hook_types_dispatch_with_correct_arity(tmp_path: Path) -> None:
    _, plugin = _load("hooked_astrbot", tmp_path)
    context = _context("hi", user_id="u9")
    invoked: list[str] = []
    for name, payload in _HOOK_PAYLOADS:
        data = dict(payload)
        if name not in ("on_astrbot_loaded", "on_platform_loaded"):
            data.setdefault("context", context)
        outcome = _run(plugin.dispatch_event(name, data))
        assert outcome["handlers"], f"{name} should have a handler"
        assert outcome["event_type"] == name
        assert all("error" not in item for item in outcome["results"]), outcome["results"]
        invoked.extend(outcome["handlers"])
    # 每个钩子都以正确的参数个数被真正调用（fixture 里会记录调用名）
    recorded = {entry[0] for entry in plugin.instance.hook_calls}
    assert recorded == {name for name, _ in _HOOK_PAYLOADS}
    # 上游把 register_llm_tool 注册的 Handler 放进 OnLLMRequestEvent 桶，这一行为被保留
    assert invoked.count("llm_request") == 1
    assert "legacy_echo" in invoked


def test_llm_request_hook_receives_event_and_request(tmp_path: Path) -> None:
    _, plugin = _load("hooked_astrbot", tmp_path)
    _run(
        plugin.dispatch_event(
            "on_llm_request",
            {
                "context": _context("hi", user_id="u9"),
                "request": {"system_prompt": "sys"},
            },
        ),
    )
    assert (
        "on_llm_request",
        "neko:FriendMessage:u9|sys[neko-hooked]",
    ) in plugin.instance.hook_calls


def test_unknown_event_type_is_unsupported(tmp_path: Path) -> None:
    _, plugin = _load("hooked_astrbot", tmp_path)
    with pytest.raises(UnsupportedCapabilityError):
        _run(plugin.dispatch_event("on_a_made_up_event", {}))


def test_plugin_error_hook_catches_handler_exception(tmp_path: Path) -> None:
    _, plugin = _load("hooked_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("/boom")))
    assert result.handled is True
    assert "boom" in result.error
    assert result.data["stopped"] is True
    assert plugin.instance.error_hooks
    assert plugin.instance.error_hooks[0].endswith(":boom:boom")


def test_star_tools_send_message_reaches_host(tmp_path: Path) -> None:
    host, plugin = _load("hooked_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("/star_tools", user_id="u3")))
    assert result.replies == ["star_tools=True"]
    assert any(item["text"] == "star tools" for item in host.sent)


def test_star_tools_create_message(tmp_path: Path) -> None:
    _, plugin = _load("hooked_astrbot", tmp_path)
    shim = backend_module._ensure_shim()
    tools = importlib.import_module("astrbot.core.star.star_tools").StarTools
    platform = importlib.import_module("astrbot.core.platform")
    components = importlib.import_module("astrbot.core.message.components")
    message = _run(
        tools.create_message(
            "GroupMessage",
            "self-1",
            "g5",
            platform.MessageMember(user_id="u5", nickname="Neko"),
            [components.Plain("hi")],
            "hi",
            group_id="g5",
        ),
    )
    assert message.type is platform.MessageType.GROUP_MESSAGE
    assert message.group_id == "g5"
    assert message.message_id
    assert message.sender.user_id == "u5"
    assert message.message_str == "hi"
    assert shim.star_tools_cls is tools
    assert tools._context is not None
    shim_error = importlib.import_module("astrbot").UnsupportedCapabilityError
    with pytest.raises(shim_error):
        _run(tools.create_event(message, platform="aiocqhttp"))


def test_star_tools_get_data_dir(tmp_path: Path) -> None:
    host, plugin = _load("hooked_astrbot", tmp_path)
    expected = host.data_dir("hooked_astrbot").resolve()
    assert plugin.instance.data_dir == expected
    assert plugin.instance.named_data_dir == expected
    tools = importlib.import_module("astrbot.core.star.star_tools").StarTools
    unknown = tools.get_data_dir("unknown_foreign_plugin")
    assert unknown == host.data_dir("unknown_foreign_plugin").resolve()


def test_message_listener_in_hooked_plugin(tmp_path: Path) -> None:
    _, plugin = _load("hooked_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("随便说说")))
    assert result.handled is True
    assert result.replies == []
    assert any(name.endswith("_on_all_message") for name in _handler_names(result))
    assert ("message", "随便说说") in plugin.instance.hook_calls


# --------------------------------------------------------------------------- #
# 生命周期 / 配置 / 状态
# --------------------------------------------------------------------------- #


def test_start_and_stop_lifecycle(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    _run(BACKEND.start(plugin))
    assert plugin.started is True
    assert plugin.native["started"] is True
    assert plugin.instance.lifecycle == ["initialize"]
    assert plugin.instance.loaded_hooks == 1

    _run(BACKEND.stop(plugin))
    assert plugin.started is False
    assert plugin.instance.lifecycle == ["initialize", "terminate"]
    with pytest.raises(PluginNotLoadedError):
        _run(BACKEND.start(plugin))


def test_start_failure_is_recorded_not_raised(tmp_path: Path) -> None:
    host, plugin = _load("legacy_astrbot", tmp_path)
    instance = plugin.instance

    async def broken() -> None:
        raise RuntimeError("init blew up")

    instance.initialize = broken
    _run(BACKEND.start(plugin))
    assert plugin.started is False
    assert "init blew up" in plugin.native["start_error"]
    assert any(
        level == "error" and "failed to start" in message
        for level, message, _source in host.logs
    )


def test_stop_unknown_plugin_is_tolerated(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    _run(BACKEND.stop(plugin))
    _run(BACKEND.stop(plugin))


def test_config_defaults_and_save(tmp_path: Path) -> None:
    host, plugin = _load("hello_astrbot", tmp_path)
    config = plugin.instance.context.get_config()
    assert config["greeting"] == "Hello"
    assert config["max_items"] == 3
    assert config["verbose"] is False
    assert config["nested"] == {"token": "t"}
    assert config.get("missing") is None
    assert config.missing is None
    assert _run(config.save_config_async({"greeting": "Hi"})) is True
    assert host.saved[-1]["data"]["greeting"] == "Hi"


def test_host_config_is_merged_on_start(tmp_path: Path) -> None:
    host, plugin = _load(
        "hello_astrbot",
        tmp_path,
        configs={"hello_astrbot": {"greeting": "Yo"}},
    )
    _run(BACKEND.start(plugin))
    assert plugin.instance.context.get_config()["greeting"] == "Yo"
    result = _run(plugin.handle_message(_context("/hello")))
    assert result.replies == ["Yo, world!"]


def test_native_state_for_bridge_status(tmp_path: Path) -> None:
    _, plugin = _load("hello_astrbot", tmp_path)
    native = plugin.native
    assert isinstance(native, dict)
    assert native["framework"] == "astrbot"
    assert native["plugin_id"] == "hello_astrbot"
    assert native["native_plugin_id"] == "neko-tests/hello_astrbot"
    assert native["class"] == "HelloAstrBot"
    assert native["module"].startswith("_neko_bot_bridge_astrbot_hello_astrbot")
    assert native["astrbot_version"] == "4.28.1"
    assert native["shim_version"] == "0.1.0"
    assert native["unsupported_calls"] == []
    assert "hello" in native["specs"]["commands"]
    assert native["specs"]["tools"] == ["get_weather"]
    assert native["handlers"]["hooks"] >= 2
    assert native["conf_schema"] == ["greeting", "max_items", "nested", "verbose"]


def test_kv_store_persists_in_plugin_data_dir(tmp_path: Path) -> None:
    host, plugin = _load("hello_astrbot", tmp_path)
    result = _run(plugin.handle_message(_context("/kv 7")))
    assert result.replies == ["kv=7"]
    stored = json.loads((host.data_dir("hello_astrbot") / "plugin_kv.json").read_text("utf-8"))
    assert stored["visits"] == 7


def test_kv_store_via_initialize(tmp_path: Path) -> None:
    host, plugin = _load("hello_astrbot", tmp_path)
    _run(BACKEND.start(plugin))
    stored = json.loads((host.data_dir("hello_astrbot") / "plugin_kv.json").read_text("utf-8"))
    assert stored["boots"] == 1


# --------------------------------------------------------------------------- #
# 旧版插件
# --------------------------------------------------------------------------- #


def test_legacy_plugin_loads_and_runs(tmp_path: Path) -> None:
    _, plugin = _load("legacy_astrbot", tmp_path)
    assert plugin.native["class"] == "LegacyHelloPlugin"
    assert {spec.id for spec in plugin.commands} == {"legacy", "legacy_re"}
    assert [spec.name for spec in plugin.tools] == ["legacy_time"]
    legacy_spec = next(spec for spec in plugin.commands if spec.id == "legacy")
    assert legacy_spec.patterns == ("legacy",)
    assert legacy_spec.description == "旧接口注册的指令"

    assert _run(plugin.handle_message(_context("/legacy"))).replies == ["legacy ok"]
    assert _run(plugin.handle_message(_context("/legacyre"))).replies == ["legacy regex ok"]
    assert _run(plugin.call_tool("legacy_time", {})) == "legacy-time: 12:00"


def test_metadata_yaml_wins_over_register_decorator(tmp_path: Path) -> None:
    manifest = _detect("legacy_astrbot")
    assert manifest.name == "legacy_astrbot"
    assert manifest.version == "2.0.0"
    assert manifest.extra["author"] == "neko-tests"


def test_load_missing_directory_raises_plugin_load_error(tmp_path: Path) -> None:
    manifest = PluginManifest(
        path=tmp_path / "nope",
        plugin_id="nope",
        framework="astrbot",
    )
    with pytest.raises(PluginLoadError):
        BACKEND.load(manifest, _make_host(tmp_path))
