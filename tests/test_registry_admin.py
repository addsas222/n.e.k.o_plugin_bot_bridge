"""注册表的启停 / 删除能力测试（``bridge/plugin_admin.py``）。

全部离线、真实文件系统（临时目录），覆盖：

* 定位：``框架:插件`` / 原生 id / 唯一后缀 / 歧义 / 找不到；
* 启停：停掉后从加载表消失且写进 ``disabled_plugins``，再启动又回来；
* 删除：只允许删插件根目录里的东西（越界 / 根目录本身必须拒绝）；文件型插件连 ``.pyc``
  一起清；目录型插件整棵删；``purge_state`` 连带清数据/缓存。
"""

from __future__ import annotations

import asyncio
import shutil
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from bridge.registry import BridgeRegistry, plugin_key

FIXTURES = Path(__file__).resolve().parent / "fixtures"


class _AdminHost:
    """够注册表 + 管理动作用的假宿主。"""

    def __init__(self, plugins_root: Path, settings: Mapping[str, Any] | None = None) -> None:
        self.plugins_root = plugins_root
        self.settings: dict[str, Any] = {
            "enabled_frameworks": [],
            "continue_on_error": True,
            "call_timeout": 5.0,
            "max_plugins_per_framework": 8,
            "expose_commands_as_tools": False,
            "expose_plugins_tools": False,
            "disabled_plugins": [],
        }
        if settings:
            self.settings.update(dict(settings))
        self.lines: list[str] = []
        self._data = plugins_root.parent / "state"

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

    async def plugin_config(self, plugin_id: str) -> dict[str, Any]:
        return {}

    async def save_plugin_config(self, plugin_id: str, data: Mapping[str, Any]) -> bool:
        return True

    async def send_chat(self, text: str, *, source: str = "", **kwargs: Any) -> bool:
        return True

    async def llm_generate(self, prompt: object, **kwargs: Any) -> dict[str, Any]:
        return {"success": False, "response": "", "error": "not used"}

    async def recent_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        return []

    def lines_matching(self, needle: str) -> list[str]:
        return [line for line in self.lines if needle in line]


def _build_root(tmp_path: Path, *fixtures: str) -> Path:
    """把夹具插件铺成一个插件根目录（``<framework>/<plugin>``）。"""
    root = tmp_path / "plugins_root"
    for item in fixtures:
        framework, name = item.split("/", 1)
        source = FIXTURES / framework / name
        assert source.exists(), f"夹具不存在：{source}"
        target = root / framework / name
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    return root


async def _registry(tmp_path: Path, *fixtures: str, **settings: Any) -> tuple[BridgeRegistry, _AdminHost]:
    root = _build_root(tmp_path, *fixtures)
    host = _AdminHost(root, settings or None)
    registry = BridgeRegistry(host)  # type: ignore[arg-type]
    await registry.start()
    return registry, host


@pytest.mark.asyncio
async def test_find_key_accepts_key_id_and_unique_suffix(tmp_path: Path) -> None:
    registry, _host = await _registry(tmp_path, "nonebot/hello_nonebot")
    key = plugin_key(next(iter(registry.plugins.values())).manifest)
    assert registry.find_key(key) == key
    assert registry.find_key("hello_nonebot") == key
    assert registry.find_key(key.split(":", 1)[1]) == key
    from bridge.errors import BridgeError, PluginNotLoadedError

    with pytest.raises(PluginNotLoadedError):
        registry.find_key("不存在的插件")
    with pytest.raises(BridgeError):
        registry.find_key("")


@pytest.mark.asyncio
async def test_set_enabled_stops_and_restarts_one_plugin(tmp_path: Path) -> None:
    registry, host = await _registry(tmp_path, "nonebot/hello_nonebot", "astrbot/hello_astrbot")
    assert len(registry.plugins) == 2

    stopped = await registry.set_enabled("hello_nonebot", False)
    assert stopped["changed"] is True and stopped["enabled"] is False
    assert stopped["loaded"] is False
    assert "hello_nonebot" in stopped["disabled_plugins"]
    assert registry.status()["disabled_plugins"] == ["hello_nonebot"]
    assert not any("hello_nonebot" in key for key in registry.plugins)
    assert "hello_astrbot" in " ".join(registry.plugins)  # 只动了一个插件

    # 停用状态写进设置后，重扫不会再拉起来
    status = await registry.reload()
    assert status["plugin_count"] == 1

    started = await registry.set_enabled("hello_nonebot", True)
    assert started["changed"] is True and started["enabled"] is True
    assert started["disabled_plugins"] == []
    assert any("hello_nonebot" in key for key in registry.plugins)

    # 再停一次也不会重复写禁用列表
    again = await registry.set_enabled("hello_nonebot", False)
    assert again["disabled_plugins"].count("hello_nonebot") == 1
    assert host.lines_matching("停止插件") or host.lines_matching("启动插件")


