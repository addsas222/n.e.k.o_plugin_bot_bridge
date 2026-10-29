"""``bridge/backends/koishi.py`` 的聚焦测试。

分层覆盖：识别（package.json 形态）、运行目录物化（宿主脚本 + 桩 node_modules）、
真实 Node 宿主端到端（加载冒烟夹具 → 命令投影 → handle_message 调用 → 回显），
以及 node 缺失时的结构化失败。Node 不在 PATH 时运行时用例整体跳过。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import shutil
import sys
import types
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = PLUGIN_ROOT / "bridge"
BACKENDS_DIR = PLUGIN_DIR / "backends"
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "koishi" / "smoke"
PLUGIN_ID = "koishi-plugin-smoke"


def _import_backend_module() -> Any:
    root = str(PLUGIN_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        return importlib.import_module("bridge.backends.koishi")
    except ImportError:
        for name, path in (("bridge", PLUGIN_DIR), ("bridge.backends", BACKENDS_DIR)):
            if name in sys.modules:
                continue
            namespace = types.ModuleType(name)
            namespace.__path__ = [str(path)]  # type: ignore[attr-defined]
            sys.modules[name] = namespace
        return importlib.import_module("bridge.backends.koishi")


koishi_backend = _import_backend_module()
_BRIDGE_PACKAGE = str(koishi_backend.__package__).rsplit(".", 1)[0]
contracts = importlib.import_module(f"{_BRIDGE_PACKAGE}.contracts")
errors = importlib.import_module(f"{_BRIDGE_PACKAGE}.errors")

BACKEND = koishi_backend.BACKEND
ERRORS = errors

NODE_MISSING = shutil.which("node") is None

_skip_no_node = pytest.mark.skipif(NODE_MISSING, reason="node is not on PATH")
_skip_no_fixture = pytest.mark.skipif(not FIXTURE.is_dir(), reason="koishi fixture is missing")


class FakeHost:
    """``HostServices`` 的最小实现：记录调用，不碰真实 N.E.K.O.。"""

    def __init__(self, tmp_path: Path, settings: dict[str, Any] | None = None) -> None:
        self._root = Path(tmp_path)
        self._root.mkdir(parents=True, exist_ok=True)
        self.settings: dict[str, Any] = {"koishi_npm_install": False}
        if settings:
            self.settings.update(settings)
        self.logs: list[tuple[str, str, str]] = []
        self.sent: list[str] = []

    def log(self, level: str, message: str, *, source: str = "") -> None:
        self.logs.append((level, message, source))

    def data_dir(self, plugin_id: str) -> Path:
        path = self._root / "data" / plugin_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cache_dir(self, plugin_id: str) -> Path:
        path = self._root / "cache" / plugin_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def plugins_root(self) -> Path:
        return self._root / "plugins"

    async def send_chat(self, text: str, **_: Any) -> bool:
        self.sent.append(str(text))
        return True

    @property
    def text(self) -> str:
        return "\n".join(f"[{level}] {message}" for level, message, _ in self.logs)


def _make_manifest(path: Path) -> Any:
    return BACKEND.detect(path)


# --------------------------------------------------------------------------- #
# 识别
# --------------------------------------------------------------------------- #


@_skip_no_fixture
def test_detect_recognizes_koishi_plugin(tmp_path: Path) -> None:
    copied = Path(shutil.copytree(FIXTURE, tmp_path / "koishi-plugin-smoke"))
    manifest = BACKEND.detect(copied)
    assert manifest is not None
    assert manifest.plugin_id == PLUGIN_ID
    assert manifest.framework == "koishi"
    assert manifest.version == "1.0.0"
    assert manifest.entry == "index.js"


@_skip_no_fixture
def test_detect_rejects_non_koishi_directory(tmp_path: Path) -> None:
    plain = tmp_path / "plain-python-plugin"
    plain.mkdir()
    (plain / "plugin.py").write_text("print('hi')\n", encoding="utf-8")
    (plain / "package.json").write_text(
        json.dumps({"name": "plain-python-plugin"}), encoding="utf-8"
    )
    assert BACKEND.detect(plain) is None


# --------------------------------------------------------------------------- #
# 加载与命令调用（真实 Node 子进程）
# --------------------------------------------------------------------------- #


@_skip_no_node
@_skip_no_fixture
def test_load_projects_commands_and_invokes(tmp_path: Path) -> None:
    plugin_dir = Path(shutil.copytree(FIXTURE, tmp_path / "koishi-plugin-smoke"))
    manifest = _make_manifest(plugin_dir)
    host = FakeHost(tmp_path)

    loaded = BACKEND.load(manifest, host)
    assert loaded.framework == "koishi"
    assert [command.id for command in loaded.commands] == ["smoke"]
    assert loaded.commands[0].patterns[0] == "smoke"

    result = asyncio.run(loaded.handle_message(contracts.CommandContext(text="smoke hello")))
    assert result.handled is True
    assert result.framework == "koishi"
    assert result.command == "smoke"
    assert "pong: hello" in result.replies

    asyncio.run(BACKEND.stop(loaded))


@_skip_no_node
@_skip_no_fixture
def test_handle_message_ignores_unmatched_text(tmp_path: Path) -> None:
    plugin_dir = Path(shutil.copytree(FIXTURE, tmp_path / "koishi-plugin-smoke"))
    manifest = _make_manifest(plugin_dir)
    loaded = BACKEND.load(manifest, FakeHost(tmp_path))

    result = asyncio.run(loaded.handle_message(contracts.CommandContext(text="无关闲聊")))
    assert result.handled is False

    asyncio.run(BACKEND.stop(loaded))


@_skip_no_node
@_skip_no_fixture
def test_start_round_trips_through_call_id_protocol(tmp_path: Path) -> None:
    """backend.start() 必须走通 call_id 协议：宿主回 result，start 不超时。

    （回归：早先 request() 不带 call_id、宿主 start 分支也不回包，
    start() 每次干等 30s 后超时——插件等于不可用。）
    """
    plugin_dir = Path(shutil.copytree(FIXTURE, tmp_path / "koishi-plugin-smoke"))
    manifest = _make_manifest(plugin_dir)
    host = FakeHost(tmp_path)

    loaded = BACKEND.load(manifest, host)
    assert loaded.started is False
    asyncio.run(BACKEND.start(loaded))
    assert loaded.started is True
    assert loaded.native.proc.poll() is None  # 进程还活着，没有卡死被杀

    asyncio.run(BACKEND.stop(loaded))
    assert loaded.started is False


@_skip_no_node
@_skip_no_fixture
def test_load_fails_loudly_when_node_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    plugin_dir = Path(shutil.copytree(FIXTURE, tmp_path / "koishi-plugin-smoke"))
    manifest = _make_manifest(plugin_dir)
    host = FakeHost(tmp_path)
    with pytest.raises(errors.BackendUnavailableError) as excinfo:
        BACKEND.load(manifest, host)
    assert "Node.js" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 运行时物化
# --------------------------------------------------------------------------- #


@_skip_no_fixture
def test_runtime_materializes_shim_into_framework_root(tmp_path: Path) -> None:
    plugin_dir = Path(shutil.copytree(FIXTURE, tmp_path / "koishi" / "koishi-plugin-smoke"))
    manifest = _make_manifest(plugin_dir)
    host = FakeHost(tmp_path)

    BACKEND._materialize_runtime(plugin_dir.parent, manifest, host)

    framework_root = plugin_dir.parent
    assert (framework_root / "koishi-host.mjs").is_file()
    stub = framework_root / "node_modules" / "koishi" / "index.cjs"
    assert stub.is_file()
    # 内容相同就不重写（幂等）：mtime 不变
    BACKEND._materialize_runtime(framework_root, manifest, host)
    assert stub.is_file()


@_skip_no_fixture
def test_runtime_materialize_external_root_never_overwrites(tmp_path: Path) -> None:
    """外部插件源目录（framework_dirs 指到别处）：只补缺，绝不覆写已有文件。

    外部目录可能是一座真实的 Koishi 实例 —— 覆写它的 ``node_modules/koishi``
    等于拿桥的协议桩换掉人家的完整运行时。
    """
    plugin_dir = Path(shutil.copytree(FIXTURE, tmp_path / "external" / "koishi-plugin-smoke"))
    manifest = _make_manifest(plugin_dir)
    host = FakeHost(tmp_path)
    framework_root = plugin_dir.parent

    sentinel = b"// real koishi install - do not touch"
    real_stub = framework_root / "node_modules" / "koishi" / "index.cjs"
    real_stub.parent.mkdir(parents=True, exist_ok=True)
    real_stub.write_bytes(sentinel)

    BACKEND._materialize_runtime(framework_root, manifest, host, overwrite=False)

    assert real_stub.read_bytes() == sentinel  # 已存在的文件原样保留
    assert (framework_root / "koishi-host.mjs").is_file()  # 缺失的宿主脚本补上
    # 其余缺失的桩文件照常补齐（schemastery 桩没被预先创建）
    assert (framework_root / "node_modules" / "schemastery" / "index.cjs").is_file()
    # 覆写被跳过这件事必须能从日志里看见
    assert any("保留原文件" in msg for _, msg, _ in host.logs), host.text


@_skip_no_fixture
def test_runtime_materialize_owned_root_updates_stale_files(tmp_path: Path) -> None:
    """回归：桥自管目录（overwrite=True）里内容**过时**的宿主脚本/桩必须被替换。

    只断言「存在/幂等」抓不住这个回归：copy-on-differ 一旦丢失，shim 升级就再也
    传播不出去，且不会有任何报错。
    """
    plugin_dir = Path(shutil.copytree(FIXTURE, tmp_path / "koishi" / "koishi-plugin-smoke"))
    manifest = _make_manifest(plugin_dir)
    host = FakeHost(tmp_path)
    framework_root = plugin_dir.parent

    stale_host_script = framework_root / "koishi-host.mjs"
    stale_host_script.write_bytes(b"// stale host script")
    stale_stub = framework_root / "node_modules" / "koishi" / "index.cjs"
    stale_stub.parent.mkdir(parents=True, exist_ok=True)
    stale_stub.write_bytes(b"// stale stub")

    BACKEND._materialize_runtime(framework_root, manifest, host, overwrite=True)

    shim_source = (koishi_backend.SHIM_DIR / "koishi-host.mjs").read_bytes()
    assert stale_host_script.read_bytes() == shim_source  # 过时宿主脚本被替换
    assert stale_stub.read_bytes() == (koishi_backend._SHIM_NODE_MODULES / "koishi" / "index.cjs").read_bytes()
    assert not any("保留原文件" in msg for _, msg, _ in host.logs), host.text


# --------------------------------------------------------------------------- #
# 框架根归属（桥自管 vs 外部插件源目录）
# --------------------------------------------------------------------------- #


def test_owned_framework_root_covers_flat_and_nested_layouts(tmp_path: Path) -> None:
    """扁平（框架根 == plugins_root）与分类（<plugins_root>/koishi）都算桥自管。"""
    host = FakeHost(tmp_path)  # FakeHost.plugins_root == tmp_path/"plugins"
    plugins_root = host.plugins_root
    assert BACKEND._is_owned_framework_root(plugins_root / "koishi", host)  # 分类布局
    assert BACKEND._is_owned_framework_root(plugins_root, host)  # 扁平布局
    assert BACKEND._is_owned_framework_root(plugins_root / "sub" / "dir", host)  # 更深层也在桥内
    assert not BACKEND._is_owned_framework_root(tmp_path / "external", host)  # 外部源目录
    assert not BACKEND._is_owned_framework_root(plugins_root.parent, host)  # 根目录的上级不算


def test_owned_framework_root_without_plugins_root_attr(tmp_path: Path) -> None:
    """host 没有 plugins_root 属性时按 "." 兜底（与 registry.discover 同款回落）。"""

    class _BareHost:
        settings: dict[str, Any] = {}

    inside = Path.cwd() / "somewhere"  # 相对 "." 的子路径 → 归桥管
    assert BACKEND._is_owned_framework_root(inside, _BareHost())
    assert not BACKEND._is_owned_framework_root(tmp_path, _BareHost())  # 绝对外部路径


# --------------------------------------------------------------------------- #
# 外部目录不代跑 npm install
# --------------------------------------------------------------------------- #


def test_npm_install_skipped_for_external_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """外部插件源目录：即使 koishi_npm_install=True 也不往里面跑 npm install。"""
    calls: list[list[str]] = []
    monkeypatch.setattr(koishi_backend.shutil, "which", lambda name: "C:/fake/npm" if name == "npm" else None)
    monkeypatch.setattr(koishi_backend.subprocess, "run", lambda argv, **_: calls.append(argv))
    host = FakeHost(tmp_path, settings={"koishi_npm_install": True})
    plugin_dir = tmp_path / "external" / "koishi-plugin-with-deps"
    plugin_dir.mkdir(parents=True)

    BACKEND._install_npm_dependencies_if_needed(
        plugin_dir, {"name": "some-plugin", "dependencies": {"left-pad": "^1"}}, host, owned=False
    )
    assert calls == [], "外部目录不该被写 node_modules / package-lock.json"
    assert any("跳过 npm install" in msg for _, msg, _ in host.logs), host.text

    # 归桥管的目录维持原行为：会调用 npm install
    BACKEND._install_npm_dependencies_if_needed(
        plugin_dir, {"name": "some-plugin", "dependencies": {"left-pad": "^1"}}, host, owned=True
    )
    assert len(calls) == 1 and calls[0][0].endswith(("npm", "npm.exe"))


def test_backend_declares_no_python_requirements() -> None:
    """koishi 的依赖是 npm 的，不是 Python 的——requires 必须为空。"""
    assert tuple(BACKEND.requires) == ()
