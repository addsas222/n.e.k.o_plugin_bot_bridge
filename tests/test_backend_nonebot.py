"""``bridge/backends/nonebot.py`` 的聚焦测试。

覆盖：识别（目录 / 单文件 / 非 NoneBot）、加载（模块导入 + 规格提取）、事件分发
（命令 / 正则 / 会话续跑 / 通知）、工具调用、能力回落（分发内回复 vs 分发外发言）、
未映射能力的结构化失败、生命周期钩子、依赖解析、幂等性。

测试自带一个实现 ``HostServices`` 的假宿主（记录所有调用），因此不联网、不依赖 N.E.K.O.。
每个测试用 ``asyncio.run`` 驱动（不依赖 pytest-asyncio），并且不用真实延时。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "nonebot"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = PLUGIN_ROOT / "bridge"
BACKENDS_DIR = PLUGIN_DIR / "backends"
PLUGIN_ID = "hello_nonebot"
SINGLE_FILE_PLUGIN = "single_file_plugin"


def _import_backend_module() -> Any:
    """导入 ``bridge.backends.nonebot``。

    先按正常包路径导入；如果 ``bridge/backends/__init__.py``（由桥接核心维护，
    它 import 了 ``.contracts``）当前不可导入，就注册最小命名空间包后重试 ——
    测试只关心后端模块本身，不触发 ``load_backends()``。
    """
    root = str(PLUGIN_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        return importlib.import_module("bridge.backends.nonebot")
    except ImportError:
        for name, path in (("bridge", PLUGIN_DIR), ("bridge.backends", BACKENDS_DIR)):
            if name in sys.modules:
                continue
            namespace = types.ModuleType(name)
            namespace.__path__ = [str(path)]  # type: ignore[attr-defined]
            sys.modules[name] = namespace
        return importlib.import_module("bridge.backends.nonebot")


nonebot_backend = _import_backend_module()
_BRIDGE_PACKAGE = str(nonebot_backend.__package__).rsplit(".", 1)[0]
contracts = importlib.import_module(f"{_BRIDGE_PACKAGE}.contracts")
errors = importlib.import_module(f"{_BRIDGE_PACKAGE}.errors")

BACKEND = nonebot_backend.BACKEND
CommandContext = contracts.CommandContext
BACKEND_MODULE = nonebot_backend
ERRORS = errors

_SKIP_MARK = pytest.mark.skipif(
    not (FIXTURES / PLUGIN_ID).is_dir(), reason="nonebot fixtures are missing"
)


class FakeHost:
    """``HostServices`` 的最小实现：记录调用，不碰真实 N.E.K.O.。"""

    def __init__(self, tmp_path: Path, settings: dict[str, Any] | None = None) -> None:
        self._root = Path(tmp_path)
        self._root.mkdir(parents=True, exist_ok=True)
        self.settings: dict[str, Any] = {
            "nonebot_command_start": ["/"],
            "nonebot_command_sep": ["."],
            "nonebot_self_id": "10000",
        }
        if settings:
            self.settings.update(settings)
        self.logs: list[tuple[str, str, str]] = []
        self.chats: list[dict[str, Any]] = []
        self.llm_calls: list[Any] = []
        self.plugin_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.saved_configs: list[tuple[str, dict[str, Any]]] = []

    # ---- 日志 / 路径 ------------------------------------------------------ #
    def log(self, level: str, message: str, *, source: str = "") -> None:
        self.logs.append((level, source, message))

    def data_dir(self, plugin_id: str) -> Path:
        target = self._root / plugin_id / "data"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def cache_dir(self, plugin_id: str) -> Path:
        target = self._root / plugin_id / "cache"
        target.mkdir(parents=True, exist_ok=True)
        return target

    # ---- 能力面 ---------------------------------------------------------- #
    async def send_chat(
        self,
        text: str,
        *,
        source: str,
        visibility: Any = None,
        ai_behavior: str = "",
        priority: int = 5,
        metadata: Any = None,
    ) -> bool:
        self.chats.append(
            {
                "text": text,
                "source": source,
                "visibility": list(visibility or []),
                "ai_behavior": ai_behavior,
                "priority": priority,
                "metadata": dict(metadata or {}),
            }
        )
        return True

    async def llm_generate(
        self,
        prompt: Any,
        *,
        model: str = "",
        system: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: Any = None,
    ) -> dict[str, Any]:
        self.llm_calls.append(prompt)
        return {
            "success": True,
            "response": "fake-response",
            "reasoning": "",
            "model": model or "fake",
            "tool_calls": [],
            "error": "",
        }

    async def llm_embed(self, texts: Any) -> list[list[float]]:
        raise ERRORS.UnsupportedCapabilityError("llm.embed")

    async def call_plugin_entry(
        self, plugin_id: str, entry_id: str, params: Any = None, timeout: float = 30.0
    ) -> dict[str, Any]:
        self.plugin_calls.append((plugin_id, entry_id, dict(params or {})))
        return {"success": True, "plugin_id": plugin_id, "entry_id": entry_id}

    async def list_plugins(self) -> list[dict[str, Any]]:
        return [{"plugin_id": "bot_bridge"}]

    async def recent_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        return []

    async def plugin_config(self, plugin_id: str) -> dict[str, Any]:
        return {}

    async def save_plugin_config(self, plugin_id: str, data: Any) -> bool:
        self.saved_configs.append((plugin_id, dict(data)))
        return True


def _load(host: FakeHost, fixture: str = PLUGIN_ID) -> Any:
    """detect + load 一个 fixture 插件。"""
    path = FIXTURES / fixture
    if not path.exists():
        path = FIXTURES / f"{fixture}.py"
    manifest = BACKEND.detect(path)
    assert manifest is not None, f"fixture {fixture} was not detected as a nonebot plugin"
    return BACKEND.load(manifest, host)


def _ctx(text: str, *, user_id: str = "neko-user", is_group: bool = False, group_id: str = "") -> Any:
    return CommandContext(
        text=text,
        user_id=user_id,
        user_name="测试用户",
        group_id=group_id,
        is_group=is_group,
        source="bot_bridge",
    )


def _spec_id_by_pattern(loaded: Any, pattern: str) -> str:
    for command in loaded.commands:
        if pattern in command.patterns:
            return command.id
    raise AssertionError(f"no command spec matches pattern {pattern!r}")


def _tool_for_doc(loaded: Any, needle: str) -> Any:
    for tool in loaded.tools:
        if needle in tool.description:
            return tool
    raise AssertionError(f"no tool spec mentions {needle!r}")


# --------------------------------------------------------------------------- #
# detect
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_backend_exposes_framework_backend_contract(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    assert isinstance(host, contracts.HostServices)
    assert isinstance(BACKEND, contracts.FrameworkBackend)
    assert BACKEND.name == "nonebot"
    assert BACKEND.display_name == "NoneBot2"
    assert BACKEND.requires == ()


@_SKIP_MARK
def test_detect_recognises_package_and_file_plugins() -> None:
    package_manifest = BACKEND.detect(FIXTURES / PLUGIN_ID)
    assert package_manifest is not None
    assert package_manifest.framework == "nonebot"
    assert package_manifest.plugin_id == PLUGIN_ID
    assert package_manifest.path == FIXTURES / PLUGIN_ID
    assert package_manifest.extra["kind"] == "package"
    assert str(package_manifest.extra["module_name"]).startswith(BACKEND_MODULE.MODULE_PREFIX)
    assert package_manifest.entry.endswith("__init__.py")
    assert Path(package_manifest.entry).is_file()

    file_manifest = BACKEND.detect(FIXTURES / f"{SINGLE_FILE_PLUGIN}.py")
    assert file_manifest is not None
    assert file_manifest.extra["kind"] == "file"
    assert file_manifest.plugin_id == SINGLE_FILE_PLUGIN
    assert file_manifest.entry.endswith(f"{SINGLE_FILE_PLUGIN}.py")


def test_detect_rejects_non_nonebot_paths(tmp_path: Path) -> None:
    assert BACKEND.detect(tmp_path / "missing") is None
    assert BACKEND.detect(FIXTURES / "plain_module") is None
    assert BACKEND.detect(FIXTURES / "plain_module" / "plain.py") is None
    text_file = tmp_path / "notes.txt"
    text_file.write_text("import nonebot\n", encoding="utf-8")
    assert BACKEND.detect(text_file) is None
    private_module = tmp_path / "_hidden.py"
    private_module.write_text("from nonebot import on_command\n", encoding="utf-8")
    assert BACKEND.detect(private_module) is None
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert BACKEND.detect(empty_dir) is None


def test_source_detection_ignores_comments(tmp_path: Path) -> None:
    module = tmp_path / "not_a_plugin.py"
    module.write_text("# import nonebot\nprint('hello')\n", encoding="utf-8")
    assert BACKEND.detect(module) is None
    real = tmp_path / "real_plugin.py"
    real.write_text("from nonebot.adapters.onebot.v11 import Message\n", encoding="utf-8")
    manifest = BACKEND.detect(real)
    assert manifest is not None and manifest.plugin_id == "real_plugin"


# --------------------------------------------------------------------------- #
# load / 规格
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_load_extracts_specs(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    assert loaded.plugin_id == PLUGIN_ID
    assert loaded.framework == "nonebot"
    assert loaded.manifest.name == "Hello NoneBot"
    assert loaded.manifest.description.startswith("bot_bridge 的 NoneBot2 兼容层测试插件")
    assert loaded.manifest.extra["usage"].startswith("/hello")

    # 命令：on_command / on_regex / on_keyword 都是文本触发
    ids = {command.id for command in loaded.commands}
    assert {"hello", "ask", "admin"} <= ids
    hello = next(command for command in loaded.commands if command.id == "hello")
    assert hello.patterns == ("hello", "hi")
    assert hello.usage == "/hello / /hi"
    assert hello.priority == 5
    assert "问好" in hello.description
    calc = next(command for command in loaded.commands if "calc" in command.patterns[0])
    assert calc.usage.startswith("^calc")
    assert calc.priority == 6

    # 工具：每个消息型响应器都能被 LLM 调用（NoneBot2 没有原生工具概念，这是显式映射）
    tool_names = {tool.name for tool in loaded.tools}
    assert len(loaded.tools) == len(loaded.commands) + 1  # +1 = 没有文本触发的 ping
    assert all(name.startswith(f"{PLUGIN_ID}_") for name in tool_names)
    assert all(tool.origin == f"nonebot:{PLUGIN_ID}" for tool in loaded.tools)
    ping_tool = _tool_for_doc(loaded, "被 @ 或私聊时回一句话")
    assert ping_tool.name not in {f"{PLUGIN_ID}_{command.id}" for command in loaded.commands}

    # 事件：notice 型响应器
    assert [event.event_type for event in loaded.events] == ["notice"]

    # API：__plugin_meta__.extra["apis"] 声明的模块级函数
    assert {api.name for api in loaded.apis} == {"announce", "plugin_state", "resolve_dependency"}

    native = loaded.native
    assert isinstance(native, dict)
    assert native["plugin_id"] == PLUGIN_ID
    assert native["metadata"]["name"] == "Hello NoneBot"
    assert native["metadata"]["supported_adapters"] == ["~onebot.v11"]
    assert native["matchers"]["total"] == 6
    assert native["matchers"]["by_type"]["message"] == 5
    assert native["matchers"]["by_type"]["notice"] == 1
    assert native["hooks"]["startup"] == 1
    assert native["hooks"]["shutdown"] == 1
    assert native["enabled"] is True
    assert native["specs"]["commands"] == [command.id for command in loaded.commands]

    # 加载期日志通过 HostServices.log 转发
    assert any(source == f"nonebot:{PLUGIN_ID}" for _, source, _ in host.logs)


@_SKIP_MARK
def test_load_is_idempotent(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    shim_root = str(BACKEND_MODULE.SHIM_ROOT)
    first = _load(host)
    second = _load(host)
    assert [command.id for command in first.commands] == [command.id for command in second.commands]
    assert len(second.tools) == len(first.tools)
    assert sys.path.count(shim_root) == 1
    assert second.native["matchers"]["total"] == first.native["matchers"]["total"]


def test_load_raises_plugin_load_error_for_broken_module(tmp_path: Path) -> None:
    broken = tmp_path / "broken_nonebot.py"
    broken.write_text("import nonebot\nraise RuntimeError('boom')\n", encoding="utf-8")
    manifest = BACKEND.detect(broken)
    assert manifest is not None
    with pytest.raises(ERRORS.PluginLoadError) as info:
        BACKEND.load(manifest, FakeHost(tmp_path))
    assert "boom" in info.value.detail


# --------------------------------------------------------------------------- #
# 事件分发
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_handle_message_matches_command_and_collects_replies(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.handle_message(_ctx("/hello 世界"))

    result = asyncio.run(scenario())
    assert result.handled is True
    assert result.plugin_id == PLUGIN_ID
    assert result.framework == "nonebot"
    assert result.command == "hello"
    assert result.replies == ["你好，世界！很高兴见到你"]
    assert result.error == ""
    assert result.data["matchers"] == ["hello"]
    # 关键：分发路径上的发言只进 replies，绝不直接推进 N.E.K.O.
    assert host.chats == []


@_SKIP_MARK
def test_handle_message_group_event_uses_at_segment(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.handle_message(
            _ctx("[CQ:at,qq=10000] /hello 娜娜", user_id="42", is_group=True, group_id="7")
        )

    result = asyncio.run(scenario())
    assert result.handled is True
    assert result.data["is_group"] is True
    assert result.data["group_id"] == "7"
    assert len(result.replies) == 1
    # 回复以纯文本交给 N.E.K.O.（at 段在 N.E.K.O. 里没有意义，因此只保留文本）
    assert result.replies[0] == "你好，娜娜！很高兴见到你"
    assert "[CQ:at" not in result.replies[0]
    assert host.chats == []


@_SKIP_MARK
def test_handle_message_regex_and_no_match(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any, Any]:
        matched = await loaded.handle_message(_ctx("calc 21 * 2"))
        unmatched = await loaded.handle_message(_ctx("随便说点什么", is_group=True, group_id="7"))
        return matched, unmatched, await loaded.handle_message(_ctx("在吗"))

    matched, unmatched, chat = asyncio.run(scenario())
    assert matched.handled is True
    assert matched.replies == ["21 * 2 = 42"]
    assert matched.command.startswith("regex:")
    assert unmatched.handled is False
    assert unmatched.replies == []
    # 闲聊响应器（to_me + 非命令）走了自己的规则
    assert chat.handled is True
    assert chat.replies == ["pong（收到：在吗）"]


@_SKIP_MARK
def test_session_got_pause_and_resume(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any]:
        first = await loaded.handle_message(_ctx("/ask"))
        second = await loaded.handle_message(_ctx("张三"))
        return first, second

    first, second = asyncio.run(scenario())
    assert first.handled is True
    assert first.replies == ["你的名字是？"]
    assert second.handled is True
    assert second.replies == ["张三，很高兴见到你！"]
    assert second.error == ""
    # 会话续跑的临时响应器会 block 掉更低优先级的响应器（这里就是闲聊的 ping）
    assert second.data["matchers"] == ["session:command:ask"]


@_SKIP_MARK
def test_dispatch_event_runs_notice_matcher(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any, Any]:
        report = await loaded.dispatch_event(
            "notice",
            {
                "notice_type": "notify",
                "sub_type": "poke",
                "user_id": "42",
                "group_id": "7",
                "target_id": "10000",
                "self_id": "10000",
            },
        )
        state = await loaded.call_api("plugin_state", {})
        return report, state, await loaded.dispatch_event("message", {"post_type": "message", "message": "calc 2 + 2", "user_id": "42"})

    report, state, message_report = asyncio.run(scenario())
    assert report["success"] is True
    assert report["handled"] is True
    assert report["replies"] == ["已收到通知：notice.notify.poke"]
    assert state["result"]["state"]["notices"] == 1
    assert message_report["handled"] is True
    assert message_report["replies"] == ["2 + 2 = 4"]


@_SKIP_MARK
def test_superuser_permission_blocks_and_allows(tmp_path: Path) -> None:
    blocked_host = FakeHost(tmp_path / "blocked")
    blocked = _load(blocked_host)

    allowed_host = FakeHost(tmp_path / "allowed", {"nonebot_superusers": ["neko-user"]})
    allowed = _load(allowed_host)

    async def scenario() -> tuple[Any, Any]:
        return (
            await blocked.handle_message(_ctx("/admin")),
            await allowed.handle_message(_ctx("/admin")),
        )

    blocked_result, allowed_result = asyncio.run(scenario())
    assert blocked_result.handled is False
    assert blocked_result.replies == []
    assert allowed_result.handled is True
    assert allowed_result.replies == ["管理员你好"]


# --------------------------------------------------------------------------- #
# 工具 / API / 未映射能力
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_call_tool_reaches_the_matcher(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)
    tool = _tool_for_doc(loaded, "被 @ 或私聊时回一句话")

    async def scenario() -> tuple[Any, Any]:
        hit = await loaded.call_tool(tool.name, {"text": "在吗"})
        miss = await loaded.call_tool(tool.name, {"text": "/hello 世界"})
        return hit, miss

    hit, miss = asyncio.run(scenario())
    assert hit["success"] is True
    assert hit["matched"] is True
    assert hit["replies"] == ["pong（收到：在吗）"]
    # 工具调用仍然走原生规则：文本不满足响应器自己的规则时如实返回未命中
    assert miss["matched"] is False
    assert miss["success"] is False
    assert host.chats == []


@_SKIP_MARK
def test_call_api_exposes_declared_module_functions(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any]:
        state = await loaded.call_api("plugin_state", {})
        resolved = await loaded.call_api("resolve_dependency", {"name": PLUGIN_ID})
        return state, resolved

    state, resolved = asyncio.run(scenario())
    assert state["success"] is True
    assert state["result"]["config"]["greeting"] == "你好"
    assert resolved["result"]["module"].startswith(BACKEND_MODULE.MODULE_PREFIX)


@_SKIP_MARK
def test_out_of_run_send_falls_back_to_host_chat(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        result = await loaded.call_api("announce", {"text": "后台播报", "group_id": "7"})
        await asyncio.sleep(0)  # 让回复回落的任务跑起来（没有真实延时）
        return result

    result = asyncio.run(scenario())
    assert result["success"] is True
    assert result["result"]["sent"] is True
    assert host.chats == [
        {
            "text": "后台播报",
            "source": PLUGIN_ID,
            "visibility": [],
            "ai_behavior": "",
            "priority": 5,
            "metadata": {"nonebot_out_of_run": True},
        }
    ]


@_SKIP_MARK
def test_driver_exposes_type_for_driver_branching_plugins(tmp_path: Path) -> None:
    """``driver.type`` 必须存在（真机案例：nonebot_plugin_docs 读它决定 import 哪个驱动模块）。"""
    host = FakeHost(tmp_path)
    _load(host)
    import nonebot

    driver = nonebot.get_driver()
    assert isinstance(driver.type, str) and driver.type
    # 上游插件会 split("+")[0] 之后 import，我们只保证这一串不炸、且是诚实的 "~none"。
    assert driver.type.split("+")[0] == driver.type


@_SKIP_MARK
def test_unmapped_capabilities_are_structured_failures(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> list[BaseException]:
        errors: list[BaseException] = []
        for coro in (
            loaded.call_tool("hello_nonebot_not_a_tool", {}),
            loaded.call_api("not_declared", {}),
            loaded.dispatch_event("audio", {}),
            # 已声明的 API 运行期失败也要变成 BridgeError，而不是假装成功。
            loaded.call_api("resolve_dependency", {"name": "definitely_missing_plugin"}),
        ):
            try:
                await coro
            except BaseException as exc:  # noqa: BLE001 — 测试要收集所有异常类型
                errors.append(exc)
        return errors

    errors = asyncio.run(scenario())
    assert [type(error).__name__ for error in errors] == [
        "UnsupportedCapabilityError",
        "UnsupportedCapabilityError",
        "UnsupportedCapabilityError",
        "BridgeError",
    ]
    assert all(getattr(error, "code", "") in ("unsupported_capability", "bridge_error") for error in errors)
    assert "nonebot.tool.hello_nonebot_not_a_tool" in str(errors[0])
    assert "nonebot.api.not_declared" in str(errors[1])
    assert "nonebot.event.audio" in str(errors[2])


@_SKIP_MARK
def test_disabled_plugin_stops_dispatching(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, BaseException | None]:
        BACKEND.set_plugin_enabled(PLUGIN_ID, False)
        try:
            result = await loaded.handle_message(_ctx("/hello 世界"))
            try:
                await loaded.call_tool("anything", {})
            except BaseException as exc:  # noqa: BLE001 — 收集禁用时的异常
                return result, exc
            return result, None
        finally:
            BACKEND.set_plugin_enabled(PLUGIN_ID, True)

    result, error = asyncio.run(scenario())
    assert result.handled is False
    assert result.data["disabled"] is True
    assert isinstance(error, ERRORS.BridgeError)


# --------------------------------------------------------------------------- #
# 生命周期 / 依赖解析 / 单文件插件
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_start_and_stop_run_plugin_hooks(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any, Any, Any]:
        await BACKEND.start(loaded)
        after_start = await loaded.call_api("plugin_state", {})
        await BACKEND.stop(loaded)
        after_stop = await loaded.call_api("plugin_state", {})
        return after_start, after_stop, loaded.started, loaded.native

    after_start, after_stop, started, native = asyncio.run(scenario())
    assert after_start["result"]["state"]["startups"] == 1
    assert after_stop["result"]["state"]["shutdowns"] == 1
    assert started is False
    assert native["started"] is False
    assert native["hook_errors"] == []
    assert any("已加载" in message for _, _, message in host.logs)
    assert any("已卸载" in message for _, _, message in host.logs)


@_SKIP_MARK
def test_require_resolves_loaded_plugins_only(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, BaseException | None]:
        found = await loaded.call_api("resolve_dependency", {"name": PLUGIN_ID})
        try:
            await loaded.call_api("resolve_dependency", {"name": "not_loaded_plugin"})
        except BaseException as exc:  # noqa: BLE001 — 收集缺失依赖的异常
            return found, exc
        return found, None

    found, error = asyncio.run(scenario())
    assert found["success"] is True
    assert error is not None
    assert isinstance(error, ERRORS.BridgeError)
    assert "not_loaded_plugin" in str(error)


def test_single_file_plugin_dispatch(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host, SINGLE_FILE_PLUGIN)

    async def scenario() -> tuple[Any, Any]:
        return (
            await loaded.handle_message(_ctx("/sfping")),
            await loaded.handle_message(_ctx("来点 sfecho 吧")),
        )

    command, keyword = asyncio.run(scenario())
    assert {spec.id for spec in loaded.commands} == {"sfping", "keyword_sfecho"}
    assert command.handled is True and command.replies == ["sfpong"]
    assert keyword.handled is True and keyword.replies == ["sfecho ok"]
    assert host.chats == []


# --------------------------------------------------------------------------- #
# 真机目录布局（桥接层把插件放进 <插件目录>/data/plugins/<框架>/）
# --------------------------------------------------------------------------- #

LIVE_PLUGINS_ROOT = PLUGIN_ROOT / "data" / "plugins" / "nonebot"
LIVE_ECHO = LIVE_PLUGINS_ROOT / "echo_plugin.py"


def test_framework_category_dir_is_not_a_plugin() -> None:
    """``data/plugins/nonebot/`` 是分类目录，不能被当成本框架的插件。"""
    assert BACKEND.detect(LIVE_PLUGINS_ROOT) is None


@pytest.mark.skipif(not LIVE_ECHO.is_file(), reason="live echo fixture is missing")
def test_live_echo_plugin_round_trip(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    manifest = BACKEND.detect(LIVE_ECHO)
    assert manifest is not None
    assert manifest.plugin_id == "echo_plugin"
    loaded = BACKEND.load(manifest, host)
    assert {spec.patterns for spec in loaded.commands} == {("echo", "复读")}

    async def scenario() -> tuple[Any, Any, Any]:
        return (
            await loaded.handle_message(_ctx("/echo 你好")),
            await loaded.handle_message(_ctx("/复读 再见")),
            await loaded.handle_message(_ctx("/echo")),
        )

    with_args, alias, empty = asyncio.run(scenario())
    assert with_args.handled is True and with_args.replies == ["复读：你好"]
    assert alias.replies == ["复读：再见"]
    assert empty.replies == ["要复读点什么？"]
    assert host.chats == []


# --------------------------------------------------------------------------- #
# OneBot v12 分流（与 v11 并存，按插件源码判定）
# --------------------------------------------------------------------------- #


def test_v12_plugin_is_detected_as_v12(tmp_path: Path) -> None:
    """只用 v12 的插件在加载期就被判成 v12（native 里能读出来）。"""
    host = FakeHost(tmp_path)
    loaded = _load(host, "v12_hello")
    assert loaded.native["adapter"] == "v12"
    patterns = {pattern for spec in loaded.commands for pattern in spec.patterns}
    assert "v12ping" in patterns


def test_v11_plugin_still_defaults_to_v11(tmp_path: Path) -> None:
    """v11 插件不受影响：默认仍是 v11。"""
    host = FakeHost(tmp_path)
    loaded = _load(host, PLUGIN_ID)
    assert loaded.native["adapter"] == "v11"


def test_v12_group_event_uses_v12_classes(tmp_path: Path) -> None:
    """群消息合成的是 v12 的 ``GroupMessageEvent``（不是 v11 同名类）。"""
    host = FakeHost(tmp_path)
    loaded = _load(host, "v12_hello")
    ctx = CommandContext(
        text="/v12ping",
        user_id="u1",
        user_name="测试用户",
        group_id="g1",
        is_group=True,
        source="bot_bridge",
    )
    result = asyncio.run(loaded.handle_message(ctx))
    assert result.handled is True, result.error
    assert result.replies == ["v12:GroupMessageEvent:group:group_g1_u1"]


def test_v12_private_event_is_private_message(tmp_path: Path) -> None:
    """私聊合成 ``PrivateMessageEvent``，会话 id 按 v12 规则拼。"""
    host = FakeHost(tmp_path)
    loaded = _load(host, "v12_hello")
    result = asyncio.run(loaded.handle_message(_ctx("/v12ping", user_id="u9")))
    assert result.handled is True, result.error
    assert result.replies == ["v12:PrivateMessageEvent:private:private_u9"]


def test_v12_isinstance_guard_sees_group_event(tmp_path: Path) -> None:
    """插件里的 ``isinstance(event, GroupMessageEvent)`` 必须成立 —— 这是分流的硬判据。"""
    host = FakeHost(tmp_path)
    loaded = _load(host, "v12_hello")
    group = CommandContext(
        text="/v12group", user_id="u2", user_name="测试用户", group_id="g2", is_group=True, source="bot_bridge"
    )
    private = _ctx("/v12group", user_id="u2")
    group_result, private_result = asyncio.run(loaded.handle_message(group)), asyncio.run(
        loaded.handle_message(private)
    )
    assert group_result.replies == ["group:g2:Message"]
    assert private_result.replies == ["not-a-group-event"]


# --------------------------------------------------------------------------- #
# [bridge].nonebot_adapter 覆盖项
# --------------------------------------------------------------------------- #


def test_nonebot_adapter_config_forces_v12(tmp_path: Path) -> None:
    """``nonebot_adapter = "v12"`` 覆盖扫描结果：v11-only 插件也被判成 v12。"""
    host = FakeHost(tmp_path, {"nonebot_adapter": "v12"})
    loaded = _load(host, PLUGIN_ID)
    assert loaded.native["adapter"] == "v12"
    assert loaded.native["adapter_source"] == "config"


def test_nonebot_adapter_config_forces_v11(tmp_path: Path) -> None:
    """反过来也成立：v12-only 插件被强制成 v11。"""
    host = FakeHost(tmp_path, {"nonebot_adapter": "v11"})
    loaded = _load(host, "v12_hello")
    assert loaded.native["adapter"] == "v11"
    assert loaded.native["adapter_source"] == "config"


def test_nonebot_adapter_accepts_alias_spellings(tmp_path: Path) -> None:
    """``~onebot.v12`` / ``OneBot.V12`` 这类写法也认（大小写与前缀不敏感）。"""
    for value in ("~onebot.v12", "OneBot.V12", "v12", "12"):
        host = FakeHost(tmp_path / value.replace("~", "").replace(".", "-"), {"nonebot_adapter": value})
        loaded = _load(host, PLUGIN_ID)
        assert loaded.native["adapter"] == "v12", value


def test_nonebot_adapter_auto_keeps_scanning(tmp_path: Path) -> None:
    """``auto`` 保持按源码扫描，并标注来源是 scan。"""
    host = FakeHost(tmp_path, {"nonebot_adapter": "auto"})
    assert _load(host, "v12_hello").native["adapter_source"] == "scan"
    assert _load(host, PLUGIN_ID).native["adapter_source"] == "scan"


def test_nonebot_adapter_invalid_value_falls_back_to_scan(tmp_path: Path) -> None:
    """认不出的值不静默强转：回退到扫描，并留一条 warning。"""
    host = FakeHost(tmp_path, {"nonebot_adapter": "v13"})
    loaded = _load(host, "v12_hello")
    assert loaded.native["adapter"] == "v12"  # 扫描结果
    assert loaded.native["adapter_source"] == "scan"
    assert any("nonebot_adapter" in message and level == "warning" for level, _src, message in host.logs)


def test_v12_dispatch_accepts_segment_array_payload(tmp_path: Path) -> None:
    """v12 的 ``message`` 是段数组（``[{"type":..,"data":..}]``），分发路径要能吃下。

    回归：``Message`` 基类对可迭代对象是直接 ``extend``，不经过 ``_construct``；
    段数组里的 dict 会原样留在列表里，``extract_plain_text()`` 于是抛
    ``'dict' object has no attribute 'is_text'``。这条用例从 ``dispatch_event``
    入口走完整链路，确保归一化发生在协议边界上。
    """
    host = FakeHost(tmp_path)
    loaded = _load(host, "v12_hello")
    payload = {
        "type": "message",
        "detail_type": "group",
        "group_id": "g9",
        "user_id": "u9",
        "message": [{"type": "text", "data": {"text": "/v12ping"}}],
        "message_id": "m9",
        "self_id": "s9",
        "to_me": True,
    }
    result = asyncio.run(loaded.dispatch_event("message", payload))
    assert result["success"] is True, result
    assert result["handled"] is True, result
    assert any("v12:GroupMessageEvent" in reply for reply in result["replies"]), result["replies"]
