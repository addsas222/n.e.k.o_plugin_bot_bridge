"""``bridge.backends.maibot``（MaiBot 兼容层）的 pytest 用例。

约定：
* 只依赖 ``bridge/contracts.py`` 定义的 ``HostServices``，宿主能力用下面的
  :class:`FakeHost` 就地实现并记录调用（没有网络、没有 sleep、完全确定）。
* 每个用例都是同步函数 + ``asyncio.run(...)``：这样不要求测试环境装
  pytest-asyncio，也避免 async 用例在 strict 模式下被静默跳过。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sys
import types
from collections.abc import Mapping, Sequence
from importlib.machinery import PathFinder
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = PLUGIN_ROOT / "bridge"
BACKENDS_DIR = PLUGIN_DIR / "backends"
FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "maibot"
HELLO_FIXTURE = FIXTURE_ROOT / "hello_maibot"
LEGACY_FIXTURE = FIXTURE_ROOT / "legacy_maibot"


def _import_backend_module() -> Any:
    """导入 ``bridge.backends.maibot``。

    先按正常包路径导入；如果 ``bridge/backends/__init__.py``（由桥接核心维护，
    它 import 了 ``.contracts``）当前不可导入，就注册最小命名空间包后重试 ——
    测试只关心后端模块本身，不触发 ``load_backends()``。
    """
    root = str(PLUGIN_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        return importlib.import_module("bridge.backends.maibot")
    except ImportError:
        for name, path in (("bridge", PLUGIN_DIR), ("bridge.backends", BACKENDS_DIR)):
            if name in sys.modules:
                continue
            namespace = types.ModuleType(name)
            namespace.__path__ = [str(path)]  # type: ignore[attr-defined]
            sys.modules[name] = namespace
        return importlib.import_module("bridge.backends.maibot")


maibot = _import_backend_module()
_BRIDGE_PACKAGE = str(maibot.__package__).rsplit(".", 1)[0]
contracts = importlib.import_module(f"{_BRIDGE_PACKAGE}.contracts")
errors = importlib.import_module(f"{_BRIDGE_PACKAGE}.errors")

BACKEND = maibot.BACKEND


# --------------------------------------------------------------------------- #
# 假宿主
# --------------------------------------------------------------------------- #


class FakeHost:
    """就地实现 ``HostServices`` 协议，并记录每次调用。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.logs: list[tuple[str, str, str]] = []
        self.chat_calls: list[dict[str, Any]] = []
        self.llm_calls: list[dict[str, Any]] = []
        self.embed_calls: list[list[str]] = []
        self.entry_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.saved_configs: list[tuple[str, dict[str, Any]]] = []
        self.plugin_configs: dict[str, dict[str, Any]] = {}
        self.messages: list[dict[str, Any]] = [
            {
                "id": "m1",
                "stream_id": "stream-1",
                "text": "早上好",
                "timestamp": 1000.0,
                "user_id": "u1",
                "group_id": "g1",
                "is_group": True,
            },
            {
                "id": "m2",
                "stream_id": "stream-2",
                "text": "晚上好",
                "timestamp": 2000.0,
                "user_id": "u2",
                "group_id": "",
                "is_group": False,
            },
        ]
        self._settings: dict[str, Any] = {"call_timeout": 5.0}

    # 基础面

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

    # 能力面

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
        self.chat_calls.append(
            {
                "text": text,
                "source": source,
                "visibility": list(visibility) if visibility else None,
                "ai_behavior": ai_behavior,
                "priority": priority,
                "metadata": dict(metadata or {}),
            }
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
                "tools": list(tools) if tools else None,
            }
        )
        return {
            "success": True,
            "response": "fake-llm-response",
            "reasoning": "fake-reasoning",
            "model": model or "fake-model",
            "tool_calls": [],
            "error": "",
        }

    async def llm_embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.embed_calls.append(list(texts))
        return [[float(len(text)), 0.5] for text in texts]

    async def call_plugin_entry(
        self,
        plugin_id: str,
        entry_id: str,
        params: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        self.entry_calls.append((plugin_id, entry_id, dict(params or {})))
        return {"success": True, "plugin_id": plugin_id, "entry_id": entry_id, "echo": dict(params or {})}

    async def list_plugins(self) -> list[dict[str, Any]]:
        return [{"plugin_id": "bot_bridge", "name": "bot_bridge", "version": "0.1.0"}]

    async def recent_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(item) for item in self.messages[: max(0, int(limit))]]

    async def plugin_config(self, plugin_id: str) -> dict[str, Any]:
        return dict(self.plugin_configs.get(plugin_id, {}))

    async def save_plugin_config(self, plugin_id: str, data: Mapping[str, Any]) -> bool:
        payload = dict(data)
        self.plugin_configs[plugin_id] = payload
        self.saved_configs.append((plugin_id, payload))
        return True

    # 断言辅助

    def sent_texts(self) -> list[str]:
        return [str(call["text"]) for call in self.chat_calls]


