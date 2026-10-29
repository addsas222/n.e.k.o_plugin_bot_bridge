"""注册表 / 路由层的针对性测试。

覆盖三处容易悄悄退化的地方：

* 兼容层缺运行时依赖时必须**报出来并停用**，而不是表现为「一个插件都没发现」；
* 插件发现同时支持 ``<根>/<插件>``（扁平）与 ``<根>/<框架>/<插件>``（分类目录）两种布局；
* 跨框架路由：有明确触发模式的命令优先于「什么都收」的监听器，且可以点名插件。
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path

import pytest

from bridge.backends import load_backends
from bridge.contracts import CommandContext, CommandResult, CommandSpec, LoadedPlugin, PluginManifest
from bridge.router import BridgeRouter

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MAIBOT_FIXTURE = FIXTURES / "maibot" / "hello_maibot"


# --------------------------------------------------------------------------- #
# 兼容层依赖检查
# --------------------------------------------------------------------------- #


def test_load_backends_reports_missing_requirement(monkeypatch: pytest.MonkeyPatch) -> None:
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name: str, *args: object, **kwargs: object):
        if name == "yaml":
            return None
        return real_find_spec(name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)
    backends, errors = load_backends(["astrbot"])
    assert backends == []
    assert "yaml" in errors["astrbot"]
    assert "dependency" in errors["astrbot"]


def test_load_backends_returns_available_frameworks() -> None:
    """每个框架要么可用，要么带着原因出现在 errors 里 —— 不允许无声消失。"""
    from bridge.backends import _discover_backend_modules

    backends, errors = load_backends(None)
    available = {backend.name for backend in backends}
    assert available, "兼容层一个都没起来，环境缺依赖？"
    # 发现到的每一个框架都必须有归宿：可用的，或带原因的（自动发现，所以不写死清单）
    assert available | set(errors) == set(_discover_backend_modules())
    for name, reason in errors.items():
        assert reason, name  # 失败一定要带原因
    for backend in backends:
        assert backend.display_name
        assert callable(backend.detect) and callable(backend.load)


# --------------------------------------------------------------------------- #
# 发现：扁平 / 分类目录两种布局
# --------------------------------------------------------------------------- #


class _StubHost:
    """只满足 :class:`HostServices` 里被用到的那几个入口。"""

    def __init__(self, plugins_root: Path) -> None:
        self.plugins_root = plugins_root
        self.settings: Mapping[str, object] = {
            "enabled_frameworks": ["maibot"],
            "continue_on_error": True,
            "call_timeout": 5.0,
            "max_plugins_per_framework": 8,
            "expose_commands_as_tools": False,
            "expose_plugins_tools": False,
        }
        self.pushes: list[tuple[str, str]] = []
        self.lines: list[str] = []
        self._data = plugins_root.parent

    def log(self, level: str, message: str, *, source: str = "") -> None:
        self.lines.append(f"{level}: {source}: {message}")

    def data_dir(self, plugin_id: str) -> Path:
        target = self._data / plugin_id / "data"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def cache_dir(self, plugin_id: str) -> Path:
        target = self._data / plugin_id / "cache"
        target.mkdir(parents=True, exist_ok=True)
        return target

    async def plugin_config(self, plugin_id: str) -> dict[str, object]:
        return {}

    async def save_plugin_config(self, plugin_id: str, data: Mapping[str, object]) -> bool:
        return True

    async def send_chat(self, text: str, *, source: str, **kwargs: object) -> bool:
        self.pushes.append((source, text))
        return True

    async def llm_generate(self, prompt: object, **kwargs: object) -> dict[str, object]:
        return {"success": False, "response": "", "error": "not used in this test"}

    async def recent_messages(self, limit: int = 20) -> list[dict[str, object]]:
        return []

    async def list_plugins(self) -> list[dict[str, object]]:
        return []

    async def call_plugin_entry(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"success": True}


def _copy_fixture(destination: Path) -> None:
    shutil.copytree(MAIBOT_FIXTURE, destination, dirs_exist_ok=True)


@pytest.mark.asyncio
async def test_discovery_supports_flat_and_framework_layouts() -> None:
    root = Path(tempfile.mkdtemp(prefix="bot_bridge_discovery_"))
    _copy_fixture(root / "flat_copy")            # 扁平布局
    _copy_fixture(root / "maibot" / "nested_copy")  # 分类目录布局
    host = _StubHost(root)

    from bridge.registry import BridgeRegistry

    registry = BridgeRegistry(host)
    await registry.start()
    try:
        keys = set(registry.plugins)
        assert any(key.endswith("flat_copy") for key in keys), keys
        assert any(key.endswith("nested_copy") for key in keys), keys
    finally:
        await registry.stop()


# --------------------------------------------------------------------------- #
# 发现：各框架单独的插件源目录（[bridge.framework_dirs]）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_framework_dir_loads_plugins_from_external_source() -> None:
    """框架源目录里的插件要能被发现，且公共根目录不会被代建出它的影子。"""
    root = Path(tempfile.mkdtemp(prefix="bot_bridge_fwroot_"))
    external = Path(tempfile.mkdtemp(prefix="bot_bridge_fwext_"))
    _copy_fixture(external / "hello_ext")
    host = _StubHost(root)
    host.settings = {**host.settings, "framework_dirs": {"maibot": str(external)}}

    from bridge.registry import BridgeRegistry

    registry = BridgeRegistry(host)
    await registry.start()
    try:
        assert "maibot:hello_ext" in registry.plugins
        # 根目录扫描照常工作，两边互不干扰
        # 比真实路径：Windows runner 上 tempfile.mkdtemp 可能给 8.3 短名
        # （C:/Users/RUNNER~1/...），与插件自己解析出的长名不是同一个字符串。
        assert Path(registry.get("maibot:hello_ext").manifest.path).resolve() == (
            external / "hello_ext"
        ).resolve()
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_framework_dir_wins_over_plugins_root_on_same_key() -> None:
    """同一插件同时出现在框架源目录与 plugins_root：框架源目录优先。"""
    root = Path(tempfile.mkdtemp(prefix="bot_bridge_fwprio_"))
    external = Path(tempfile.mkdtemp(prefix="bot_bridge_fwprio_ext_"))
    _copy_fixture(root / "maibot" / "dup")
    _copy_fixture(external / "dup")
    host = _StubHost(root)
    host.settings = {**host.settings, "framework_dirs": {"maibot": str(external)}}

    from bridge.registry import BridgeRegistry

    registry = BridgeRegistry(host)
    await registry.start()
    try:
        loaded = registry.get("maibot:dup")
        assert loaded is not None
        assert Path(loaded.manifest.path).resolve() == (external / "dup").resolve()
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_framework_dir_missing_is_skipped_without_create() -> None:
    """配置的框架源目录不存在：告警跳过，**不代建**（多半是路径写错）。"""
    root = Path(tempfile.mkdtemp(prefix="bot_bridge_fwmiss_"))
    ghost = root / "no_such_dir"
    host = _StubHost(root)
    host.settings = {**host.settings, "framework_dirs": {"maibot": str(ghost)}}

    from bridge.registry import BridgeRegistry

    registry = BridgeRegistry(host)
    await registry.start()
    try:
        assert not ghost.exists(), "不存在的源目录被悄悄创建了"
        assert any("不存在" in line and "no_such_dir" in line for line in host.lines), host.lines
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_framework_dir_unknown_framework_is_ignored() -> None:
    """framework_dirs 里写了未启用/不存在的框架名：忽略并告警，不影响其它框架。"""
    root = Path(tempfile.mkdtemp(prefix="bot_bridge_fwunk_"))
    other = Path(tempfile.mkdtemp(prefix="bot_bridge_fwunk_dir_"))
    _copy_fixture(other / "hello_orphan")  # 会被 koishi 源目录指过去，但 koishi 未启用
    host = _StubHost(root)
    host.settings = {**host.settings, "framework_dirs": {"koishi": str(other)}}

    from bridge.registry import BridgeRegistry

    registry = BridgeRegistry(host)
    await registry.start()
    try:
        assert registry.plugins == {}
        assert any("未启用或不存在" in line for line in host.lines), host.lines
        assert registry.status()["framework_dirs"] == {"koishi": str(other)}
    finally:
        await registry.stop()


# --------------------------------------------------------------------------- #
# 安全校验：report / enforce 两种模式
# --------------------------------------------------------------------------- #


def _plant_dangerous_file(plugin_dir: Path) -> None:
    """往夹具插件里种一个一眼危险但不会被 import 的文件（扫描器应命中）。"""
    (plugin_dir / "sec_probe.py").write_text("x = eval('1+1')\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_security_enforce_refuses_dangerous_plugin() -> None:
    root = Path(tempfile.mkdtemp(prefix="bot_bridge_secenf_"))
    _copy_fixture(root / "maibot" / "scanme")
    _plant_dangerous_file(root / "maibot" / "scanme")
    host = _StubHost(root)
    host.settings = {**host.settings, "security_scan": {"enabled": True, "mode": "enforce"}}

    from bridge.registry import BridgeRegistry

    registry = BridgeRegistry(host)
    await registry.start()
    try:
        assert "maibot:scanme" not in registry.plugins  # 高风险 → 拒绝加载
        reason = registry.plugin_errors.get("maibot:scanme", "")
        assert "安全扫描" in reason and "高风险" in reason, reason
        report = registry.status()["security_reports"].get("maibot:scanme") or {}
        assert report.get("dangerous", 0) >= 1  # 拒绝原因要可查（全文报告留着）
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_security_report_mode_loads_but_flags() -> None:
    root = Path(tempfile.mkdtemp(prefix="bot_bridge_secrep_"))
    _copy_fixture(root / "maibot" / "scanme")
    _plant_dangerous_file(root / "maibot" / "scanme")
    host = _StubHost(root)  # DEFAULT_SETTINGS 之外默认没有 security_scan 键 → report 兜底
    host.settings = {**host.settings, "security_scan": {"enabled": True, "mode": "report"}}

    from bridge.registry import BridgeRegistry

    registry = BridgeRegistry(host)
    await registry.start()
    try:
        assert "maibot:scanme" in registry.plugins  # report 模式照常加载
        assert any("高风险" in line and "scanme" in line for line in host.lines), host.lines
        row = registry.status()["plugins"][0]
        assert (row.get("security") or {}).get("dangerous", 0) >= 1
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_security_disabled_skips_scan() -> None:
    root = Path(tempfile.mkdtemp(prefix="bot_bridge_secoff_"))
    _copy_fixture(root / "maibot" / "scanme")
    _plant_dangerous_file(root / "maibot" / "scanme")
    host = _StubHost(root)
    host.settings = {**host.settings, "security_scan": {"enabled": False, "mode": "enforce"}}

    from bridge.registry import BridgeRegistry

    registry = BridgeRegistry(host)
    await registry.start()
    try:
        assert "maibot:scanme" in registry.plugins  # 关掉扫描后 enforce 也不拦
        assert registry.status()["security_scan"] == {"enabled": False, "mode": "enforce"}
        assert "maibot:scanme" not in registry.status()["security_reports"]
    finally:
        await registry.stop()


@pytest.mark.asyncio
async def test_security_scans_single_file_plugins() -> None:
    """单文件插件（nonebot 裸 ``.py``）经 registry 发现后同样受 enforce 拦截。"""
    root = Path(tempfile.mkdtemp(prefix="bot_bridge_secfile_"))
    source = FIXTURES / "nonebot" / "single_file_plugin.py"
    target = root / "single_file_plugin.py"
    shutil.copyfile(source, target)
    target.write_text(target.read_text(encoding="utf-8") + "\n\n__sec_probe = eval('1+1')\n", encoding="utf-8")
    host = _StubHost(root)
    host.settings = {
        **host.settings,
        "enabled_frameworks": ["nonebot"],
        "security_scan": {"enabled": True, "mode": "enforce"},
    }

    from bridge.registry import BridgeRegistry

    registry = BridgeRegistry(host)
    await registry.start()
    try:
        assert "nonebot:single_file_plugin" not in registry.plugins, "单文件插件必须同样受扫受拦"
        assert "安全扫描" in registry.plugin_errors.get("nonebot:single_file_plugin", "")
    finally:
        await registry.stop()


# --------------------------------------------------------------------------- #
# 路由：精确命令优先 + 点名
# --------------------------------------------------------------------------- #


class _StubRegistry:
    def __init__(self, plugins: dict[str, LoadedPlugin]) -> None:
        self.plugins = plugins

    def get(self, key: str) -> LoadedPlugin | None:
        return self.plugins.get(key)


def _loaded(plugin_id: str, spec: CommandSpec, answer: str) -> LoadedPlugin:
    """``plugin_id`` 是插件自己的名字；注册表键是 ``stub:<plugin_id>``（同真实注册表）。"""

    async def handle_message(context: CommandContext) -> CommandResult:
        return CommandResult(handled=True, plugin_id=plugin_id, command=spec.id, replies=[answer])

    return LoadedPlugin(
        manifest=PluginManifest(path=Path("."), plugin_id=plugin_id, framework="stub"),
        commands=[spec],
        handle_message=handle_message,
    )


def _key(plugin_id: str) -> str:
    return f"stub:{plugin_id}"


def _make_router(plugins: dict[str, LoadedPlugin]) -> tuple[BridgeRouter, _StubHost]:
    host = _StubHost(Path(tempfile.mkdtemp(prefix="bot_bridge_router_")))
    host.settings = {**host.settings, "enabled_frameworks": []}
    return BridgeRouter(object(), host, _StubRegistry(plugins)), host


@pytest.mark.asyncio
async def test_specific_command_beats_catch_all_listener() -> None:
    catch_all = _loaded("greedy", CommandSpec(id="message", patterns=(), priority=100), "catch-all")
    specific = _loaded("precise", CommandSpec(id="hello", patterns=("hello",), priority=1), "precise")
    # 故意让 catch-all 排在前面（字典序 + 它自己的优先级更高），只能是「精确优先」把它压下去。
    router, _host = _make_router({_key("greedy"): catch_all, _key("precise"): specific})

    result = await router.handle_text(CommandContext(text="hello 世界"))
    assert result.handled is True
    assert result.plugin_id == "precise"
    assert result.replies == ["precise"]


@pytest.mark.asyncio
async def test_handle_text_can_target_one_plugin() -> None:
    first = _loaded("first", CommandSpec(id="hello", patterns=("hello",)), "first")
    second = _loaded("second", CommandSpec(id="hello", patterns=("hello",)), "second")
    router, _host = _make_router({_key("first"): first, _key("second"): second})

    result = await router.handle_text(CommandContext(text="hello"), only="stub:second")
    assert result.plugin_id == "second"

    missing = await router.handle_text(CommandContext(text="hello"), only="stub:nope")
    assert missing.handled is False


@pytest.mark.asyncio
async def test_matched_replies_are_pushed_into_chat() -> None:
    """命中命令后，回复必须真的走 host.send_chat（而不是只停留在返回值里）。"""
    plugin = _loaded("precise", CommandSpec(id="hello", patterns=("hello",)), "来自插件的回复")
    router, host = _make_router({_key("precise"): plugin})

    await router.handle_text(CommandContext(text="hello"))
    await asyncio.sleep(0)
    assert host.pushes == [("stub:precise", "来自插件的回复")]


@pytest.mark.asyncio
async def test_slow_command_reports_our_own_timeout() -> None:
    """慢命令必须由**桥自己**超时并返回结构化错误，而不是让宿主把入口砍成「跑失败」。"""
    import asyncio

    async def slow_handler(context: CommandContext) -> CommandResult:
        await asyncio.sleep(1.0)
        return CommandResult(handled=True, plugin_id="slow", command="slow", replies=["太晚了"])

    plugin = LoadedPlugin(
        manifest=PluginManifest(path=Path("."), plugin_id="slow", framework="stub"),
        commands=[CommandSpec(id="slow", patterns=("slow",))],
        handle_message=slow_handler,
    )
    router, host = _make_router({_key("slow"): plugin})
    host.settings = {**host.settings, "call_timeout": 0.05}

    result = await router.handle_text(CommandContext(text="slow"))
    assert result.handled is False
    assert result.error and "timed out" in result.error, result.error
    assert any("timed out" in line for line in host.lines), host.lines[-3:]


@pytest.mark.asyncio
async def test_configured_call_timeout_is_clamped_to_the_host_budget() -> None:
    """配置里写 60s 也不能真按 60s 跑：宿主入口预算 ~30s，超了只会被砍成「跑失败」。"""
    from bridge.router import _DEFAULT_CALL_TIMEOUT, _CLAMP_WARNED, _resolve_call_timeout

    host = _StubHost(Path(tempfile.mkdtemp(prefix="bot_bridge_clamp_")))
    host.settings = {**host.settings, "call_timeout": 60.0}
    _CLAMP_WARNED.discard(id(host))

    assert _resolve_call_timeout(host) == _DEFAULT_CALL_TIMEOUT
    assert _resolve_call_timeout(host) == _DEFAULT_CALL_TIMEOUT  # 第二次不再重复告警
    warnings = [line for line in host.lines if "call_timeout" in line]
    assert len(warnings) == 1, warnings

    host.settings = {**host.settings, "call_timeout": 5.0}
    assert _resolve_call_timeout(host) == 5.0  # 比预算小就照用