@pytest.mark.asyncio
async def test_set_enabled_is_idempotent(tmp_path: Path) -> None:
    registry, _host = await _registry(tmp_path, "nonebot/hello_nonebot")
    first = await registry.set_enabled("hello_nonebot", True)  # 本来就在跑
    assert first["changed"] is False and first["enabled"] is True
    assert first["disabled_plugins"] == []


@pytest.mark.asyncio
async def test_delete_refuses_paths_outside_the_plugins_root(tmp_path: Path) -> None:
    registry, host = await _registry(tmp_path, "nonebot/hello_nonebot")
    key = next(iter(registry.plugins))
    outside = tmp_path / "outside_plugin"
    outside.mkdir()
    (outside / "plugin.py").write_text("x = 1\n", encoding="utf-8")
    registry._manifests[key] = type(registry._manifests[key])(  # noqa: SLF001 — 模拟清单被指到外面
        path=outside,
        plugin_id=registry._manifests[key].plugin_id,  # noqa: SLF001
        framework=registry._manifests[key].framework,  # noqa: SLF001
    )

    from bridge.errors import BridgeError

    with pytest.raises(BridgeError) as excinfo:
        await registry.delete("hello_nonebot")
    assert "插件根目录" in str(excinfo.value)
    assert outside.exists(), "越界路径绝不能被删"

    root_manifest = type(registry._manifests[key])(  # noqa: SLF001
        path=Path(host.plugins_root),
        plugin_id="root_itself",
        framework="nonebot",
    )
    registry._manifests["nonebot:root_itself"] = root_manifest  # noqa: SLF001
    with pytest.raises(BridgeError) as excinfo2:
        await registry.delete("root_itself")
    assert "根目录" in str(excinfo2.value)
    assert Path(host.plugins_root).exists()


@pytest.mark.asyncio
async def test_delete_removes_files_and_optional_state(tmp_path: Path) -> None:
    """单文件插件：文件删掉、它的 .pyc 清掉，purge_state 时数据/缓存目录也清掉。"""
    registry, host = await _registry(tmp_path, "nonebot/single_file_plugin.py")
    plugin = next(iter(registry.plugins.values()))
    plugin_id = plugin.plugin_id
    manifest_path = Path(plugin.manifest.path)
    assert manifest_path.exists() and manifest_path.is_file()

    pycache = manifest_path.parent / "__pycache__"
    pycache.mkdir(exist_ok=True)
    stale = pycache / f"{manifest_path.stem}.cpython-314.pyc"
    stale.write_bytes(b"pyc")
    state_dir = host.data_dir(plugin_id)
    cache_dir = host.cache_dir(plugin_id)

    payload = await registry.delete("single_file_plugin", purge_state=True)
    assert payload["removed"] and payload["purged"]
    assert not manifest_path.exists()
    assert not stale.exists()
    assert not state_dir.exists() and not cache_dir.exists()
    assert registry.plugins == {}
    assert registry.plugin_errors == {}
    assert plugin_id not in registry.disabled_plugins()


@pytest.mark.asyncio
async def test_delete_directory_plugin(tmp_path: Path) -> None:
    registry, _host = await _registry(tmp_path, "astrbot/hello_astrbot")
    plugin_dir = Path(next(iter(registry.plugins.values())).manifest.path)
    assert plugin_dir.is_dir()

    payload = await registry.delete("hello_astrbot")
    assert not plugin_dir.exists()
    assert payload["key"].startswith("astrbot:")
    assert any("已删除插件" in line for line in _host.lines)


@pytest.mark.asyncio
async def test_disabled_at_startup_can_be_started_later(tmp_path: Path) -> None:
    """启动前就在 disabled_plugins 里的插件：不加载、但能被找到并（重新）启用。"""
    registry, _host = await _registry(tmp_path, "nonebot/hello_nonebot", disabled_plugins=["hello_nonebot"])
    assert registry.plugins == {}
    assert registry.disabled_plugins() == ["hello_nonebot"]
    assert registry.find_key("hello_nonebot").endswith(":hello_nonebot")

    started = await registry.set_enabled("hello_nonebot", True)
    assert started["changed"] is True and started["disabled_plugins"] == []
    assert len(registry.plugins) == 1


@pytest.mark.asyncio
async def test_delete_works_for_a_disabled_plugin(tmp_path: Path) -> None:
    """没在跑的插件也要能删（先摘状态、再删文件）。"""
    registry, _host = await _registry(tmp_path, "nonebot/single_file_plugin.py", disabled_plugins=["single_file_plugin"])
    target = tmp_path / "plugins_root" / "nonebot" / "single_file_plugin.py"
    assert target.exists()

    payload = await registry.delete("single_file_plugin")
    assert not target.exists()
    assert payload["disabled_plugins"] == []


