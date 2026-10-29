"""``bridge/backends/hoshino.py`` 的聚焦测试。

覆盖：识别（模块目录 / 多文件目录 / 单文件 / 非 Hoshino）、加载（模块导入 + 规格提取 +
工具投影）、消息分发（命令 / 别名 / 前缀 / 后缀 / 关键词 / 全匹配 / 正则 / 消息监听 /
通知 / 请求）、``bot.finish`` 的收尾语义、分发内外的回复去向、能力缺失的结构化失败、
生命周期钩子、配置落地、启用开关、重复 load 的幂等、以及真机目录布局。

测试自带一个实现 ``HostServices`` 的假宿主（记录所有调用），因此不联网、不依赖 N.E.K.O.。
每个测试用 ``asyncio.run`` 驱动（不依赖 pytest-asyncio），并且不用真实延时。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hoshino"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = PLUGIN_ROOT / "bridge"
BACKENDS_DIR = PLUGIN_DIR / "backends"
PLUGIN_ID = "hello_hoshino"
BROKEN = "broken_hoshino"
V1_PLUGIN = "v1_direct_import"
SINGLE_FILE = "single_file_plugin.py"
MULTI_MODULE = "multi_module"


def _import_backend_module() -> Any:
    """导入 ``bridge.backends.hoshino``。

    先按正常包路径导入；如果 ``bridge/backends/__init__.py``（由桥接核心维护）当前不可
    导入，就注册最小命名空间包后重试 —— 测试只关心后端模块本身。
    """
    root = str(PLUGIN_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        return importlib.import_module("bridge.backends.hoshino")
    except ImportError:
        for name, path in (("bridge", PLUGIN_DIR), ("bridge.backends", BACKENDS_DIR)):
            if name in sys.modules:
                continue
            namespace = types.ModuleType(name)
            namespace.__path__ = [str(path)]  # type: ignore[attr-defined]
            sys.modules[name] = namespace
        return importlib.import_module("bridge.backends.hoshino")


hoshino_backend = _import_backend_module()
_BRIDGE_PACKAGE = str(hoshino_backend.__package__).rsplit(".", 1)[0]
contracts = importlib.import_module(f"{_BRIDGE_PACKAGE}.contracts")
errors = importlib.import_module(f"{_BRIDGE_PACKAGE}.errors")

BACKEND = hoshino_backend.BACKEND
BACKEND_MODULE = hoshino_backend
CommandContext = contracts.CommandContext
ERRORS = errors


def _unsupported(capability: str, exc: BaseException) -> bool:
    """能力缺失异常：shim 侧（``aiocqhttp.UnsupportedCapabilityError``）与桥接侧是**两个类**
    （shim 不依赖桥接包），因此统一按 ``code`` 判定。"""
    return getattr(exc, "code", "") == "unsupported_capability" and capability in str(exc)

_SKIP_MARK = pytest.mark.skipif(not (FIXTURES / PLUGIN_ID).is_dir(), reason="hoshino fixtures are missing")


class FakeHost:
    """``HostServices`` 的最小实现：记录调用，不碰真实 N.E.K.O.。"""

    def __init__(self, tmp_path: Path, settings: dict[str, Any] | None = None) -> None:
        self._root = Path(tmp_path)
        self._root.mkdir(parents=True, exist_ok=True)
        self.settings: dict[str, Any] = {
            "hoshino_command_start": ["/"],
            "hoshino_self_id": "10000",
            "hoshino_nickname": ["NEKO"],
        }
        if settings:
            self.settings.update(settings)
        self.logs: list[tuple[str, str, str]] = []
        self.chats: list[dict[str, Any]] = []
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

    def text(self) -> str:
        return "\n".join(f"{level} {source}: {message}" for level, source, message in self.logs)

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
            },
        )
        return True

    async def llm_generate(self, prompt: Any, **kwargs: Any) -> dict[str, Any]:
        return {"success": True, "response": "fake-response", "reasoning": "", "model": "fake", "tool_calls": [], "error": ""}

    async def llm_embed(self, texts: Any) -> list[list[float]]:
        raise ERRORS.UnsupportedCapabilityError("llm.embed")

    async def call_plugin_entry(self, plugin_id: str, entry_id: str, params: Any = None, timeout: float = 30.0) -> Any:
        return {"success": True}

    async def list_plugins(self) -> list[dict[str, Any]]:
        return [{"plugin_id": "bot_bridge"}]

    async def recent_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        return []

    async def plugin_config(self, plugin_id: str) -> dict[str, Any]:
        return {}

    async def save_plugin_config(self, plugin_id: str, data: Any) -> bool:
        self.saved_configs.append((plugin_id, dict(data)))
        return True


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _load(host: FakeHost, fixture: str = PLUGIN_ID) -> Any:
    """detect + load 一个 fixture 插件。"""
    path = FIXTURES / fixture
    if not path.exists():
        path = FIXTURES / f"{fixture}.py"
    manifest = BACKEND.detect(path)
    assert manifest is not None, f"fixture {fixture} was not detected as a hoshino plugin"
    return BACKEND.load(manifest, host)


def _ctx(
    text: str,
    *,
    user_id: str = "1001",
    is_group: bool = True,
    group_id: str = "2002",
    user_name: str = "测试用户",
    raw: dict[str, Any] | None = None,
) -> Any:
    return CommandContext(
        text=text,
        user_id=user_id,
        user_name=user_name,
        group_id=group_id if is_group else "",
        is_group=is_group,
        source="bot_bridge",
        raw=dict(raw or {}),
    )


def _command(loaded: Any, spec_id: str) -> Any:
    for spec in loaded.commands:
        if spec.id == spec_id:
            return spec
    raise AssertionError(f"no command spec with id {spec_id!r}: {[spec.id for spec in loaded.commands]}")


def _spec_id_for(loaded: Any, needle: str) -> str:
    for spec in loaded.commands:
        if needle in spec.patterns or needle in spec.name or needle in spec.usage:
            return spec.id
    raise AssertionError(f"no spec mentions {needle!r}: {[(s.id, s.patterns, s.name) for s in loaded.commands]}")


def _tool(loaded: Any, needle: str) -> Any:
    for tool in loaded.tools:
        if needle in tool.description or needle in tool.name:
            return tool
    raise AssertionError(f"no tool mentions {needle!r}: {[t.name for t in loaded.tools]}")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# 契约 / 识别
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_backend_exposes_framework_backend_contract(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    assert isinstance(host, contracts.HostServices)
    assert isinstance(BACKEND, contracts.FrameworkBackend)
    assert BACKEND.name == "hoshino"
    assert BACKEND.display_name == "HoshinoBot (Arcadia)"
    assert BACKEND.requires == ()


@_SKIP_MARK
def test_detect_recognises_directory_file_and_multi_module() -> None:
    package = BACKEND.detect(FIXTURES / PLUGIN_ID)
    assert package is not None
    assert package.framework == "hoshino"
    assert package.plugin_id == PLUGIN_ID
    assert package.extra["kind"] == "package"
    assert package.entry.endswith("__init__.py")
    assert Path(package.entry).is_file()
    assert str(package.extra["module_name"]).startswith(BACKEND_MODULE.MODULE_PREFIX)
    assert package.name == "hello_hoshino"
    assert package.description.startswith("兼容层测试服务")
    services = package.extra["services"]
    assert services[0]["name"] == "hello_hoshino"
    assert services[0]["manage_priv"] == "priv.ADMIN"
    assert services[0]["enable_on_default"] is True
    assert services[0]["visible"] is True
    assert "import hoshino" in package.extra["features"]
    assert "Service(" in package.extra["features"]

    single = BACKEND.detect(FIXTURES / SINGLE_FILE)
    assert single is not None
    assert single.extra["kind"] == "file"
    assert single.plugin_id == "single_file_plugin"
    assert single.name == "single_file_hoshino"

    multi = BACKEND.detect(FIXTURES / MULTI_MODULE)
    assert multi is not None
    assert multi.extra["kind"] == "package"
    assert multi.plugin_id == MULTI_MODULE
    assert multi.entry.endswith("dice.py")
    assert multi.name == "multi_module_dice"


def test_detect_rejects_other_paths(tmp_path: Path) -> None:
    assert BACKEND.detect(tmp_path / "missing") is None
    assert BACKEND.detect(FIXTURES / "plain_module") is None
    assert BACKEND.detect(FIXTURES / "plain_module" / "plain.py") is None
    # 分类目录本身不是插件（桥接层把插件放进 ``<插件目录>/data/plugins/<框架>/``）
    assert BACKEND.detect(PLUGIN_ROOT / "data" / "plugins") is None
    assert BACKEND.detect(PLUGIN_ROOT / "data" / "plugins" / "astrbot") is None
    text_file = tmp_path / "notes.txt"
    text_file.write_text("from hoshino import Service\n", encoding="utf-8")
    assert BACKEND.detect(text_file) is None
    private = tmp_path / "_private.py"
    private.write_text("from hoshino import Service\n", encoding="utf-8")
    assert BACKEND.detect(private) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert BACKEND.detect(empty) is None
    # NoneBot2 形状（``on_command`` + ``on_keyword`` 但没有 hoshino 导入）不能被认领
    nonebot_dir = tmp_path / "nb2"
    nonebot_dir.mkdir()
    (nonebot_dir / "plugin.py").write_text(
        "from nonebot import on_command, on_keyword\n"
        "cmd = on_command('x')\n"
        "kw = on_keyword({'y'})\n",
        encoding="utf-8",
    )
    assert BACKEND.detect(nonebot_dir) is None


def test_detect_survives_broken_syntax(tmp_path: Path) -> None:
    broken = tmp_path / "syntax_error.py"
    broken.write_text("from hoshino import Service\nthis is not python(\n", encoding="utf-8")
    manifest = BACKEND.detect(broken)
    assert manifest is not None  # 粗匹配兜底：仍是 Hoshino 插件，加载时才报错
    bad = tmp_path / "no_hoshino.py"
    bad.write_text("this is not python(\n", encoding="utf-8")
    assert BACKEND.detect(bad) is None


# --------------------------------------------------------------------------- #
# 加载 / 规格
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_load_extracts_specs_and_tools(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    assert loaded.plugin_id == PLUGIN_ID
    assert loaded.framework == "hoshino"
    assert loaded.instance is not None
    # 清单被运行期信息补齐（detect 只能从源码里读 Service(...)）
    assert loaded.manifest.name == "hello_hoshino"
    assert loaded.manifest.description.startswith("兼容层测试服务")
    assert [item["name"] for item in loaded.manifest.extra["runtime_services"]] == [
        "hello_hoshino",
        "hello_hoshino_extra",
    ]

    ids = {spec.id for spec in loaded.commands}
    # 命令
    assert "command_hello" in ids
    hello = _command(loaded, "command_hello")
    assert hello.name == "hello"
    assert hello.patterns == ("hello", "打招呼")
    assert hello.aliases == ("打招呼",)
    assert "问好" in hello.description
    assert hello.usage == "hello / 打招呼 [参数]"
    assert {"command_counter", "command_secret", "command_diag", "command_announce", "command_unsupported"} <= ids
    # 触发器（id 是 ASCII 安全的，人类可读的触发词在 name / patterns / usage 里）
    prefix_spec = _command(loaded, _spec_id_for(loaded, "图"))
    assert prefix_spec.patterns[0] == "图"
    assert prefix_spec.usage == "prefix：图"
    assert _command(loaded, _spec_id_for(loaded, "结束")).patterns == ("结束",)
    assert _command(loaded, _spec_id_for(loaded, "签到")).patterns == ("签到",)
    assert _command(loaded, _spec_id_for(loaded, "帮助")).patterns == ("帮助",)
    rex_spec = _command(loaded, _spec_id_for(loaded, r"^\.r\s*(\d+)d(\d+)"))
    assert rex_spec.patterns[0].startswith("^")
    # 消息监听（没有确定触发词 → patterns 为空，作为兜底项）
    watcher = _command(loaded, _spec_id_for(loaded, "on_message(group)"))
    assert watcher.patterns == ()
    # 同目录兄弟模块也被加载（上游按文件加载目录插件）
    assert "command_extracmd" in ids
    assert _command(loaded, _spec_id_for(loaded, "extra_kw")).patterns == ("extra_kw",)

    # 工具：每个消息型处理器都投影成一个工具
    assert len(loaded.tools) == len(loaded.commands)
    assert all(tool.origin == f"hoshino:{PLUGIN_ID}" for tool in loaded.tools)
    assert all(tool.name.startswith(f"{PLUGIN_ID}_") for tool in loaded.tools)
    assert _tool(loaded, "问好").name == f"{PLUGIN_ID}_command_hello"

    # 事件订阅：on_notice / on_request
    assert sorted(event.event_type for event in loaded.events) == ["notice.group_increase", "request.group.add"]
    # Hoshino 没有"插件对外 API"这回事：不伪造
    assert loaded.apis == []


@_SKIP_MARK
def test_native_reports_shims_jobs_and_services(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)
    native = loaded.native

    assert native["framework"] == "hoshino"
    assert Path(str(native["shim"])).is_dir()
    assert Path(str(native["support_shim"])).is_dir()
    assert native["plugin_id"] == PLUGIN_ID
    assert [item["name"] for item in native["services"]] == ["hello_hoshino", "hello_hoshino_extra"]
    assert native["hooks"] == {"startup": 1, "shutdown": 1}
    assert [item["name"] for item in native["jobs"]] == ["nightly"]
    assert native["jobs"][0]["trigger"] == "cron"
    assert native["jobs"][0]["kwargs"] == {"hour": "5"}
    assert native["unsupported_calls"] == []
    assert native["import_errors"] == []
    assert native["enabled"] is True and native["started"] is False
    assert len(native["modules"]) == 2  # __init__.py + extra.py
    # 定时任务只登记不执行 → 至少有一条说明性 warning 日志
    assert any("定时任务" in message and level == "warning" for level, _, message in host.logs)


@_SKIP_MARK
def test_wildcard_module_dir_without_init_is_loaded_file_by_file(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host, MULTI_MODULE)
    assert {spec.name for spec in loaded.commands if spec.name.startswith("mm_") or spec.name == "roll"} == {"roll", "mm_ping"}
    assert len(loaded.native["modules"]) == 2  # dice.py + ping.py


@pytest.mark.parametrize("package", ["hoshino", "aiocqhttp"])
def test_foreign_shim_is_never_overwritten(
    package: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """解释器里已经有别人的 ``hoshino`` / ``aiocqhttp`` 时，兼容层必须拒绝接管（绝不混用）。"""
    foreign = types.ModuleType(package)
    foreign.__file__ = f"<foreign {package}>"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package, foreign)
    monkeypatch.setattr(hoshino_backend, "_shim_cache", None)
    with pytest.raises(ERRORS.BackendUnavailableError) as excinfo:
        hoshino_backend._ensure_shim()
    assert f"another '{package}' package is already imported" in str(excinfo.value)
    assert "cannot take over" in str(excinfo.value)


def test_broken_plugin_raises_structured_load_error(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    manifest = BACKEND.detect(FIXTURES / BROKEN)
    assert manifest is not None
    with pytest.raises(ERRORS.PluginLoadError) as excinfo:
        BACKEND.load(manifest, host)
    assert "intentional import-time failure" in str(excinfo.value)
    assert excinfo.value.plugin_ref == BROKEN
    assert excinfo.value.code == "plugin_load_failed"


def test_direct_nonebot_v1_import_is_rejected_with_explanation(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    manifest = BACKEND.detect(FIXTURES / V1_PLUGIN)
    assert manifest is not None
    with pytest.raises(ERRORS.PluginLoadError) as excinfo:
        BACKEND.load(manifest, host)
    detail = str(excinfo.value)
    assert "import 了 nonebot（v1 API" in detail
    assert "from hoshino.typing import" in detail


@_SKIP_MARK
def test_sibling_import_error_is_recorded_but_plugin_still_loads(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "partial_hoshino"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        "from hoshino import Service\n"
        "from hoshino.typing import CQEvent\n"
        "sv = Service('partial')\n"
        "@sv.on_command('ok')\n"
        "async def ok(bot, ev: CQEvent):\n"
        "    await bot.send(ev, 'ok')\n",
        encoding="utf-8",
    )
    (plugin_dir / "bad_sibling.py").write_text(
        "from hoshino import Service\n"
        "sv = Service('bad_sibling')\n"
        "raise RuntimeError('sibling boom')\n",
        encoding="utf-8",
    )
    host = FakeHost(tmp_path)
    manifest = BACKEND.detect(plugin_dir)
    assert manifest is not None
    loaded = BACKEND.load(manifest, host)
    assert [spec.id for spec in loaded.commands] == ["command_ok"]
    assert any("sibling boom" in item for item in loaded.native["import_errors"])
    assert any("bad_sibling.py 导入失败" in message for _, _, message in host.logs)


def _write_plugin(root: Path, name: str, body: str) -> Path:
    """在临时目录里造一个 Hoshino 插件（用于覆盖夹具之外的写法）。"""
    plugin_dir = root / name
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "__init__.py").write_text(body, encoding="utf-8")
    return plugin_dir


# --------------------------------------------------------------------------- #
# 消息分发
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_handle_message_matches_command_and_alias(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any, Any]:
        return (
            await loaded.handle_message(_ctx("/hello 世界")),
            await loaded.handle_message(_ctx("打招呼 大家")),
            await loaded.handle_message(_ctx("hello")),
        )

    direct, alias, bare = _run(scenario())
    for result in (direct, alias, bare):
        assert result.handled is True
        assert result.plugin_id == PLUGIN_ID and result.framework == "hoshino"
        assert result.command == "command_hello"
    assert direct.replies == ["你好，世界！"]
    assert alias.replies == ["你好，大家！"]
    assert bare.replies == ["你好，测试用户！"]  # 没给参数时用发送者昵称
    # at_sender=True：文本进回复，at 段记进结构化记录（N.E.K.O. 只承载文本）
    assert direct.data["output"][0]["at_sender"] is True
    assert direct.data["output"][0]["segments"] == [{"kind": "at", "data": {"qq": "1001"}}]
    assert host.chats == []  # 分发期间不直接推给宿主


@_SKIP_MARK
def test_session_style_command_reads_and_writes_config(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> list[Any]:
        return [
            await loaded.handle_message(_ctx("/counter")),
            await loaded.handle_message(_ctx("/counter 再加一次")),
            await loaded.handle_message(_ctx("/counter reset")),
        ]

    first, second, third = _run(scenario())
    assert first.replies == ["计数：1"]
    assert second.replies == ["计数：2"]
    assert third.replies == ["计数：1"]  # reset 后又 +1
    config_file = host.data_dir(PLUGIN_ID) / "config" / f"{PLUGIN_ID}.json"
    assert json.loads(config_file.read_text(encoding="utf-8"))["counter"] == 1


@_SKIP_MARK
def test_diag_command_sees_services_and_enable_state(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)
    result = _run(loaded.handle_message(_ctx("/diag")))
    assert result.handled is True
    assert "hello_hoshino" in result.replies[0]
    assert "hello_hoshino_extra" in result.replies[0]
    assert "enabled=True" in result.replies[0]


@_SKIP_MARK
def test_triggers_prefix_suffix_keyword_fullmatch_and_rex(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> dict[str, Any]:
        return {
            "prefix": await loaded.handle_message(_ctx("查图 公主连结")),
            "suffix": await loaded.handle_message(_ctx("帮忙 结束")),
            "keyword": await loaded.handle_message(_ctx("今天签到一下")),
            "fullmatch": await loaded.handle_message(_ctx("帮助")),
            "rex": await loaded.handle_message(_ctx(".r 3d6")),
            "rex_str": await loaded.handle_message(_ctx(".echo 你好呀")),
        }

    results = _run(scenario())
    assert results["prefix"].replies == ["贴图:公主连结"]  # 前缀被摘掉后传给 handler
    assert results["prefix"].data["matched_text"] == "查图"
    assert results["suffix"].replies == ["收尾:帮忙"]
    assert results["keyword"].replies == ["签到成功"]
    assert results["keyword"].data["matched_text"] == "签到"
    assert results["fullmatch"].replies == ["帮助：hello / counter / 图 / 签到"]
    assert results["rex"].replies == ["掷 3 个 6 面骰"]
    assert results["rex"].data["matched_text"] == r"^\.r\s*(\d+)d(\d+)"
    # 图片段不会假装发出去：记进结构化记录并写日志
    assert results["rex_str"].replies == ["复读:你好呀"]
    assert any(item.get("kind") == "image" for item in results["rex_str"].data["output"][-1]["segments"])
    assert any("只记录不发送" in message for _, _, message in host.logs)


@_SKIP_MARK
def test_fullmatch_does_not_match_longer_text(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.handle_message(_ctx("帮助我", is_group=False))

    result = _run(scenario())
    assert result.handled is False


@_SKIP_MARK
def test_message_listener_only_catches_group_messages(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any]:
        return (
            await loaded.handle_message(_ctx("随便说点什么")),
            await loaded.handle_message(_ctx("随便说点什么", is_group=False)),
        )

    group, private = _run(scenario())
    assert group.handled is True
    assert "message:group" in group.data["handler_keys"]
    assert private.handled is False
    assert private.command == ""
    assert any("看到群消息：随便说点什么" in message for _, _, message in host.logs)


@_SKIP_MARK
def test_bot_finish_stops_the_pipeline(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.handle_message(_ctx("/bye"))

    result = _run(scenario())
    assert result.handled is True
    assert result.replies == ["再见"]  # bot.finish 之后的 send 不再执行
    assert result.data["finished"] is True


@_SKIP_MARK
def test_command_that_raises_is_reported_not_swallowed(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.handle_message(_ctx("/unsupported"))

    result = _run(scenario())
    assert result.handled is True  # 上游语义：异常也算这条消息被消费
    assert "UnsupportedCapabilityError" in result.error
    assert "hoshino.util.silence" in result.error
    assert result.replies == []
    # 能力缺失必须留痕，绝不假装成功
    assert "hoshino.util.silence" in loaded.native["unsupported_calls"]
    assert any(level == "error" for level, _, _ in host.logs)


@_SKIP_MARK
def test_private_sessions_are_allowed_by_default(tmp_path: Path) -> None:
    """N.E.K.O. 的对话基本都是私聊：默认按 priv.PRIVATE 处理（上游对非群聊直接拒绝）。"""
    host = FakeHost(tmp_path)
    loaded = _load(host)
    result = _run(loaded.handle_message(_ctx("/hello 私聊", is_group=False)))
    assert result.handled is True
    assert result.replies == ["你好，私聊！"]

    strict = FakeHost(tmp_path / "strict", settings={"hoshino_allow_private": False})
    strict_loaded = _load(strict)
    blocked = _run(strict_loaded.handle_message(_ctx("/hello 私聊", is_group=False)))
    assert blocked.handled is False
    assert any("#permission" in item for item in blocked.data["skipped"])


@_SKIP_MARK
def test_only_to_me_is_respected(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host, SINGLE_FILE)  # fixture 里没有 only_to_me，先确认普通命令可用

    async def scenario() -> Any:
        result = await loaded.handle_message(_ctx("sfping"))
        return result

    assert _run(scenario()).replies == ["sfpong"]

    # 再验证 only_to_me 的判定：用 hello_hoshino 的 `secret`（only_to_me 默认 False）与
    # 显式构造的 to_me 标记。
    host2 = FakeHost(tmp_path / "two")
    loaded2 = _load(host2)
    to_me = _run(loaded2.handle_message(_ctx("/hello 世界", raw={"to_me": False}, is_group=False)))
    assert to_me.handled is True  # 私聊 to_me 恒为真
    at_me = _run(loaded2.handle_message(_ctx("[CQ:at,qq=10000] hello 世界")))
    assert at_me.handled is True
    nickname = _run(loaded2.handle_message(_ctx("NEKO hello 世界")))
    assert nickname.handled is True


def test_only_to_me_and_deny_tip(tmp_path: Path) -> None:
    """``only_to_me`` 的判定与服务被禁用时的 ``deny_tip``。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "gate_hoshino",
        "from hoshino import Service\n"
        "from hoshino.typing import CQEvent\n"
        "sv = Service('gate_hoshino')\n"
        "@sv.on_command('gated', only_to_me=True, deny_tip='服务已关闭')\n"
        "async def gated(bot, ev: CQEvent):\n"
        "    await bot.send(ev, 'gated ok')\n",
    )
    host = FakeHost(tmp_path)
    manifest = BACKEND.detect(plugin_dir)
    assert manifest is not None
    loaded = BACKEND.load(manifest, host)

    async def scenario() -> tuple[Any, Any, Any]:
        quiet = await loaded.handle_message(_ctx("gated"))  # 群里没 @ 机器人
        loud = await loaded.handle_message(_ctx("gated", raw={"to_me": True}))
        service = hoshino_backend._ensure_shim().service.Service.get_loaded_services()["gate_hoshino"]
        service.set_disable("2002")
        try:
            denied = await loaded.handle_message(_ctx("gated", raw={"to_me": True}))
        finally:
            service.set_enable("2002")
        return quiet, loud, denied

    quiet, loud, denied = _run(scenario())
    assert quiet.handled is False
    assert any("#only_to_me" in item for item in quiet.data["skipped"])
    assert loud.replies == ["gated ok"]
    assert denied.handled is False
    assert denied.replies == ["服务已关闭"]  # deny_tip 也走回复收集
    assert any("#service_disabled" in item for item in denied.data["skipped"])


