"""bot_bridge 冒烟测试（真 SDK + 假宿主）。

跑法（在插件目录下）：

    <venv>\\Scripts\\python.exe -m pytest tests -q

覆盖：清单合法性、入口类身份、启动/停止、发现与加载、命令分发、能力回落、
结构化失败、重载。夹具插件放在 ``tests/fixtures/<framework>/``。
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
sys.path.insert(0, str(HERE))

import support  # noqa: E402 — 必须在 sys.path 就位后导入

support.install_import_shims()

from plugin.sdk.adapter import NekoAdapterPlugin  # noqa: E402

import plugin.plugins.bot_bridge as bridge_module  # noqa: E402


def test_manifest_is_loadable_adapter() -> None:
    data = tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))
    plugin_table = data["plugin"]
    assert plugin_table["id"] == "bot_bridge"
    assert plugin_table["type"] == "adapter"
    assert plugin_table["entry"] == "plugin.plugins.bot_bridge:BotBridgePlugin"
    assert data["adapter"]["mode"] == "gateway"
    assert (PLUGIN_DIR / "config.example.toml").is_file()
    assert (PLUGIN_DIR / "i18n" / "zh-CN.json").is_file()


def test_entry_class_is_adapter() -> None:
    assert issubclass(bridge_module.BotBridgePlugin, NekoAdapterPlugin)


@pytest.mark.asyncio
async def test_startup_without_plugins_registers_bridge_tools() -> None:
    plugin, ctx = await support.instantiate_plugin()
    ctx._effective_config["bridge"] = {"plugins_root": str(Path(ctx.tmpdir) / "empty")}
    startup = await plugin.on_startup()
    try:
        assert startup.is_ok(), startup
        names = {item["name"] for item in plugin.list_llm_tools()}
        assert {"bridge_list_plugins", "bridge_run_command", "bridge_call_plugin_tool"} <= names
        status = await plugin.bridge_status()
        assert status.is_ok()
        assert status.value["plugin_count"] == 0
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_default_plugins_root_is_resolved() -> None:
    """没有配置 plugins_root 时必须退回数据目录（曾经在这里踩过初始化顺序的坑）。"""
    plugin, ctx = await support.instantiate_plugin()
    startup = await plugin.on_startup()
    try:
        assert startup.is_ok(), startup
        assert plugin._host is not None
        root = Path(plugin._host.plugins_root)
        assert str(root).strip()
        # 必须是 <数据目录>/plugins，不能是 data_dir("plugins") 得到的 <数据目录>/plugins/data。
        assert root.name == "plugins", root
        assert root.parent.name != "plugins", root
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_loads_fixtures_and_routes_commands() -> None:
    root = _fixture_root()
    if root is None:
        pytest.skip("no fixture plugins available")
    plugin, ctx = await support.instantiate_plugin(plugins_root=root)
    startup = await plugin.on_startup()
    try:
        assert startup.is_ok(), startup
        listed = (await plugin.bridge_list_plugins()).value["plugins"]
        assert listed, "fixture plugins were not discovered"
        routed = False
        for entry in listed:
            commands = entry.get("commands") or []
            if not commands:
                continue
            result = await plugin.bridge_run_command(text=_sample_text(commands[0]))
            assert result.is_ok(), result
            if result.value["handled"]:
                routed = True
        assert routed, "no fixture command matched"
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_unknown_plugin_is_structured_error() -> None:
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="bot_bridge_empty_"))
    plugin, ctx = await support.instantiate_plugin(plugins_root=root)
    startup = await plugin.on_startup()
    try:
        assert startup.is_ok(), startup
        result = await plugin.bridge_call_api(plugin="missing:plugin", name="whatever")
        assert not result.is_ok()
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_reload_keeps_working() -> None:
    root = Path(support.MockHostContext().tmpdir) / "reload_root"
    root.mkdir(parents=True, exist_ok=True)
    plugin, ctx = await support.instantiate_plugin(plugins_root=root)
    startup = await plugin.on_startup()
    try:
        assert startup.is_ok(), startup
        result = await plugin.bridge_reload()
        assert result.is_ok(), result
        assert result.value["status"]["plugin_count"] == 0
    finally:
        await plugin.on_shutdown()


def _fixture_root() -> Path | None:
    fixtures = HERE / "fixtures"
    if not fixtures.is_dir():
        return None
    import shutil
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="bot_bridge_pytest_"))
    found = False
    for framework_dir in sorted(fixtures.iterdir()):
        if not framework_dir.is_dir():
            continue
        for plugin_dir in sorted(framework_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue
            shutil.copytree(plugin_dir, root / framework_dir.name / plugin_dir.name, dirs_exist_ok=True)
            found = True
    return root if found else None


def _sample_text(command: dict[str, object]) -> str:
    patterns = command.get("patterns") or []
    if isinstance(patterns, list) and patterns:
        cleaned = str(patterns[0]).strip("^$")
        cleaned = cleaned.replace("\\s*", " ").replace("\\s+", " ").replace(".*", "").replace("\\.", ".")
        if cleaned.strip():
            return cleaned.strip()
    return str(command.get("id") or "hello")


@pytest.mark.asyncio
async def test_offline_runs_never_touch_the_live_plugin_dir() -> None:
    """离线测试必须把数据目录指向临时目录：否则会在装好的插件目录里留下状态目录。"""
    plugin, ctx = await support.instantiate_plugin()
    effective = ctx._effective_config.get("bridge") or {}
    assert str(effective.get("data_root", "")).startswith(str(ctx.tmpdir)), effective
    startup = await plugin.on_startup()
    try:
        assert startup.is_ok(), startup
        assert plugin._host is not None
        data_root = plugin._host._data_root()
        assert str(data_root).startswith(str(ctx.tmpdir)), data_root
        # 真的建一次目录，确认落在临时根下
        created = plugin._host.data_dir("probe_plugin")
        assert str(created).startswith(str(ctx.tmpdir)), created
        assert created.name == "data"
    finally:
        await plugin.on_shutdown()