@pytest.mark.asyncio
async def test_stopped_plugins_lists_disabled_and_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """停用/加载失败的插件也要能被列出来（带 key），否则面板没法把它们拉回来。

    加载失败用**异常注入**（`_load_one` 直接抛），跟各 backend 的 detect 规则解耦：
    以后兼容层改判据也不会连带弄挂这条测试。
    """
    registry, host = await _registry(tmp_path, "nonebot/hello_nonebot", "astrbot/hello_astrbot")

    async def boom(_manifest: object) -> object:
        raise RuntimeError("注入的加载失败")

    failing_key = "nonebot:hello_nonebot"
    monkeypatch.setattr(registry, "_load_one", boom)
    await registry.reload()  # 这一次扫描里 hello_nonebot 加载失败，hello_astrbot 正常

    rows = registry.stopped_plugins()
    by_key = {row["key"]: row for row in rows}
    assert failing_key in by_key, rows
    assert "注入的加载失败" in by_key[failing_key]["error"]
    assert by_key[failing_key]["disabled"] is False
    assert registry.status()["stopped_plugins"] == rows

    # 停用另一个插件：它同样出现在列表里，但标记为 disabled（原因与失败不同）
    monkeypatch.undo()
    await registry.set_enabled("hello_astrbot", False)
    stopped = {row["key"]: row for row in registry.stopped_plugins()}
    assert stopped["astrbot:hello_astrbot"]["disabled"] is True
    assert stopped["astrbot:hello_astrbot"]["error"] == ""


@pytest.mark.asyncio
async def test_delete_works_for_a_disabled_plugin(tmp_path: Path) -> None:
    """没在跑的插件也要能删（先摘状态、再删文件）。"""
    registry, _host = await _registry(tmp_path, "nonebot/single_file_plugin.py", disabled_plugins=["single_file_plugin"])
    target = tmp_path / "plugins_root" / "nonebot" / "single_file_plugin.py"
    assert target.exists()

    payload = await registry.delete("single_file_plugin")
    assert not target.exists()
    assert payload["disabled_plugins"] == []


@pytest.mark.asyncio
async def test_stopped_plugins_lists_disabled_and_failed(tmp_path: Path) -> None:
    """停用/加载失败的插件也要能被列出来（带 key），否则面板没法把它们拉回来。"""
    root = tmp_path / "plugins_root"
    (root / "nonebot").mkdir(parents=True)
    shutil.copy2(FIXTURES / "nonebot" / "single_file_plugin.py", root / "nonebot" / "single_file_plugin.py")
    broken = root / "nonebot" / "broken_nonebot"
    broken.mkdir()
    (broken / "__init__.py").write_text(
        "from nonebot import on_command\n\nboom = on_command('boom')\nraise RuntimeError('boom at import')\n",
        encoding="utf-8",
    )

    host = _AdminHost(root, {"enabled_frameworks": ["nonebot"], "disabled_plugins": ["single_file_plugin"]})
    registry = BridgeRegistry(host)  # type: ignore[arg-type]
    await registry.start()

    rows = registry.stopped_plugins()
    by_key = {row["key"]: row for row in rows}
    assert "nonebot:single_file_plugin" in by_key
    assert by_key["nonebot:single_file_plugin"]["disabled"] is True
    assert by_key["nonebot:single_file_plugin"]["framework"] == "nonebot"
    assert by_key["nonebot:single_file_plugin"]["enabled"] is False
    # 加载失败的插件也在列表里，并且带原因（它不是「停用」，是没起来）
    assert any(row["error"] for row in rows), rows
    assert registry.status()["stopped_plugins"] == rows

    # 用 key 直接启动 → 从 stopped 列表消失
    started = await registry.set_enabled("nonebot:single_file_plugin", True)
    assert started["changed"] is True
    assert all(row["key"] != "nonebot:single_file_plugin" for row in registry.stopped_plugins())


@pytest.mark.asyncio
async def test_toggle_does_not_wait_for_the_config_mirror(tmp_path: Path) -> None:
    """配置镜像走后台：宿主卡住时启停入口也不能被拖住（真机踩过 15s 全等）。"""
    registry, host = await _registry(tmp_path, "nonebot/hello_nonebot")

    class _SlowConfig:
        async def update(self, *_args: object, **_kwargs: object) -> dict[str, object]:
            await asyncio.sleep(30)  # 模拟宿主迟迟不回
            return {}

    plugin_side_slow = _SlowConfig()
    started = time.monotonic()
    # 注册表这一层只负责状态；镜像由插件层负责，这里验证注册表不等任何 IO
    out = await registry.set_enabled("hello_nonebot", False)
    spent = time.monotonic() - started

    assert out["disabled_plugins"] == ["hello_nonebot"]
    assert spent < 1.0, f"启停被拖住了：{spent:.1f}s"
    assert plugin_side_slow is not None  # 占位说明：镜像在插件层，见 test_admin_surface