@_SKIP_MARK
def test_unmatched_message_is_not_handled(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)
    result = _run(loaded.handle_message(_ctx("完全无关的一句话", is_group=False)))
    assert result.handled is False
    assert result.command == ""
    assert result.replies == []
    assert result.error == ""


@_SKIP_MARK
def test_deny_tip_when_service_disabled_for_group(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        # 直接调 Service 的分群开关（上游用法），然后看命令是否被拒
        service = loaded.native["services"][0]
        assert service["name"] == "hello_hoshino"
        from hoshino.service import Service  # 通过 sys.modules 里的 shim 取同一个类

        sv = Service.get_loaded_services()["hello_hoshino"]
        sv.set_disable("2002")
        try:
            return await loaded.handle_message(_ctx("/hello 世界"))
        finally:
            sv.set_enable("2002")

    result = _run(scenario())
    assert result.handled is False
    assert any("#service_disabled" in item for item in result.data["skipped"])
    saved = host.data_dir(PLUGIN_ID) / "service_config" / "hello_hoshino.json"
    assert json.loads(saved.read_text(encoding="utf-8"))["enable_group"] == ["2002"]


# --------------------------------------------------------------------------- #
# 通知 / 请求
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_notice_event_reaches_handler(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.dispatch_event(
            "notice.group_increase",
            {
                "post_type": "notice",
                "notice_type": "group_increase",
                "sub_type": "approve",
                "group_id": 2002,
                "user_id": 3003,
                "self_id": "10000",
            },
        )

    payload = _run(scenario())
    assert payload["success"] is True
    assert payload["handled"] is True
    assert payload["replies"] == ["欢迎新成员 3003"]
    assert payload["handlers"] == ["notice:group_increase"]


@_SKIP_MARK
def test_request_event_reaches_handler(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.dispatch_event(
            "request.group.add",
            {
                "post_type": "request",
                "request_type": "group",
                "sub_type": "add",
                "group_id": 2002,
                "user_id": 3003,
                "comment": "让我进来",
                "self_id": "10000",
            },
        )

    payload = _run(scenario())
    assert payload["handled"] is True
    assert payload["replies"] == ["收到入群申请（来自 3003）"]


@_SKIP_MARK
def test_unsupported_events_are_structured_failures(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> list[BaseException]:
        collected: list[BaseException] = []
        for coro in (
            loaded.dispatch_event("audio", {}),
            loaded.dispatch_event("message", {"post_type": "message"}),  # 缺 message_type
            loaded.call_tool("hello_hoshino_not_a_tool", {}),
        ):
            try:
                await coro
            except BaseException as exc:  # noqa: BLE001 — 测试要收集所有异常类型
                collected.append(exc)
        return collected

    collected = _run(scenario())
    assert [type(item).__name__ for item in collected] == [
        "UnsupportedCapabilityError",
        "UnsupportedCapabilityError",
        "UnsupportedCapabilityError",
    ]
    assert "hoshino.event.audio" in str(collected[0])
    assert "message_type" in str(collected[1])
    assert "hoshino.tool.hello_hoshino_not_a_tool" in str(collected[2])


# --------------------------------------------------------------------------- #
# 工具调用
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_call_tool_runs_command_and_trigger(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)
    hello_tool = _command(loaded, "command_hello")
    prefix_tool = _command(loaded, _spec_id_for(loaded, "图"))

    async def scenario() -> tuple[Any, Any, Any]:
        return (
            await loaded.call_tool(f"{PLUGIN_ID}_{hello_tool.id}", {"text": "hello 工具", "group_id": "7"}),
            await loaded.call_tool(f"{PLUGIN_ID}_{prefix_tool.id}", {"text": "图 猫猫"}),
            await loaded.call_tool(f"{PLUGIN_ID}_{prefix_tool.id}", {"text": "没有这个前缀"}),
        )

    command_result, trigger_result, unmatched = _run(scenario())
    assert command_result["success"] is True
    assert command_result["matched"] is True
    assert command_result["replies"] == ["你好，工具！"]
    assert trigger_result["replies"] == ["贴图:猫猫"]
    assert unmatched["matched"] is False
    assert unmatched["success"] is False
    assert any("not_matched" in item for item in unmatched["skipped"])
    assert unmatched["replies"] == []


# --------------------------------------------------------------------------- #
# 生命周期 / 配置 / 启用开关
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_start_and_stop_run_hooks(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any]:
        await BACKEND.start(loaded)
        started_flag = (loaded.started, loaded.native["started"])
        state = loaded.instance.state
        await BACKEND.stop(loaded)
        return started_flag, state

    started_flag, state = _run(scenario())
    assert started_flag == (True, True)
    assert state["startups"] == 1 and state["shutdowns"] == 1
    assert loaded.started is False and loaded.native["started"] is False
    assert loaded.native["hook_errors"] == []
    assert any("hello_hoshino 启动" in message for _, _, message in host.logs)
    assert any("hello_hoshino 停止" in message for _, _, message in host.logs)


@_SKIP_MARK
def test_hook_failure_is_recorded_not_fatal(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "rough_hoshino"
    plugin_dir.mkdir()
    (plugin_dir / "__init__.py").write_text(
        "from hoshino import Service\n"
        "from hoshino.typing import CQEvent\n"
        "sv = Service('rough')\n"
        "@sv.on_command('ok')\n"
        "async def ok(bot, ev: CQEvent):\n"
        "    await bot.send(ev, 'ok')\n"
        "@sv.on_startup\n"
        "async def boom():\n"
        "    raise RuntimeError('startup boom')\n",
        encoding="utf-8",
    )
    host = FakeHost(tmp_path)
    manifest = BACKEND.detect(plugin_dir)
    assert manifest is not None
    loaded = BACKEND.load(manifest, host)

    async def scenario() -> tuple[Any, str]:
        await BACKEND.start(loaded)
        return await loaded.handle_message(_ctx("/ok")), host.text()

    result, logs = _run(scenario())
    assert result.replies == ["ok"]  # 钩子失败不影响命令
    assert loaded.started is True
    assert any("startup boom" in item for item in loaded.native["hook_errors"])
    assert "startup boom" in logs


@_SKIP_MARK
def test_config_snapshot_and_plugin_config_helper(tmp_path: Path) -> None:
    host = FakeHost(
        tmp_path,
        settings={
            "hoshino_superusers": ["999"],
            "hoshino_command_start": ["/", "!"],
            "hoshino_config": {"CUSTOM_KEY": 42},
        },
    )
    _load(host)

    async def scenario() -> dict[str, Any]:
        from hoshino import config as hoshino_config

        values = hoshino_config.snapshot()
        values["plugin_config"] = await hoshino_config.plugin_config(PLUGIN_ID)
        return values

    values = _run(scenario())
    assert values["SUPERUSERS"] == ["999"]
    assert values["COMMAND_START"] == ["/", "!"]
    assert values["CUSTOM_KEY"] == 42
    assert values["RES_DIR"] == ""


@_SKIP_MARK
def test_explicitly_unsupported_config_objects(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    _load(host)

    from hoshino import config as hoshino_config

    with pytest.raises(NotImplementedError) as group_broadcast:
        hoshino_config.get_group_broadcast("2002")
    assert _unsupported("hoshino.config.get_group_broadcast", group_broadcast.value)
    with pytest.raises(NotImplementedError) as module_config:
        getattr(hoshino_config, "hourcall").HOUR_CALLS
    assert _unsupported("hoshino.config.hourcall.HOUR_CALLS", module_config.value)
    with pytest.raises(AttributeError):
        getattr(hoshino_config, "NOT_A_REAL_KEY")


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

    result, error = _run(scenario())
    assert result.handled is False
    assert result.data["disabled"] is True
    assert isinstance(error, ERRORS.BridgeError)


@_SKIP_MARK
def test_repeated_load_does_not_duplicate_state(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    first = _load(host)
    second = _load(host)

    assert len([s for s in first.commands]) == len([s for s in second.commands])
    assert {s.id for s in first.commands} == {s.id for s in second.commands}
    assert len(second.native["services"]) == 2  # 没有因为重复加载而翻倍

    async def scenario() -> Any:
        hit = await second.handle_message(_ctx("/hello 世界"))
        miss = await second.handle_message(_ctx("完全没命中任何触发器", is_group=False))
        return hit, miss

    hit, miss = _run(scenario())
    assert hit.replies == ["你好，世界！"]
    # 触发器也重新注册过（旧的在重新加载时被摘掉），不会出现重复回复
    assert hit.replies.count("你好，世界！") == 1
    assert miss.handled is False


# --------------------------------------------------------------------------- #
# 分发之外的发言
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_broadcast_inside_and_outside_a_run(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any]:
        inside = await loaded.handle_message(_ctx("/announce 大家好"))
        outside = await loaded.instance.announce("后台播报")
        await asyncio.sleep(0)  # 让回落投递的任务跑起来（没有真实延时）
        return inside, outside

    inside, outside = _run(scenario())
    assert inside.replies == ["公告:大家好", "scope=current_conversation"]
    assert inside.data["output"][0]["hoshino_broadcast"] is True
    assert outside == {"delivered": 0, "handed_off": 1, "dropped": 0, "targets": 1, "scope": "current_conversation"}
    assert [chart["text"] for chart in host.chats] == ["后台播报"]
    assert host.chats[0]["metadata"]["hoshino_broadcast"] is True
    assert host.chats[0]["source"] == PLUGIN_ID


@_SKIP_MARK
def test_unmapped_bot_actions_are_structured_failures(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    _load(host)

    from aiocqhttp import UnsupportedCapabilityError as ShimUnsupportedCapabilityError

    shim = hoshino_backend._ensure_shim()
    bot = shim.output.RUNTIME.any_bot()

    async def scenario() -> list[BaseException]:
        collected: list[BaseException] = []
        calls = (
            lambda: bot.get_group_member_info(group_id=1, user_id=2),
            lambda: bot.set_group_ban(group_id=1, user_id=2, duration=60),
            lambda: bot.call_action("get_group_list"),
            lambda: bot.something_new_in_onebot(),
        )
        for call in calls:
            try:
                await call()
            except BaseException as exc:  # noqa: BLE001 — 收集全部能力缺失（占位对象是同步抛错的）
                collected.append(exc)
        return collected

    collected = _run(scenario())
    assert all(isinstance(item, ShimUnsupportedCapabilityError) for item in collected)
    assert [item.capability for item in collected] == [
        "OneBot.get_group_member_info",
        "OneBot.set_group_ban",
        "OneBot.call_action",
        "OneBot.something_new_in_onebot",
    ]
    assert all(item.code == "unsupported_capability" for item in collected)
    assert collected[0].to_payload()["success"] is False


@_SKIP_MARK
def test_request_session_approve_is_explicitly_unsupported(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    _load(host)

    shim = hoshino_backend._ensure_shim()
    bot = shim.output.RUNTIME.any_bot()
    event = shim.build_event({"post_type": "request", "request_type": "group", "sub_type": "add", "user_id": 5})
    session = shim.request_session_cls(bot, event)

    async def scenario() -> BaseException | None:
        try:
            await session.approve()
        except BaseException as exc:  # noqa: BLE001 — 收集能力缺失
            return exc
        return None

    error = _run(scenario())
    assert isinstance(error, NotImplementedError)
    assert _unsupported("nonebot.RequestSession.approve", error)


@_SKIP_MARK
def test_command_session_multi_turn_helpers_are_explicitly_unsupported(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    _load(host)
    shim = hoshino_backend._ensure_shim()
    bot = shim.output.RUNTIME.any_bot()
    event = shim.build_event({"post_type": "message", "message_type": "group", "group_id": 1, "user_id": 2})
    session = shim.command_session_cls(bot, event, args_text="a b")

    assert session.current_arg_text == "a b"
    assert session.get_arg("first") == "a"
    assert session.arg == "b"
    assert session.current_arg_text == "b"
    assert session.get_arg("second") == "b"
    with pytest.raises(NotImplementedError) as missing:
        session.get_arg("third")  # 参数用完了：上游会发提示并等用户下一条消息，shim 显式不支持
    assert _unsupported("nonebot.CommandSession.get", missing.value)
    with pytest.raises(NotImplementedError) as pause:
        session.pause()
    assert _unsupported("nonebot.CommandSession.pause", pause.value)
    with pytest.raises(NotImplementedError) as switch:
        session.switch("other")
    assert _unsupported("nonebot.CommandSession.switch", switch.value)


# --------------------------------------------------------------------------- #
# hoshino.util / hoshino.R 的落地行为
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_util_helpers_land_on_host_data_dir(tmp_path: Path) -> None:
    """``util.load_config`` / ``save_config`` 落在数据目录；查资料类接口要么给真数据要么显式拒绝。"""
    host = FakeHost(tmp_path)
    _load(host)

    shim = hoshino_backend._ensure_shim()
    util = shim.util

    async def scenario() -> dict[str, Any]:
        with shim.output.RUNTIME.scope(PLUGIN_ID):
            first = util.load_config("some_module")
            saved_path = util.save_config("some_module", {"answer": 42})
            reloaded = util.load_config("some_module")
            event = shim.build_event(
                {
                    "post_type": "message",
                    "message_type": "group",
                    "group_id": 7,
                    "user_id": 11,
                    "sender": {"nickname": "阿米娅", "card": "小驴", "role": "admin"},
                },
            )
            with shim.output.RUNTIME.running_event(event):
                same = await util.get_group_member_info("1", 7, 11)
                me = await util.get_user_info("1", 11)
                try:
                    # 查别人 / 不在当前消息里的用户：N.E.K.O. 侧没有这种接口
                    await util.get_group_member_info("1", 7, 999)
                    other: BaseException | None = None
                except BaseException as exc:  # noqa: BLE001 — 收集能力缺失
                    other = exc
        return {"first": first, "path": saved_path, "reloaded": reloaded, "same": same, "other": other, "me": me}

    outcome = _run(scenario())
    assert outcome["first"] == {}
    assert outcome["path"] == host.data_dir(PLUGIN_ID) / "config" / "some_module.json"
    assert outcome["reloaded"] == {"answer": 42}
    assert outcome["same"]["card"] == "小驴" and outcome["same"]["role"] == "admin"
    assert outcome["same"]["_source"] == "event" and outcome["same"]["_partial"] is True
    assert outcome["me"]["nickname"] == "阿米娅"
    # 查别人：N.E.K.O. 没有群成员查询接口 → 显式失败，不伪造资料
    assert isinstance(outcome["other"], NotImplementedError)
    assert _unsupported("OneBot.get_group_member_info", outcome["other"])


@_SKIP_MARK
def test_util_misc_helpers(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    _load(host)
    shim = hoshino_backend._ensure_shim()
    util = shim.util

    limiter = util.FreqLimiter(60.0)
    assert limiter.check("k") is True
    limiter.start_cd("k")
    assert limiter.check("k") is False
    assert limiter.left_time("k") > 0

    daily = util.DailyNumberLimiter(2)
    assert daily.check("k") and daily.check("k")
    daily.increase("k")
    daily.increase("k")
    assert daily.check("k") is False
    daily.reset("k")
    assert daily.check("k") is True

    assert util.normalize_str("ＡＢＣ　") == "abc "  # NFKC + 小写
    assert util.render_list(["a", "b"], "标题") == "标题\n┣a\n┗b"
    assert util.image_to_base64(b"abc") == "base64://YWJj"
    assert util.image_to_base64("base64://YWJj") == "base64://YWJj"
    with pytest.raises(NotImplementedError):
        util.image_to_base64("https://example.invalid/x.png")
    with pytest.raises(TypeError):
        util.image_to_base64(12345)

    # 敏感词过滤器：没装词库时显式拒绝，装了之后真的过滤
    with pytest.raises(NotImplementedError) as no_words:
        util.gfw.filter("你好")
    assert _unsupported("hoshino.util.gfw.filter", no_words.value)
    words = tmp_path / "words.txt"
    words.write_text("广告\n代练\n", encoding="utf-8")
    assert util.gfw.parse(words) == 2
    assert util.filt_message("这是广告消息") == "这是**消息"


@_SKIP_MARK
def test_resource_handles_point_into_data_dir(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    _load(host)
    shim = hoshino_backend._ensure_shim()

    with shim.output.RUNTIME.scope(PLUGIN_ID):
        res_dir = shim.hoshino.R.res_dir()
        resource = shim.hoshino.R.img("pcr", "a.png")
        assert Path(res_dir) == host.data_dir(PLUGIN_ID) / "resources"
        assert resource.path.endswith("a.png")
        assert resource.exist is False
        cqcode = resource.cqcode
        assert cqcode.type == "image" and str(cqcode.data["file"]).startswith("file:///")
        with pytest.raises(NotImplementedError) as url_error:
            resource.url
        assert _unsupported("hoshino.R.url", url_error.value)
        outside = pytest.raises(ValueError)
    with outside:
        shim.hoshino.R.get("..", "escape.png")


# --------------------------------------------------------------------------- #
# Service.sucmd / message_preprocessor
# --------------------------------------------------------------------------- #


def test_sucmd_requires_superuser_and_private(tmp_path: Path) -> None:
    plugin_dir = _write_plugin(
        tmp_path,
        "sudo_hoshino",
        "from hoshino import Service, sucmd\n"
        "from hoshino.typing import CQEvent\n"
        "sv = Service('sudo_hoshino')\n"
        "@sucmd('sudo_test')\n"
        "async def sudo_test(bot, ev: CQEvent):\n"
        "    await bot.send(ev, 'superuser ok')\n",
    )
    host = FakeHost(tmp_path, settings={"hoshino_superusers": ["999"]})
    manifest = BACKEND.detect(plugin_dir)
    assert manifest is not None
    loaded = BACKEND.load(manifest, host)
    sudo_spec = _command(loaded, _spec_id_for(loaded, "sudo_test"))
    assert sudo_spec.name == "sudo_test" and sudo_spec.patterns == ("sudo_test",)

    async def scenario() -> tuple[Any, Any, Any]:
        return (
            await loaded.handle_message(_ctx("/sudo_test", user_id="999", is_group=False)),
            await loaded.handle_message(_ctx("/sudo_test", user_id="1001", is_group=False)),
            await loaded.handle_message(_ctx("/sudo_test", user_id="999", is_group=True, group_id="5")),
        )

    allowed, denied, not_private = _run(scenario())
    assert allowed.replies == ["superuser ok"]
    assert denied.handled is True and denied.replies == []  # 非超管：什么都没发生
    assert not_private.replies == ["> This command should only be used in private session."]


def test_message_preprocessor_runs_before_handlers(tmp_path: Path) -> None:
    plugin_dir = _write_plugin(
        tmp_path,
        "preproc_hoshino",
        "from hoshino import Service, message_preprocessor\n"
        "from hoshino.typing import CQEvent\n"
        "sv = Service('preproc_hoshino')\n"
        "seen = []\n"
        "@message_preprocessor\n"
        "async def watch(bot, event: CQEvent, state):\n"
        "    seen.append(event.plain_text)\n"
        "    event['preprocessed'] = True\n"
        "@sv.on_command('pp')\n"
        "async def pp(bot, ev: CQEvent):\n"
        "    await bot.send(ev, f\"pre={ev.get('preprocessed')}\")\n",
    )
    host = FakeHost(tmp_path)
    manifest = BACKEND.detect(plugin_dir)
    assert manifest is not None
    loaded = BACKEND.load(manifest, host)

    async def scenario() -> Any:
        return await loaded.handle_message(_ctx("/pp"))

    result = _run(scenario())
    assert result.replies == ["pre=True"]
    assert loaded.instance.seen == ["/pp"]


def test_preprocessor_can_stop_the_pipeline(tmp_path: Path) -> None:
    plugin_dir = _write_plugin(
        tmp_path,
        "blocking_hoshino",
        "from hoshino import Service, message_preprocessor\n"
        "from hoshino.typing import CQEvent\n"
        "from hoshino.control import CanceledException\n"
        "sv = Service('blocking_hoshino')\n"
        "@message_preprocessor\n"
        "async def block(bot, event: CQEvent, state):\n"
        "    if '屏蔽' in event.plain_text:\n"
        "        raise CanceledException('preprocessor refused')\n"
        "@sv.on_command('pp2')\n"
        "async def pp2(bot, ev: CQEvent):\n"
        "    await bot.send(ev, 'should not run')\n",
    )
    host = FakeHost(tmp_path)
    manifest = BACKEND.detect(plugin_dir)
    assert manifest is not None
    loaded = BACKEND.load(manifest, host)

    async def scenario() -> tuple[Any, Any]:
        return (
            await loaded.handle_message(_ctx("/pp2 屏蔽")),
            await loaded.handle_message(_ctx("/pp2")),
        )

    blocked, normal = _run(scenario())
    assert blocked.handled is True and blocked.replies == []
    assert "preprocessor" in blocked.data["handler_keys"][0]
    assert normal.replies == ["should not run"]


@_SKIP_MARK
def test_service_enable_group_enumeration_is_explicitly_unsupported(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    _load(host)
    shim = hoshino_backend._ensure_shim()
    service = shim.service.Service.get_loaded_services()["hello_hoshino"]

    async def scenario() -> BaseException | None:
        try:
            await service.get_enable_groups()
        except BaseException as exc:  # noqa: BLE001 — 收集能力缺失
            return exc
        return None

    error = _run(scenario())
    assert isinstance(error, NotImplementedError)
    assert _unsupported("Service.get_enable_groups", error)


# --------------------------------------------------------------------------- #
# 真机目录布局（桥接层把插件放进 <插件目录>/data/plugins/hoshino/）
# --------------------------------------------------------------------------- #

LIVE_PLUGINS_ROOT = PLUGIN_ROOT / "data" / "plugins" / "hoshino"


def test_framework_category_dir_is_not_a_plugin() -> None:
    """``data/plugins/hoshino/`` 是分类目录，不能被当成本框架的插件。"""
    for path in (LIVE_PLUGINS_ROOT, PLUGIN_ROOT / "data" / "plugins"):
        assert BACKEND.detect(path) is None


@pytest.mark.skipif(
    not (PLUGIN_ROOT / "tests" / "live" / "plugins" / "hoshino").is_dir(),
    reason="live hoshino fixtures are missing",
)
def test_live_plugin_round_trip(tmp_path: Path) -> None:
    live_root = PLUGIN_ROOT / "tests" / "live" / "plugins" / "hoshino"
    for plugin_dir in sorted(item for item in live_root.iterdir() if item.is_dir()):
        host = FakeHost(tmp_path / plugin_dir.name)
        manifest = BACKEND.detect(plugin_dir)
        assert manifest is not None, f"live plugin {plugin_dir.name} was not detected"
        loaded = BACKEND.load(manifest, host)
        assert loaded.commands, f"live plugin {plugin_dir.name} exposes no command"
        assert loaded.tools, f"live plugin {plugin_dir.name} exposes no tool"

        async def scenario(spec_id: str = loaded.commands[0].id, patterns: tuple[str, ...] = loaded.commands[0].patterns) -> Any:
            text = patterns[0] if patterns else ""
            return await loaded.call_tool(f"{loaded.plugin_id}_{spec_id}", {"text": text, "group_id": "1"})

        result = _run(scenario())
        assert result["matched"] is True, f"live plugin {plugin_dir.name} did not react to its own trigger"
