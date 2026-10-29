"""启停 / 删除入口（``bridge_set_plugin`` / ``bridge_delete_plugin``）的行为测试。

走真插件 + 假宿主（``tests/support.py``）+ 真夹具目录，验证：

* 入口返回结构化载荷（``ok`` / ``summary`` / ``disabled_plugins``）；
* 停用后 ``[bridge].disabled_plugins`` 真的落到宿主配置里（面板重启后不会又拉起来）；
* 删除会真的删文件，越界/找不到给结构化 ``Err`` 而不是抛；
* 启停会重投影桥接面（工具/入口数量跟着变）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import support

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _plugins_root(tmp_path: Path, *fixtures: str) -> Path:
    root = tmp_path / "plugins_root"
    for item in fixtures:
        framework, name = item.split("/", 1)
        source = FIXTURES / framework / name
        target = root / framework / name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return root


async def _plugin_with(tmp_path: Path, *fixtures: str):
    plugin, ctx = await support.instantiate_plugin()
    # 合并而不是整体替换：support 注入的 data_root 必须留下（否则会写到真机目录）
    bridge_section = dict(ctx._effective_config.get("bridge") or {})
    bridge_section.update({"plugins_root": str(_plugins_root(tmp_path, *fixtures)), "market": {"enabled": False}})
    ctx._effective_config["bridge"] = bridge_section
    startup = await plugin.on_startup()
    assert startup.is_ok(), startup
    return plugin, ctx


@pytest.mark.asyncio
async def test_set_plugin_entry_stops_and_starts(tmp_path: Path) -> None:
    plugin, ctx = await _plugin_with(tmp_path, "nonebot/hello_nonebot")
    try:
        registry = plugin._registry
        assert registry is not None and len(registry.plugins) == 1
        tools_before = len(plugin._router.status()["tools"])

        stopped = await plugin.bridge_set_plugin(plugin_id="hello_nonebot", enabled=False)
        assert stopped.is_ok(), stopped
        await plugin.flush_config_mirror()  # 配置镜像走后台，断言前等它
        payload = stopped.value
        assert payload["enabled"] is False and payload["changed"] is True
        assert payload["disabled_plugins"] == ["hello_nonebot"]
        assert "停止" in payload["summary"]
        assert registry.plugins == {}
        # 落盘：宿主配置里真的写了 disabled_plugins
        bridge_section = (ctx._effective_config.get("bridge") or {})
        assert list(bridge_section.get("disabled_plugins") or []) == ["hello_nonebot"]
        assert len(plugin._router.status()["tools"]) < tools_before or tools_before == 0

        started = await plugin.bridge_set_plugin(plugin_id="hello_nonebot", enabled=True)
        assert started.is_ok(), started
        await plugin.flush_config_mirror()
        assert started.value["disabled_plugins"] == []
        assert len(registry.plugins) == 1
        assert list((ctx._effective_config.get("bridge") or {}).get("disabled_plugins") or []) == []
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_set_plugin_entry_reports_unknown_plugin(tmp_path: Path) -> None:
    plugin, _ctx = await _plugin_with(tmp_path, "nonebot/hello_nonebot")
    try:
        result = await plugin.bridge_set_plugin(plugin_id="没有这个插件", enabled=True)
        assert not result.is_ok()
        assert "not loaded" in str(result.error) or "没有这个插件" in str(result.error)
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_delete_plugin_entry_removes_directory(tmp_path: Path) -> None:
    plugin, _ctx = await _plugin_with(tmp_path, "astrbot/hello_astrbot")
    try:
        target = Path(next(iter(plugin._registry.plugins.values())).manifest.path)
        assert target.is_dir()

        result = await plugin.bridge_delete_plugin(plugin_id="hello_astrbot")
        assert result.is_ok(), result
        payload = result.value
        assert payload["ok"] is True and payload["removed"]
        assert not target.exists()
        assert plugin._registry.plugins == {}
        assert "已删除" in payload["summary"]
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_admin_tool_reports_failure_as_dict(tmp_path: Path) -> None:
    """工具版启停：失败必须返回 dict（工具不能把异常抛给模型）。"""
    plugin, _ctx = await _plugin_with(tmp_path, "nonebot/hello_nonebot")
    try:
        ok_payload = await plugin._admin_tool_handler(plugin_id="hello_nonebot", enabled=False)
        assert ok_payload["ok"] is True and ok_payload["enabled"] is False
        bad_payload = await plugin._admin_tool_handler(plugin_id="不存在", enabled=True)
        assert bad_payload["ok"] is False and bad_payload["error"]
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_stopping_a_plugin_drops_its_surfaces(tmp_path: Path) -> None:
    """停用后它在宿主的入口/工具里必须消失（否则模型还能调到已停的插件）。"""
    plugin, _ctx = await _plugin_with(tmp_path, "nonebot/hello_nonebot")
    try:
        router = plugin._router
        before = router.status()
        assert any("hello_nonebot" in name for name in before["entries"]), before["entries"]
        assert any("hello_nonebot" in name for name in before["tools"]), before["tools"]

        stopped = await plugin.bridge_set_plugin(plugin_id="nonebot:hello_nonebot", enabled=False)
        assert stopped.is_ok(), stopped
        after = router.status()
        assert not any("hello_nonebot" in name for name in after["entries"]), after["entries"]
        assert not any("hello_nonebot" in name for name in after["tools"]), after["tools"]

        restarted = await plugin.bridge_set_plugin(plugin_id="nonebot:hello_nonebot", enabled=True)
        assert restarted.is_ok(), restarted
        again = router.status()
        assert any("hello_nonebot" in name for name in again["entries"]), again["entries"]
        assert any("hello_nonebot" in name for name in again["tools"]), again["tools"]
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_dashboard_state_lists_stopped_plugins_with_keys(tmp_path: Path) -> None:
    """面板要能看到停用的插件（带 key），否则没有可点的「启动」对象。"""
    plugin, _ctx = await _plugin_with(tmp_path, "nonebot/hello_nonebot")
    try:
        await plugin.bridge_set_plugin(plugin_id="nonebot:hello_nonebot", enabled=False)
        state = await plugin.dashboard_state()
        stopped = state.get("stopped_plugins") or []
        assert [row["key"] for row in stopped] == ["nonebot:hello_nonebot"]
        assert stopped[0]["disabled"] is True
        assert state["disabled_plugins"] == ["hello_nonebot"]
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_disabled_state_file_is_authoritative(tmp_path: Path) -> None:
    """状态文件存在时以它为准：配置里删掉 id 不能把禁用项「删掉」（配置只是镜像）。"""
    plugin, ctx = await _plugin_with(tmp_path, "nonebot/hello_nonebot")
    try:
        await plugin.bridge_set_plugin(plugin_id="nonebot:hello_nonebot", enabled=False)
        await plugin.flush_config_mirror()
        store = plugin._disabled_store_path()
        assert store is not None and store.exists()
        assert "hello_nonebot" in store.read_text(encoding="utf-8")

        # 模拟用户手改配置：把 disabled_plugins 清空
        ctx._effective_config["bridge"]["disabled_plugins"] = []
        await plugin.on_shutdown()
        fresh, _ctx2 = await _PluginBoot(ctx)  # 同一个 data_root 再启动一次
        assert fresh._settings["disabled_plugins"] == ["hello_nonebot"]
        assert fresh._registry is not None and fresh._registry.plugins == {}
        # 注册表**自己**也看到这份合并结果（它经 host.settings 读；构造顺序/快照都会让它过期）
        assert fresh._registry.disabled_plugins() == ["hello_nonebot"]
        assert fresh._registry.status()["disabled_plugins"] == ["hello_nonebot"]
    finally:
        pass


async def _PluginBoot(ctx):  # noqa: ANN001, ANN202 - 测试辅助：复用同一份配置/数据目录重启
    import plugin.plugins.bot_bridge as module

    instance = module.BotBridgePlugin(ctx)
    result = await instance.on_startup()
    assert result.is_ok(), result
    return instance, ctx


@pytest.mark.asyncio
async def test_toggle_returns_before_the_slow_config_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """宿主配置写回卡住时，启停入口必须**立刻**返回（真机踩过：每次启停都等满超时）。"""
    import asyncio
    import time

    plugin, _ctx = await _plugin_with(tmp_path, "nonebot/hello_nonebot")
    try:
        async def slow_update(*_args: object, **_kwargs: object) -> dict[str, object]:
            await asyncio.sleep(30)  # 模拟「宿主迟迟不回」
            return {}

        monkeypatch.setattr(type(plugin.config), "update", slow_update)

        started = time.monotonic()
        result = await plugin.bridge_set_plugin(plugin_id="nonebot:hello_nonebot", enabled=False)
        spent = time.monotonic() - started

        assert result.is_ok(), result
        assert spent < 1.0, f"启停入口被配置写回拖住了：{spent:.1f}s"
        # 状态文件已经落稳（权威来源），配置只是后台镜像
        store = plugin._disabled_store_path()
        assert store is not None and "hello_nonebot" in store.read_text(encoding="utf-8")
        assert plugin._registry is not None and plugin._registry.disabled_plugins() == ["hello_nonebot"]
    finally:
        for task in list(plugin._config_mirror_tasks):
            task.cancel()
        await plugin.on_shutdown()