@pytest.fixture()
def host(tmp_path: Path) -> FakeHost:
    return FakeHost(tmp_path)


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _detect(path: Path):
    manifest = BACKEND.detect(path)
    assert manifest is not None, f"{path} 应该被识别为 MaiBot 插件目录"
    return manifest


def _load(fixture: Path, host: FakeHost):
    manifest = _detect(fixture)
    plugin = BACKEND.load(manifest, host)
    return manifest, plugin


def _context(text: str, **kwargs: Any):
    payload: dict[str, Any] = {"user_id": "u1", "user_name": "测试用户", "channel": "neko", "source": "test"}
    payload.update(kwargs)
    return contracts.CommandContext(text=text, **payload)


def _event_payload(text: str = "你好呀") -> dict[str, Any]:
    return {
        "text": text,
        "user_id": "u1",
        "user_name": "测试用户",
        "group_id": "g1",
        "channel": "neko",
        "source": "test",
        "is_group": True,
        "raw": {},
    }


def _real_src_package_available() -> bool:
    """旧版插件要 ``import src.plugin_system``；真实 ``src`` 包存在时兼容层不接管。"""
    try:
        return PathFinder.find_spec("src") is not None
    except (ImportError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #


def test_backend_exposes_framework_backend_contract(host: FakeHost) -> None:
    assert BACKEND.name == "maibot"
    assert BACKEND.display_name == "MaiBot"
    assert BACKEND.requires == ("pydantic",)
    assert isinstance(host, contracts.HostServices)
    for attribute in ("detect", "load", "start", "stop"):
        assert callable(getattr(BACKEND, attribute))


def test_detect_sdk_v2_fixture() -> None:
    manifest = _detect(HELLO_FIXTURE)
    assert manifest.framework == "maibot"
    assert manifest.plugin_id == HELLO_FIXTURE.name
    assert manifest.entry == "plugin.py"
    assert manifest.version == "1.0.0"
    assert manifest.extra["layout"] == "sdk_v2"
    assert manifest.extra["native_id"] == "bot-bridge-tests.hello-maibot"
    assert manifest.extra["config_file"] == "config.toml"
    assert "send.text" in manifest.extra["capabilities"]


def test_detect_legacy_fixture() -> None:
    manifest = _detect(LEGACY_FIXTURE)
    assert manifest.plugin_id == LEGACY_FIXTURE.name
    assert manifest.extra["layout"] == "legacy"
    assert manifest.extra["manifest_version"] == 1
    assert manifest.extra["native_id"] == LEGACY_FIXTURE.name


def test_detect_returns_none_for_non_maibot_paths(tmp_path: Path) -> None:
    plain = tmp_path / "not_a_plugin"
    plain.mkdir()
    (plain / "README.md").write_text("nothing here", encoding="utf-8")
    assert BACKEND.detect(plain) is None

    # 有的框架用 __init__.py 当入口（NoneBot2），不该被 MaiBot 认领。
    nonebot_like = tmp_path / "nonebot_like"
    nonebot_like.mkdir()
    (nonebot_like / "__init__.py").write_text("from nonebot import on_command\n", encoding="utf-8")
    assert BACKEND.detect(nonebot_like) is None

    # 有 plugin.py 但没有 _manifest.json：按官方 Runner 的规则不算 MaiBot 插件。
    manifestless = tmp_path / "manifestless"
    manifestless.mkdir()
    (manifestless / "plugin.py").write_text("from maibot_sdk import MaiBotPlugin\n", encoding="utf-8")
    assert BACKEND.detect(manifestless) is None

    assert BACKEND.detect(tmp_path / "does_not_exist") is None


# --------------------------------------------------------------------------- #
# SDK v2 插件
# --------------------------------------------------------------------------- #


def test_load_extracts_component_specs(host: FakeHost) -> None:
    manifest, plugin = _load(HELLO_FIXTURE, host)

    command_ids = {spec.id for spec in plugin.commands}
    assert {"ping", "echo", "config_probe", "db_probe", "emoji_probe"} <= command_ids
    echo = next(spec for spec in plugin.commands if spec.id == "echo")
    assert echo.patterns == (r"^/echo\s+(?P<text>.+)$",)
    assert echo.description

    tools = {spec.name: spec for spec in plugin.tools}
    assert {"maibot_add", "maibot_fail"} <= set(tools)
    schema = tools["maibot_add"].parameters or {}
    assert schema["type"] == "object"
    assert schema["required"] == ["left", "right"]
    assert schema["properties"]["left"]["type"] == "number"
    assert tools["maibot_add"].origin == "maibot:TOOL"

    event_types = {spec.event_type for spec in plugin.events}
    assert "on_message" in event_types
    assert "message.received" in event_types  # 桥接层入站别名

    apis = {spec.name: spec for spec in plugin.apis}
    assert "hello_status" in apis
    assert apis["hello_status"].version == "1"

    native = plugin.native
    assert isinstance(native, dict)
    assert native["sdk"]["source"] in {"user", "vendored"}
    # 断言「上报的版本 == 实际加载的那份 SDK 自己声明的版本」，而不是钉死一个
    # 字面量：vendor 升级（2.8.2 → 2.19.0）只该换 vendor 目录，不该让用例变红。
    assert native["sdk"]["version"]
    if native["sdk"]["source"] == "vendored":
        vendored_init = PLUGIN_ROOT / "vendor" / "maibot_sdk" / "__init__.py"
        declared = re.search(
            r'^__version__\s*=\s*"([^"]+)"',
            vendored_init.read_text(encoding="utf-8"),
            re.MULTILINE,
        )
        assert declared, "vendor 副本缺少 __version__"
        assert native["sdk"]["version"] == declared.group(1)
    assert native["native_id"] == "bot-bridge-tests.hello-maibot"
    assert native["component_counts"]["COMMAND"] == len(plugin.commands)
    assert native["legacy_adapter"] is False
    assert str(manifest.path) == native["directory"]


def test_start_and_stop_run_lifecycle(host: FakeHost) -> None:
    async def scenario() -> None:
        _, plugin = _load(HELLO_FIXTURE, host)
        instance = plugin.instance
        assert instance is not None
        assert getattr(instance, "loaded") is False

        await BACKEND.start(plugin)
        assert getattr(instance, "loaded") is True
        assert plugin.native["started"] is True
        assert plugin.native["start_error"] == ""

        await BACKEND.stop(plugin)
        assert getattr(instance, "unloaded") is True
        assert plugin.native["started"] is False
        assert maibot.runtime_for(plugin) is None

    asyncio.run(scenario())


def test_handle_message_matches_command_and_reaches_host_services(host: FakeHost) -> None:
    async def scenario() -> None:
        _, plugin = _load(HELLO_FIXTURE, host)

        result = await plugin.handle_message(_context("/echo 你好", user_id="u42"))
        assert isinstance(result, contracts.CommandResult)
        assert result.handled is True
        assert result.command == "echo"
        assert result.framework == "maibot"
        assert result.plugin_id == HELLO_FIXTURE.name
        assert any("echo：你好" in reply for reply in result.replies)
        assert result.data["matched_groups"] == {"text": "你好"}
        assert host.sent_texts() == ["echo：你好"]

        # ctx.send.text 真的走到了 HostServices.send_chat，并且带上了桥接元数据。
        call = host.chat_calls[0]
        assert call["source"] == f"maibot:{HELLO_FIXTURE.name}"
        assert call["metadata"]["maibot_capability"] == "send.text"
        assert "u42" in str(call["metadata"]["maibot_stream_id"])

        # ctx.llm.generate 走到了 HostServices.llm_generate。
        assert len(host.llm_calls) == 1
        assert "复述这句话：你好" in str(host.llm_calls[0]["prompt"])
        instance = plugin.instance
        assert getattr(instance, "llm_calls")[0]["response"] == "fake-llm-response"

    asyncio.run(scenario())


def test_handle_message_without_match_is_not_handled(host: FakeHost) -> None:
    async def scenario() -> None:
        _, plugin = _load(HELLO_FIXTURE, host)
        result = await plugin.handle_message(_context("随便聊两句"))
        assert result.handled is False
        assert result.replies == []
        assert host.chat_calls == []

    asyncio.run(scenario())


def test_call_tool_reaches_fixture_tool_and_reports_failures(host: FakeHost) -> None:
    async def scenario() -> None:
        _, plugin = _load(HELLO_FIXTURE, host)
        assert plugin.call_tool is not None

        result = await plugin.call_tool("maibot_add", {"left": 2, "right": 3})
        assert isinstance(result, Mapping)
        assert result["name"] == "maibot_add"
        assert result["content"] == "5"

        with pytest.raises(errors.BridgeError):
            await plugin.call_tool("maibot_fail", {})
        with pytest.raises(errors.BridgeError):
            await plugin.call_tool("does_not_exist", {})

    asyncio.run(scenario())


def test_call_api_and_dispatch_event(host: FakeHost) -> None:
    async def scenario() -> None:
        _, plugin = _load(HELLO_FIXTURE, host)

        payload = await plugin.call_api("hello_status", {"from": "test"})
        assert isinstance(payload, Mapping)
        assert payload["plugin"] == "hello_maibot"
        assert payload["arguments"] == {"from": "test"}

        with pytest.raises(errors.BridgeError):
            await plugin.call_api("nope", {})

        delivery = await plugin.dispatch_event("message.received", _event_payload("事件测试"))
        assert isinstance(delivery, Mapping)
        assert delivery["delivered"] == 1
        assert delivery["errors"] == []
        instance = plugin.instance
        assert getattr(instance, "seen_messages")[-1]["plain_text"] == "事件测试"

        # 框架原生事件名也能派发到同一个处理器。
        again = await plugin.dispatch_event("on_message", _event_payload("原生名"))
        assert again["delivered"] == 1
        assert getattr(instance, "seen_messages")[-1]["plain_text"] == "原生名"

    asyncio.run(scenario())


def test_unmapped_capability_returns_structured_failure(host: FakeHost) -> None:
    async def scenario() -> None:
        _, plugin = _load(HELLO_FIXTURE, host)
        runtime = maibot.runtime_for(plugin)
        assert runtime is not None

        failure = await runtime.call_capability("emoji.get_count", {})
        assert failure == {
            "success": False,
            "code": "unsupported_capability",
            "error": "emoji.get_count is not mapped to N.E.K.O.",
        }
        assert any("emoji.get_count" in message for _level, message, _source in host.logs)

        for capability in ("render.html2png", "statistics.local.models", "maisaka.proactive.trigger", "person.get_id"):
            detail = await runtime.call_capability(capability, {})
            assert detail["success"] is False
            assert detail["code"] == "unsupported_capability"

        # 通过 SDK 走一遍：插件侧拿到的是结构化失败字典，而不是假成功。
        result = await plugin.handle_message(_context("/emoji_probe"))
        assert result.handled is True
        assert any("unsupported_capability" in reply for reply in result.replies)
        assert host.chat_calls == []

    asyncio.run(scenario())


def test_database_capability_uses_plugin_sqlite_store(host: FakeHost) -> None:
    async def scenario() -> None:
        manifest, plugin = _load(HELLO_FIXTURE, host)

        first = await plugin.handle_message(_context("/db_probe alpha"))
        second = await plugin.handle_message(_context("/db_probe beta"))
        assert first.handled is True and second.handled is True
        assert any("db_count=1" in reply for reply in first.replies)
        assert any("db_count=2" in reply for reply in second.replies)

        store_path = host.data_dir(manifest.plugin_id) / "maibot_store.sqlite3"
        assert store_path.is_file()

        runtime = maibot.runtime_for(plugin)
        assert runtime is not None
        rows = await runtime.call_capability("database.get", {"model_name": "notes", "filters": {"value": "beta"}})
        assert rows["success"] is True
        assert [row["value"] for row in rows["result"]] == ["beta"]
        counted = await runtime.call_capability("database.count", {"model_name": "notes"})
        assert counted == {"success": True, "count": 2, "result": 2}

    asyncio.run(scenario())


def test_handler_result_is_surfaced_and_json_safe(host: FakeHost) -> None:
    async def scenario() -> None:
        _, plugin = _load(HELLO_FIXTURE, host)

        # dict 形状的返回值：回复文本照旧提取，整个 dict 也要留在 data 里。
        dict_result = await plugin.handle_message(_context("/status"))
        assert dict_result.handled is True
        assert dict_result.replies == ["状态：在线"]
        assert dict_result.data["handler_result"] == {
            "content": "状态：在线",
            "plugin": "hello_maibot",
            "delivered": True,
            "seen": 0,
        }
        assert "handler_result_repr" not in dict_result.data

        # 元组形状的返回值（旧协议 (success, reply, intercept)）走 JSON 归一化。
        tuple_result = await plugin.handle_message(_context("/echo 你好"))
        raw = tuple_result.data["handler_result"]
        assert isinstance(raw, list) and raw[0] is True
        assert "echo：你好" in raw[1]
        assert tuple_result.data["success"] is True
        assert tuple_result.data["intercept_message_level"] == 1

        # 桥接核心拿到的 payload 必须能直接 JSON 序列化。
        payload = tuple_result.to_payload()
        assert json.loads(json.dumps(payload, ensure_ascii=False))["data"]["handler_result"] == raw

        # 不可 JSON 化的返回值退化成 repr 字符串，而不是把对象塞进状态输出。
        fields = maibot._handler_result_fields(object())
        assert list(fields) == ["handler_result_repr"]
        assert fields["handler_result_repr"].startswith("<object object at")
        assert maibot._handler_result_fields(None) == {"handler_result": None}

    asyncio.run(scenario())


def test_config_comes_from_plugin_toml_then_host(host: FakeHost) -> None:
    async def scenario() -> None:
        manifest, plugin = _load(HELLO_FIXTURE, host)

        result = await plugin.handle_message(_context("/config_probe"))
        assert any("greeting=你好，我是 hello_maibot" in reply for reply in result.replies)

        host.plugin_configs[manifest.plugin_id] = {
            "plugin": {"config_version": "1.1.0", "enabled": True},
            "greeting": {"message": "来自 N.E.K.O. 的问候"},
        }
        await BACKEND.start(plugin)
        after = await plugin.handle_message(_context("/config_probe"))
        assert any("greeting=来自 N.E.K.O. 的问候" in reply for reply in after.replies)
        assert plugin.native["config_present"] is True

    asyncio.run(scenario())


def test_image_capability_is_not_faked(host: FakeHost) -> None:
    async def scenario() -> None:
        _, plugin = _load(HELLO_FIXTURE, host)
        runtime = maibot.runtime_for(plugin)
        assert runtime is not None

        detail = await runtime.call_capability("send.image", {"image_base64": "AAAA", "stream_id": "s1"})
        assert detail["success"] is False
        assert detail["code"] == "unsupported_capability"
        assert host.chat_calls == []

        # 文本 + 图片的混合消息：文本照发，但结果里显式标记降级。
        hybrid = await runtime.call_capability(
            "send.hybrid",
            {
                "segments": [{"type": "text", "content": "正文"}, {"type": "image", "content": "AAAA"}],
                "stream_id": "s1",
            },
        )
        assert hybrid["success"] is True
        assert hybrid["degraded"] is True
        assert hybrid["dropped_segments"] == 1
        assert host.sent_texts() == ["正文"]

    asyncio.run(scenario())


def test_message_and_chat_capabilities_use_the_snapshot(host: FakeHost) -> None:
    async def scenario() -> None:
        _, plugin = _load(HELLO_FIXTURE, host)
        runtime = maibot.runtime_for(plugin)
        assert runtime is not None

        recent = await runtime.call_capability("message.get_recent", {"chat_id": "stream-1", "limit": 5})
        assert recent["success"] is True
        assert [item["id"] for item in recent["messages"]] == ["m1", "m2"]

        by_id = await runtime.call_capability("message.get_by_id", {"message_id": "m2"})
        assert by_id["message"]["text"] == "晚上好"

        streams = await runtime.call_capability("chat.get_all_streams", {"platform": "neko"})
        assert {item["stream_id"] for item in streams["streams"]} == {"stream-1", "stream-2"}
        group_streams = await runtime.call_capability("chat.get_group_streams", {"platform": "neko"})
        assert [item["stream_id"] for item in group_streams["streams"]] == ["stream-1"]

        definitions = await runtime.call_capability("tool.get_definitions", {})
        assert {item["name"] for item in definitions["tools"]} >= {"maibot_add", "maibot_fail"}

        plugins = await runtime.call_capability("component.get_all_plugins", {})
        assert plugins["success"] is True
        assert any(item["plugin_id"] == "bot_bridge" for item in plugins["plugins"])

    asyncio.run(scenario())


def test_route_message_is_reported_as_not_routed(host: FakeHost) -> None:
    async def scenario() -> None:
        _, plugin = _load(HELLO_FIXTURE, host)
        runtime = maibot.runtime_for(plugin)
        assert runtime is not None

        routed = await runtime.rpc_call("host.route_message", HELLO_FIXTURE.name, {"gateway_name": "test-gw"})
        assert routed["success"] is True
        assert routed["accepted"] is False
        assert routed["routed"] is False

        state = await runtime.rpc_call(
            "host.update_message_gateway_state",
            HELLO_FIXTURE.name,
            {"gateway_name": "test-gw", "ready": True, "platform": "qq"},
        )
        assert state["accepted"] is True
        assert plugin.native["gateway_state"]["test-gw"]["ready"] is True

        with pytest.raises(PermissionError):
            await runtime.rpc_call("host.secret_method", HELLO_FIXTURE.name, {})

    asyncio.run(scenario())


def test_reload_is_idempotent_and_restores_sys_path(host: FakeHost) -> None:
    async def scenario() -> None:
        first_manifest, first = _load(HELLO_FIXTURE, host)
        second_manifest, second = _load(HELLO_FIXTURE, host)

        assert str(HELLO_FIXTURE.parent) not in sys.path
        assert first.native["module"] == second.native["module"]
        assert maibot.runtime_for(first) is not None
        assert maibot.runtime_for(second) is not None

        await BACKEND.start(first)
        await BACKEND.start(second)
        first_result = await first.handle_message(_context("/ping"))
        second_result = await second.handle_message(_context("/ping"))
        assert first_result.handled is True and second_result.handled is True
        assert len(host.chat_calls) == 2
        assert first_manifest.plugin_id == second_manifest.plugin_id

        await BACKEND.stop(second)
        assert maibot.runtime_for(second) is None
        # 旧句柄仍然可用（运行时是按 LoadedPlugin 对象绑定的）。
        after_stop = await first.handle_message(_context("/ping"))
        assert after_stop.handled is True

    asyncio.run(scenario())


# --------------------------------------------------------------------------- #
# 旧版插件
# --------------------------------------------------------------------------- #


def test_legacy_plugin_load_and_dispatch(host: FakeHost) -> None:
    if _real_src_package_available():
        pytest.skip("环境里存在真实的 src 包，兼容层的 src.plugin_system 别名会被真实包遮蔽")

    async def scenario() -> None:
        manifest, plugin = _load(LEGACY_FIXTURE, host)

        assert plugin.native["legacy_adapter"] is True
        command_ids = {spec.id for spec in plugin.commands}
        assert "legacy_echo" in command_ids
        patterns = {spec.id: spec.patterns for spec in plugin.commands}
        assert patterns["legacy_echo"] == (r"^!legacy\s+(?P<text>.+)$",)

        tool_names = {spec.name for spec in plugin.tools}
        assert {"legacy_sum", "legacy_greet"} <= tool_names
        assert {spec.event_type for spec in plugin.events} >= {"on_message", "message.received"}
        assert plugin.apis == []

        await BACKEND.start(plugin)
        legacy_instance = plugin.instance._legacy  # LegacyPluginAdapter 包装的旧插件
        assert legacy_instance.loaded is True

        result = await plugin.handle_message(_context("!legacy 你好", user_id="u7"))
        assert result.handled is True
        assert result.command == "legacy_echo"
        assert any("legacy echo：你好" in reply for reply in result.replies)
        assert host.sent_texts() == ["legacy echo：你好"]

        summed = await plugin.call_tool("legacy_sum", {"left": 4, "right": 6})
        assert summed["content"] == "10.0"

        greeted = await plugin.call_tool("legacy_greet", {"who": "小明"})
        assert greeted == (True, "legacy greet：小明")

        delivery = await plugin.dispatch_event("message.received", _event_payload("旧版事件"))
        assert delivery["delivered"] == 1
        handler = plugin.instance._component_instances["legacy_message_handler"]
        assert handler.handled_texts[-1] == "旧版事件"

        await BACKEND.stop(plugin)
        assert legacy_instance.unloaded is True
        assert maibot.runtime_for(plugin) is None
        assert manifest.plugin_id == LEGACY_FIXTURE.name

    asyncio.run(scenario())
