"""插件市场在适配器里的接线测试（不需要真的市场模块 / 真的网络）。

用鸭子类型的假市场对象注入 ``plugin._market_obj``，验证的是**接线**：

* 关掉 ``[bridge.market].enabled`` 时入口与搜索工具都给出明确原因；
* ``allow_install = false`` 时安装入口拒绝执行；
* 搜索 / 安装的返回值形状，以及安装成功后确实触发了注册表重载；
* 安装失败 → ``Err``（不是 Ok + 假的成功）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest

import support
from bridge.market import MarketSearchReport


@dataclass
class _FakeMarket:
    entries: list[object] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=lambda: {"maibot": 1, "nonebot": 2, "astrbot": 3})
    truncated: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)
    install_result: object | None = None
    search_error: Exception | None = None
    sources: list[dict[str, object]] = field(default_factory=lambda: [{"name": "maibot", "available": True}])
    calls: list[tuple[str, object]] = field(default_factory=list)

    async def sources_status(self, *, refresh: bool = False, only: str = "") -> list[dict[str, object]]:
        self.calls.append(("sources_status", {"refresh": refresh, "only": only}))
        return [row for row in self.sources if not only or row.get("name") == only]

    async def search(self, query: str = "", *, source: str = "", limit: int = 20, refresh: bool = False):
        self.calls.append(("search", {"query": query, "source": source, "limit": limit, "refresh": refresh}))
        if self.search_error is not None:
            raise self.search_error
        return list(self.entries)

    async def search_report(self, query: str = "", *, source: str = "", limit: int = 20, refresh: bool = False):
        self.calls.append(("search_report", {"query": query, "source": source, "limit": limit, "refresh": refresh}))
        if self.search_error is not None:
            raise self.search_error
        return MarketSearchReport(
            entries=list(self.entries),
            counts=dict(self.counts),
            truncated=dict(self.truncated),
            failures=dict(self.failures),
        )

    async def install(self, *, source: str, plugin_id: str, overwrite: bool = False):
        self.calls.append(("install", {"source": source, "plugin_id": plugin_id, "overwrite": overwrite}))
        assert self.install_result is not None
        return self.install_result


class _FakeInstallResult:
    def __init__(self, ok: bool, error: str = "", target: str = "/tmp/plugin") -> None:
        self.ok = ok
        self.error = error
        self._payload = {
            "ok": ok,
            "source": "maibot",
            "plugin_id": "demo",
            "target_dir": target,
            "files": 3,
            "url": "https://example.invalid/x.zip",
            "archive_sha256": "0" * 64,
            "error": error,
        }

    def to_payload(self) -> dict[str, object]:
        return dict(self._payload)


def _entry(**overrides: object) -> SimpleNamespace:
    base = {
        "source": "maibot",
        "plugin_id": "demo.plugin",
        "name": "演示插件",
        "version": "1.0.0",
        "author": "tester",
        "description": "用于测试的条目",
        "repository": "https://github.com/example/demo",
        "install_kind": "github-zip",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_disabled_market_reports_reason() -> None:
    plugin, ctx = await support.instantiate_plugin()
    ctx._effective_config["bridge"] = {"plugins_root": str(Path(ctx.tmpdir) / "empty"), "market": {"enabled": False}}
    startup = await plugin.on_startup()
    try:
        assert startup.is_ok(), startup
        result = await plugin.bridge_market_sources()
        assert not result.is_ok()
        assert "enabled = false" in str(result.error)
        assert plugin._register_market_tool() is False
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_sources_entry_defaults_to_no_fetch() -> None:
    plugin, ctx = await support.instantiate_plugin()
    ctx._effective_config["bridge"] = {"plugins_root": str(Path(ctx.tmpdir) / "empty")}
    startup = await plugin.on_startup()
    try:
        fake = _FakeMarket()
        plugin._market_obj = fake
        assert (await plugin.bridge_market_sources()).is_ok()
        assert (await plugin.bridge_market_sources(refresh=True, source="maibot")).is_ok()
        calls = [payload for name, payload in fake.calls if name == "sources_status"]
        assert calls == [{"refresh": False, "only": ""}, {"refresh": True, "only": "maibot"}]
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_search_entry_and_tool_share_payload() -> None:
    plugin, ctx = await support.instantiate_plugin()
    ctx._effective_config["bridge"] = {"plugins_root": str(Path(ctx.tmpdir) / "empty")}
    startup = await plugin.on_startup()
    try:
        assert startup.is_ok(), startup
        fake = _FakeMarket(entries=[_entry()])
        plugin._market_obj = fake

        entry_result = await plugin.bridge_market_search(query="演示", source="maibot", limit=5)
        assert entry_result.is_ok(), entry_result
        payload = entry_result.value
        assert payload["count"] == 1
        row = payload["results"][0]
        assert row["source"] == "maibot" and row["plugin_id"] == "demo.plugin"
        assert payload["source_counts"] == {"maibot": 1, "nonebot": 2, "astrbot": 3}
        assert payload["source_truncated"] == {} and payload["source_failures"] == {}
        assert "命中 1 个插件" in payload["summary"] and "nonebot 2" in payload["summary"]

        tool_payload = await plugin._market_tool_handler(query="演示", source="maibot", limit=5)
        assert tool_payload["count"] == 1
        assert [name for name, _ in fake.calls] == ["search_report", "search_report"]
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_search_failure_is_err() -> None:
    plugin, ctx = await support.instantiate_plugin()
    ctx._effective_config["bridge"] = {"plugins_root": str(Path(ctx.tmpdir) / "empty")}
    startup = await plugin.on_startup()
    try:
        fake = _FakeMarket(search_error=RuntimeError("索引挂了"))
        plugin._market_obj = fake
        result = await plugin.bridge_market_search(query="x")
        assert not result.is_ok()
        assert "索引挂了" in str(result.error)
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_install_requires_allow_install() -> None:
    plugin, ctx = await support.instantiate_plugin()
    ctx._effective_config["bridge"] = {
        "plugins_root": str(Path(ctx.tmpdir) / "empty"),
        "market": {"allow_install": False},
    }
    startup = await plugin.on_startup()
    try:
        plugin._market_obj = _FakeMarket(install_result=_FakeInstallResult(ok=True))
        result = await plugin.bridge_market_install(source="maibot", plugin_id="demo")
        assert not result.is_ok()
        assert "allow_install" in str(result.error)
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_install_success_reloads_registry() -> None:
    plugin, ctx = await support.instantiate_plugin()
    root = Path(ctx.tmpdir) / "install_root"
    ctx._effective_config["bridge"] = {"plugins_root": str(root)}
    startup = await plugin.on_startup()
    try:
        assert startup.is_ok(), startup
        fake = _FakeMarket(install_result=_FakeInstallResult(ok=True, target=str(root / "maibot" / "demo")))
        plugin._market_obj = fake

        reloads: list[str] = []
        original_reload = plugin._registry.reload

        async def tracked_reload():
            reloads.append("reload")
            return await original_reload()

        plugin._registry.reload = tracked_reload  # type: ignore[method-assign]

        result = await plugin.bridge_market_install(source="maibot", plugin_id="demo")
        assert result.is_ok(), result
        assert reloads == ["reload"]
        assert result.value["ok"] is True
        assert result.value["reload"]["ok"] is True
        assert "已安装" in result.value["summary"]
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_install_failure_is_err() -> None:
    plugin, ctx = await support.instantiate_plugin()
    ctx._effective_config["bridge"] = {"plugins_root": str(Path(ctx.tmpdir) / "install_root2")}
    startup = await plugin.on_startup()
    try:
        plugin._market_obj = _FakeMarket(install_result=_FakeInstallResult(ok=False, error="already installed"))
        result = await plugin.bridge_market_install(source="maibot", plugin_id="demo")
        assert not result.is_ok()
        assert "already installed" in str(result.error)
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_install_reports_load_failure_after_reload() -> None:
    """装上了但插件自身 import 失败时，结果里必须带出原因，而不是只说“已安装”。"""
    plugin, ctx = await support.instantiate_plugin()
    root = Path(ctx.tmpdir) / "install_root4"
    ctx._effective_config["bridge"] = {"plugins_root": str(root)}
    startup = await plugin.on_startup()
    try:
        assert startup.is_ok(), startup
        target = root / "astrbot" / "astrbot_plugin_x"
        plugin._market_obj = _FakeMarket(install_result=_FakeInstallResult(ok=True, target=str(target)))

        # 真实流程里 reload() 会清空旧错误、再在扫描时重新记录失败 —— 这里照同样顺序模拟。
        original_reload = plugin._registry.reload

        async def failing_reload():
            status = await original_reload()
            plugin._registry._plugin_errors["astrbot:astrbot_plugin_x"] = (
                "导入插件模块失败：ModuleNotFoundError(\"No module named 'feedparser'\")"
            )
            return status

        plugin._registry.reload = failing_reload  # type: ignore[method-assign]
        result = await plugin.bridge_market_install(source="astrbot", plugin_id="astrbot_plugin_x")
        assert result.is_ok(), result
        assert result.value["loaded"] is False
        assert "feedparser" in str(result.value["load_error"])
        assert "加载失败" in result.value["summary"]
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_install_requires_source_and_id() -> None:
    plugin, ctx = await support.instantiate_plugin()
    ctx._effective_config["bridge"] = {"plugins_root": str(Path(ctx.tmpdir) / "install_root3")}
    startup = await plugin.on_startup()
    try:
        plugin._market_obj = _FakeMarket(install_result=_FakeInstallResult(ok=True))
        result = await plugin.bridge_market_install(source="", plugin_id="")
        assert not result.is_ok()
        assert "必填" in str(result.error)
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_market_tool_registered_when_module_available() -> None:
    """市场模块存在时，搜索工具应出现在对话期工具里；安装不暴露给模型。"""
    plugin, ctx = await support.instantiate_plugin()
    ctx._effective_config["bridge"] = {"plugins_root": str(Path(ctx.tmpdir) / "empty")}
    startup = await plugin.on_startup()
    try:
        assert startup.is_ok(), startup
        names = {item["name"] for item in plugin.list_llm_tools()}
        if plugin._market_obj is None:
            pytest.skip(f"市场模块不可用：{plugin._market_error}")
        assert plugin.MARKET_TOOL_NAME in names
        assert not any("install" in name for name in names), names
    finally:
        await plugin.on_shutdown()


@pytest.mark.asyncio
async def test_search_reports_per_source_counts_and_failures() -> None:
    """跨源搜索要带出「每源命中数 / 失败源」：结果只出现一个源的插件时必须能解释清楚。"""
    plugin, ctx = await support.instantiate_plugin()
    ctx._effective_config["bridge"] = {"plugins_root": str(Path(ctx.tmpdir) / "empty")}
    startup = await plugin.on_startup()
    try:
        assert startup.is_ok(), startup
        fake = _FakeMarket(
            entries=[_entry(source="astrbot", plugin_id="astrbot_plugin_demo")],
            counts={"maibot": 0, "nonebot": 0, "astrbot": 12},
            failures={"maibot": "TimeoutError: 索引超时"},
        )
        plugin._market_obj = fake
        result = await plugin.bridge_market_search(query="demo")
        assert result.is_ok(), result
        payload = result.value
        assert payload["source_counts"] == {"maibot": 0, "nonebot": 0, "astrbot": 12}
        assert payload["source_failures"] == {"maibot": "TimeoutError: 索引超时"}
        assert payload["results"][0]["source"] == "astrbot"
    finally:
        await plugin.on_shutdown()
