"""宿主能力层：SDK Result 拆包、[bridge.capabilities] 开关、api.call 路由、后台任务挂载的循环。"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import support  # noqa: E402

support.install_import_shims()

from plugin.sdk.plugin import Err, Ok, SdkError  # noqa: E402

from plugin.plugins.bot_bridge.bridge.backends import koishi, maibot  # noqa: E402
from plugin.plugins.bot_bridge.bridge.errors import UnsupportedCapabilityError  # noqa: E402
from plugin.plugins.bot_bridge.bridge.host import NEKOHostServices  # noqa: E402


class _Plugins:
    def __init__(self, entry_result, list_result=None):
        self.entry_result = entry_result
        self.list_result = list_result
        self.calls: list[str] = []

    async def call_entry(self, reference, params=None, *, timeout=30.0):
        self.calls.append(reference)
        return self.entry_result

    async def list(self):
        return self.list_result


def _host(tmp_path, plugins=None, caps=None):
    fake = SimpleNamespace(
        plugin_id="bot_bridge",
        plugins=plugins,
        ctx=SimpleNamespace(bus=None, models=None),
        push_message=lambda **_: {"submitted": True},
    )
    settings = {"data_root": str(tmp_path), "capabilities": dict(caps or {})}
    return NEKOHostServices(fake, settings)


async def test_call_plugin_entry_unwraps_ok_and_err(tmp_path):
    host = _host(tmp_path, _Plugins(Ok({"answer": 42})))
    assert await host.call_plugin_entry("web_search", "search", {}) == {"answer": 42}

    host = _host(tmp_path, _Plugins(Err(SdkError("boom"))))
    result = await host.call_plugin_entry("web_search", "search", {})
    assert result["success"] is False and "boom" in result["error"]


async def test_list_plugins_unwraps_result(tmp_path):
    items = [{"id": "web_search"}, {"id": "bot_bridge"}]
    host = _host(tmp_path, _Plugins(None, list_result=Ok(items)))
    assert [item["id"] for item in await host.list_plugins()] == ["web_search", "bot_bridge"]
    host = _host(tmp_path, _Plugins(None, list_result=Err(SdkError("down"))))
    assert await host.list_plugins() == []


@pytest.mark.parametrize(
    ("cap", "call"),
    [
        ("send", lambda h: h.send_chat("hi", source="x")),
        ("llm", lambda h: h.llm_embed(["hi"])),
        ("plugin_call", lambda h: h.call_plugin_entry("a", "b")),
        ("plugin_call", lambda h: h.list_plugins()),
        ("chat_query", lambda h: h.recent_messages()),
        ("chat_query", lambda h: h.recent_conversations()),
        ("config", lambda h: h.plugin_config("p")),
        ("config", lambda h: h.save_plugin_config("p", {})),
    ],
)
async def test_capability_switch_off_raises_unsupported(tmp_path, cap, call):
    host = _host(tmp_path, _Plugins(Ok({})), caps={cap: False})
    with pytest.raises(UnsupportedCapabilityError) as info:
        await call(host)
    assert info.value.code == "unsupported_capability"
    assert cap in info.value.detail


async def test_llm_generate_switch_off_is_structured(tmp_path):
    host = _host(tmp_path, caps={"llm": False})
    result = await host.llm_generate("hi")
    assert result["success"] is False and result["code"] == "unsupported_capability"


async def test_switch_on_by_default(tmp_path):
    host = _host(tmp_path, _Plugins(Ok({"ok": 1})))
    assert host.capability_enabled("send") and host.capability_enabled("storage")
    assert await host.call_plugin_entry("a", "b") == {"ok": 1}


async def test_maibot_storage_switch_off(tmp_path):
    host = _host(tmp_path, caps={"storage": False})
    stub = SimpleNamespace(host=host, source="maibot:test", _unsupported=lambda c, d="": {"success": False, "code": "unsupported_capability", "error": d})
    result = await maibot._PluginRuntime._cap_database(stub, "database.query", {"model_name": "m"})
    assert result["code"] == "unsupported_capability"


# ------------------------------------------------------------------ api.call 路由


class _Runtime:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def call_api(self, name, arguments):
        self.calls.append((name, dict(arguments)))
        return {"pong": arguments.get("x")}


async def test_api_call_prefers_loaded_maibot_plugin(tmp_path, monkeypatch):
    plugins = _Plugins(Ok({"from": "neko"}))
    host = _host(tmp_path, plugins)
    runtime = _Runtime()
    monkeypatch.setitem(maibot._RUNTIMES, "hello_maibot", runtime)
    monkeypatch.setattr(
        maibot, "find_registered_api",
        lambda name, version="", host=None: {"plugin_id": "hello_maibot", "name": "ping", "entry_id": "ping"} if name == "ping" else None,
    )
    stub = SimpleNamespace(host=host)
    result = await maibot._PluginRuntime._cap_api(stub, "api.call", {"api_name": "ping", "args": {"x": 1}})
    assert result == {"success": True, "result": {"pong": 1}}
    assert runtime.calls == [("ping", {"x": 1})]
    assert plugins.calls == []  # 没有拿外来 id 去问 N.E.K.O.


async def test_api_call_falls_back_to_neko_plugin(tmp_path, monkeypatch):
    plugins = _Plugins(Ok({"from": "neko"}))
    host = _host(tmp_path, plugins)
    monkeypatch.setattr(maibot, "find_registered_api", lambda *a, **k: None)
    stub = SimpleNamespace(host=host)
    result = await maibot._PluginRuntime._cap_api(stub, "api.call", {"api_name": "web_search:search", "args": {}})
    assert result == {"success": True, "result": {"from": "neko"}}
    assert plugins.calls == ["web_search:search"]

    plugins.entry_result = Err(SdkError("nope"))
    result = await maibot._PluginRuntime._cap_api(stub, "api.call", {"api_name": "web_search.search", "args": {}})
    assert result["success"] is False and "nope" in result["error"]

    result = await maibot._PluginRuntime._cap_api(stub, "api.call", {"api_name": "nosep", "args": {}})
    assert result["code"] == "api_not_found"


# ------------------------------------------------------------------ 事件循环


def test_koishi_host_call_on_closed_loop_does_not_raise():
    proc = object.__new__(koishi._NodeProcess)
    sent: list[dict] = []
    proc._send_line = sent.append
    proc._deferred_calls = []
    dead = asyncio.new_event_loop()
    dead.close()
    proc.loop = dead
    proc._handle_host_call({"call_id": 7, "method": "send_chat", "params": {"text": "hi"}})
    assert sent and sent[0]["ok"] is False and sent[0]["call_id"] == 7


async def test_poll_task_starts_on_command_loop_not_startup():
    plugin, ctx = await support.instantiate_plugin()
    ctx._effective_config["bridge"] = {
        "plugins_root": str(Path(ctx.tmpdir) / "empty"),
        "poll_conversations": True,
    }
    startup = await plugin.on_startup()
    try:
        assert startup.is_ok(), startup
        assert plugin._inbound_task is None  # startup 的循环会被宿主关掉，不能挂在那里
        await plugin._on_command_loop_start()
        task = plugin._inbound_task
        assert task is not None and not task.done()
        assert task.get_loop() is asyncio.get_running_loop()
    finally:
        await plugin.on_shutdown()
    assert task.done()


def test_astrbot_kv_store_storage_switch_off(tmp_path):
    import importlib

    from plugin.plugins.bot_bridge.bridge.backends import astrbot as astrbot_backend

    shim = astrbot_backend._ensure_shim()
    runtime = shim.runtime.RUNTIME
    kv = importlib.import_module("astrbot.core.utils.plugin_kv_store")
    shim_error = importlib.import_module("astrbot").UnsupportedCapabilityError
    previous = runtime._host
    runtime.install_host(astrbot_backend._HostBridge(_host(tmp_path, caps={"storage": False})))
    runtime.bind_plugin("cap_test/kv", "kv", tmp_path / "kv")
    try:
        with pytest.raises(shim_error):
            kv._store_path("cap_test/kv")
        runtime.install_host(astrbot_backend._HostBridge(_host(tmp_path)))
        assert kv._store_path("cap_test/kv").name == "plugin_kv.json"
    finally:
        runtime.unbind_plugin("cap_test/kv")
        runtime._host = previous


async def test_astrbot_send_switch_off_raises_shim_error(tmp_path):
    import importlib

    from plugin.plugins.bot_bridge.bridge.backends import astrbot as astrbot_backend

    astrbot_backend._ensure_shim()
    shim_error = importlib.import_module("astrbot").UnsupportedCapabilityError
    bridge = astrbot_backend._HostBridge(_host(tmp_path, _Plugins(Ok({})), caps={"send": False, "config": False}))
    with pytest.raises(shim_error) as info:
        await bridge.send_text("p", "hi")
    assert info.value.capability == "send.chat"
    with pytest.raises(shim_error):
        await bridge.plugin_config("p")
    with pytest.raises(shim_error):
        await bridge.save_plugin_config("p", {})
