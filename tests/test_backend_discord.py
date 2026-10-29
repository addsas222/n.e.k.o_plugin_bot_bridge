"""``bridge/backends/discordpy.py`` 的聚焦测试。

全部是同步测试 + ``asyncio.run``：不依赖 ``pytest-asyncio``（N.E.K.O. 的冻结解释器里
不保证有它），同时仍然真实地驱动协程。没有网络、没有 sleep。

覆盖：``detect`` 的正例 / 负例（含「别的框架的 ``@app.command``」与分类目录）、
``load`` 的规格提取（命令 / 组 / 工具 / 监听器 / native）、``handle_message``
（前缀 / 别名 / ``*, rest`` / 引号 / 类型转换 / 组与子命令 / Embed 文本化 /
on_message 监听器 / 未命中）、结构化错误（缺参数、回调抛错、冷却、能力缺失、
前置检查）、附件只记录不投递、``call_tool`` / ``call_api``、``dispatch_event``、
生命周期（``start`` / ``stop``）、单文件与「只有 Cog」形态、重复加载不串状态。

测试自带一个实现 ``HostServices`` 的假宿主（记录所有调用），因此不联网、不依赖 N.E.K.O.。
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "discord"
LIVE_PLUGINS = Path(__file__).resolve().parent / "live" / "plugins" / "discord"
PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = PLUGIN_ROOT / "bridge"
BACKENDS_DIR = PLUGIN_DIR / "backends"
PLUGIN_ID = "hello_discord"


def _import_backend_module() -> Any:
    """导入 ``bridge.backends.discordpy``。

    先按正常包路径导入；如果 ``bridge/backends/__init__.py``（由桥接核心维护，
    它 import 了 ``.contracts``）当前不可导入，就注册最小命名空间包后重试 ——
    测试只关心后端模块本身，不触发 ``load_backends()``。
    """
    root = str(PLUGIN_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        return importlib.import_module("bridge.backends.discordpy")
    except ImportError:
        for name, path in (("bridge", PLUGIN_DIR), ("bridge.backends", BACKENDS_DIR)):
            if name in sys.modules:
                continue
            namespace = types.ModuleType(name)
            namespace.__path__ = [str(path)]  # type: ignore[attr-defined]
            sys.modules[name] = namespace
        return importlib.import_module("bridge.backends.discordpy")


discord_backend = _import_backend_module()
_BRIDGE_PACKAGE = str(discord_backend.__package__).rsplit(".", 1)[0]
contracts = importlib.import_module(f"{_BRIDGE_PACKAGE}.contracts")
errors = importlib.import_module(f"{_BRIDGE_PACKAGE}.errors")

BACKEND = discord_backend.BACKEND
BACKEND_MODULE = discord_backend
CommandContext = contracts.CommandContext
ERRORS = errors

_SKIP_MARK = pytest.mark.skipif(not (FIXTURES / PLUGIN_ID).is_dir(), reason="discord fixtures are missing")


class FakeHost:
    """``HostServices`` 的最小实现：记录调用，不碰真实 N.E.K.O.。"""

    def __init__(self, tmp_path: Path, settings: dict[str, Any] | None = None) -> None:
        self._root = Path(tmp_path)
        self._root.mkdir(parents=True, exist_ok=True)
        self.settings: dict[str, Any] = {
            "discord_command_prefix": ["!", "/"],
            "discord_bot_id": "10000",
            "discord_bot_name": "N.E.K.O.",
        }
        if settings:
            self.settings.update(settings)
        self.logs: list[tuple[str, str, str]] = []
        self.chats: list[dict[str, Any]] = []
        self.llm_calls: list[Any] = []
        self.plugin_calls: list[tuple[str, str, dict[str, Any]]] = []

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

    async def llm_generate(self, prompt: Any, **kwargs: Any) -> dict[str, Any]:
        self.llm_calls.append(prompt)
        return {"success": True, "response": "fake-response", "reasoning": "", "model": "fake", "tool_calls": [], "error": ""}

    async def llm_embed(self, texts: Any) -> list[list[float]]:
        raise ERRORS.UnsupportedCapabilityError("llm.embed")

    async def call_plugin_entry(self, plugin_id: str, entry_id: str, params: Any = None, timeout: float = 30.0) -> dict[str, Any]:
        self.plugin_calls.append((plugin_id, entry_id, dict(params or {})))
        return {"success": True, "plugin_id": plugin_id, "entry_id": entry_id}

    async def list_plugins(self) -> list[dict[str, Any]]:
        return [{"plugin_id": "bot_bridge"}]

    async def recent_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        return []

    async def plugin_config(self, plugin_id: str) -> dict[str, Any]:
        return {}

    async def save_plugin_config(self, plugin_id: str, data: Any) -> bool:
        return True


def _load(host: FakeHost, fixture: str = PLUGIN_ID) -> Any:
    """detect + load 一个 fixture 插件。"""
    path = FIXTURES / fixture
    if not path.exists():
        path = FIXTURES / f"{fixture}.py"
    manifest = BACKEND.detect(path)
    assert manifest is not None, f"fixture {fixture} was not detected as a discord plugin"
    return BACKEND.load(manifest, host)


def _ctx(
    text: str,
    *,
    user_id: str = "42",
    user_name: str = "娜娜",
    is_group: bool = True,
    group_id: str = "7",
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


def _tool_for(loaded: Any, spec_id: str) -> Any:
    for tool in loaded.tools:
        if tool.name.endswith(f"_{spec_id}"):
            return tool
    raise AssertionError(f"no tool spec for command {spec_id!r}")


def _state_module(plugin_id: str = PLUGIN_ID) -> Any:
    """拿到插件模块对象（读它自己的模块级状态）。"""
    state = BACKEND.state_for(plugin_id)
    assert state is not None and state.module is not None
    return state.module


# --------------------------------------------------------------------------- #
# 契约 / detect
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_backend_exposes_framework_backend_contract(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    assert isinstance(host, contracts.HostServices)
    assert isinstance(BACKEND, contracts.FrameworkBackend)
    assert BACKEND.name == "discord"
    assert BACKEND.display_name == "discord.py cogs"
    assert BACKEND.requires == ()


@_SKIP_MARK
def test_detect_recognises_package_file_and_cog_only() -> None:
    package = BACKEND.detect(FIXTURES / PLUGIN_ID)
    assert package is not None
    assert package.framework == "discord"
    assert package.plugin_id == PLUGIN_ID
    assert package.name == "hello_discord"  # 来自 class X(commands.Cog, name=...)
    assert package.extra["kind"] == "package"
    assert str(package.extra["module_name"]).startswith(BACKEND_MODULE.MODULE_PREFIX)
    assert package.entry.endswith("__init__.py")
    scan = package.extra["scan"]
    assert scan["imports_discord"] is True
    assert {"cog", "command"} <= set(scan["markers"])
    assert scan["setup"] == "bot"
    assert "hello" in scan["command_names"]

    single = BACKEND.detect(FIXTURES / "single_file_discord.py")
    assert single is not None
    assert single.extra["kind"] == "file"
    assert single.extra["scan"]["setup"] == "client"  # 老式同步入口

    cog_only = BACKEND.detect(FIXTURES / "cog_only_discord.py")
    assert cog_only is not None
    assert cog_only.extra["scan"]["setup"] == ""
    assert "cog" in cog_only.extra["scan"]["markers"]


def test_detect_rejects_non_discord_paths(tmp_path: Path) -> None:
    assert BACKEND.detect(tmp_path / "missing") is None
    assert BACKEND.detect(FIXTURES / "not_discord") is None
    assert BACKEND.detect(FIXTURES / "not_discord" / "__init__.py") is None
    text_file = tmp_path / "notes.txt"
    text_file.write_text("import discord\n", encoding="utf-8")
    assert BACKEND.detect(text_file) is None
    private_module = tmp_path / "_hidden.py"
    private_module.write_text("from discord.ext import commands\n", encoding="utf-8")
    assert BACKEND.detect(private_module) is None
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    assert BACKEND.detect(empty_dir) is None


def test_framework_category_dir_is_not_a_plugin(tmp_path: Path) -> None:
    """``data/plugins/discord/`` 是分类目录，不能被当成本框架的插件。"""
    category = tmp_path / "discord"
    plugin_dir = category / "some_plugin"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text("from discord.ext import commands\n", encoding="utf-8")
    assert BACKEND.detect(category) is None
    assert BACKEND.detect(plugin_dir) is not None


def test_detect_ignores_comments_and_foreign_decorators(tmp_path: Path) -> None:
    commented = tmp_path / "commented.py"
    commented.write_text("# import discord\n# @bot.command()\nprint('hi')\n", encoding="utf-8")
    assert BACKEND.detect(commented) is None

    foreign = tmp_path / "foreign.py"
    foreign.write_text(
        "class App:\n"
        "    def command(self, name):\n"
        "        return lambda func: func\n"
        "app = App()\n"
        "@app.command('hello')\n"
        "async def hello(ctx):\n"
        "    return 'hello'\n",
        encoding="utf-8",
    )
    assert BACKEND.detect(foreign) is None

    real = tmp_path / "real_plugin.py"
    real.write_text("from discord.ext import commands\n\n\n@commands.command()\nasync def ping(ctx): ...\n", encoding="utf-8")
    manifest = BACKEND.detect(real)
    assert manifest is not None and manifest.plugin_id == "real_plugin"


def test_detect_weak_signal_requires_setup_and_token(tmp_path: Path) -> None:
    """只有 ``def setup(client)`` 时，源码里还要有 discord / cog / command 任一 token。"""
    silent = tmp_path / "silent_setup.py"
    silent.write_text("def setup(client):\n    return None\n", encoding="utf-8")
    assert BACKEND.detect(silent) is None

    noisy = tmp_path / "noisy_setup.py"
    noisy.write_text(
        '"""在 client 上注册 command 的老式插件。"""\n\n\ndef setup(client):\n    return None\n',
        encoding="utf-8",
    )
    manifest = BACKEND.detect(noisy)
    assert manifest is not None
    assert manifest.extra["scan"]["setup"] == "client"
    assert manifest.extra["scan"]["imports_discord"] is False


def test_detect_does_not_claim_other_frameworks_fixtures() -> None:
    """别的框架的插件目录绝不能被 discord 兼容层认领（否则注册表会被抢走）。"""
    other_roots = ["nonebot", "astrbot", "maibot"]
    checked = 0
    for name in other_roots:
        root = FIXTURES.parent / name
        if not root.is_dir():
            continue
        for child in sorted(root.rglob("*")):
            if "__pycache__" in child.parts or (child.is_dir() and not any(child.iterdir())):
                continue
            checked += 1
            assert BACKEND.detect(child) is None, f"discord backend claimed {child}"
    live_root = Path(__file__).resolve().parent / "live" / "plugins"
    for name in other_roots:
        one = live_root / name
        if not one.is_dir():
            continue
        for child in sorted(one.iterdir()):
            checked += 1
            assert BACKEND.detect(child) is None, f"discord backend claimed {child}"
    if checked < 5:
        pytest.skip(f"other frameworks' fixtures are not available (checked {checked})")


# --------------------------------------------------------------------------- #
# load / 规格
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_load_extracts_specs(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    assert loaded.plugin_id == PLUGIN_ID
    assert loaded.framework == "discord"
    assert loaded.manifest.name == "hello_discord"
    # ``instance`` 是用于承载插件的 shim Bot（有命令处理入口，user 来自桥接配置）
    assert hasattr(loaded.instance, "process_commands")
    assert loaded.instance.user is not None
    assert loaded.instance.user.name == "N.E.K.O."

    ids = {command.id for command in loaded.commands}
    assert {"hello", "add", "quote", "info", "needarg", "mention", "admin", "admin_ping", "slow", "boom"} <= ids
    hello = next(command for command in loaded.commands if command.id == "hello")
    assert hello.patterns == ("!hello", "!hi", "!你好", "/hello", "/hi", "/你好")
    assert hello.aliases == ("hi", "你好")
    assert hello.usage == "!hello [name]"
    assert "打个招呼" in hello.description
    admin_ping = next(command for command in loaded.commands if command.id == "admin_ping")
    assert admin_ping.patterns == ("!admin ping", "/admin ping")
    assert admin_ping.usage == "!admin ping [text]"

    # 每条命令都导出成 LLM 可调用工具（discord.py 没有原生工具概念，这是显式映射）
    assert len(loaded.tools) == len(loaded.commands)
    assert all(tool.origin == f"discord:{PLUGIN_ID}" for tool in loaded.tools)
    tool = _tool_for(loaded, "hello")
    assert tool.parameters is not None and tool.parameters["required"] == ["text"]
    assert "discord.py 命令 hello" in tool.description

    # 监听器 → EventSpec（事件名保持 discord 原生写法）
    assert {event.event_type for event in loaded.events} == {
        "on_message",
        "on_ready",
        "on_command_error",
        "on_member_join",
    }

    native = loaded.native
    assert native["framework"] == "discord"
    assert native["prefixes"] == ["!", "/"]
    assert native["scan"]["setup"] == "bot"
    assert native["bot"]["name"] == "N.E.K.O."
    assert [cog["name"] for cog in native["cogs"]] == ["hello_discord"]
    assert "on_message" in native["listeners"]

    # 加载期日志通过 HostServices.log 转发（兼容层自己的日志用插件 id 做 source）
    assert any(source == PLUGIN_ID for _, source, _ in host.logs)


@_SKIP_MARK
def test_load_is_idempotent(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    shim_root = str(BACKEND_MODULE.SHIM_ROOT)
    first = _load(host)
    second = _load(host)
    assert [command.id for command in first.commands] == [command.id for command in second.commands]
    assert len(second.tools) == len(first.tools)
    assert sys.path.count(shim_root) == 1
    assert first.instance is not second.instance


@_SKIP_MARK
def test_repeated_load_does_not_share_state(tmp_path: Path) -> None:
    """两次加载各自独立：旧实例的命令仍然可用，状态计数器互不影响。"""
    first = _load(FakeHost(tmp_path / "first"))
    second = _load(FakeHost(tmp_path / "second"))

    async def scenario() -> tuple[Any, Any, Any]:
        await BACKEND.start(first)
        await BACKEND.start(second)
        first_result = await first.handle_message(_ctx("!states"))
        second_result = await second.handle_message(_ctx("!states"))
        return first_result, second_result, await first.handle_message(_ctx("!hello 世界"))

    first_states, second_states, hello = asyncio.run(scenario())
    assert first_states.handled and second_states.handled
    assert "loads=1" in first_states.replies[0]
    assert "loads=1" in second_states.replies[0]
    assert "ready=1" in first_states.replies[0]
    assert hello.handled is True and hello.replies == ["你好，世界！"]
    # 两个实例是不同的 Bot（不会互相顶掉）
    assert first.instance is not second.instance


@_SKIP_MARK
def test_load_works_from_a_worker_thread(tmp_path: Path) -> None:
    """注册表用 ``asyncio.to_thread`` 调 ``load``：那个线程里没有运行中的事件循环，
    异步 ``setup(bot)`` 必须照常工作（按需新建一个独占循环）。"""
    host = FakeHost(tmp_path)

    async def scenario() -> Any:
        return await asyncio.to_thread(_load, host)

    loaded = asyncio.run(scenario())
    assert {command.id for command in loaded.commands} >= {"hello", "admin_ping"}

    async def dispatch() -> Any:
        await BACKEND.start(loaded)
        return await loaded.handle_message(_ctx("!hello 线程"))

    result = asyncio.run(dispatch())
    assert result.handled is True and result.replies == ["你好，线程！"]


@_SKIP_MARK
def test_load_from_a_running_loop_uses_a_helper_thread(tmp_path: Path) -> None:
    """调用方自己就在事件循环里：异步 ``setup(bot)`` 要放到辅助线程的独占循环里跑，
    不能因为「event loop is already running」而加载失败。"""
    host = FakeHost(tmp_path)
    manifest = BACKEND.detect(FIXTURES / PLUGIN_ID)
    assert manifest is not None

    async def scenario() -> Any:
        return BACKEND.load(manifest, host)

    loaded = asyncio.run(scenario())
    assert {command.id for command in loaded.commands} >= {"hello", "add"}
    assert loaded.manifest.plugin_id == PLUGIN_ID


def test_load_raises_plugin_load_error_for_broken_module(tmp_path: Path) -> None:
    manifest = BACKEND.detect(FIXTURES / "broken_discord")
    assert manifest is not None
    with pytest.raises(ERRORS.PluginLoadError) as info:
        BACKEND.load(manifest, FakeHost(tmp_path))
    assert "fixture import boom" in info.value.detail


def test_load_raises_plugin_load_error_for_broken_setup(tmp_path: Path) -> None:
    manifest = BACKEND.detect(FIXTURES / "bad_setup_discord")
    assert manifest is not None
    with pytest.raises(ERRORS.PluginLoadError) as info:
        BACKEND.load(manifest, FakeHost(tmp_path))
    assert "setup boom" in info.value.detail


@_SKIP_MARK
def test_single_file_and_cog_only_plugins(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    single = _load(host, "single_file_discord")
    cog_only = _load(host, "cog_only_discord")

    async def scenario() -> tuple[Any, Any, Any]:
        return (
            await single.handle_message(_ctx("!sfping")),
            await single.handle_message(_ctx("!sfs 你好")),
            await cog_only.handle_message(_ctx("!cogonly")),
        )

    ping, alias, auto = asyncio.run(scenario())
    assert ping.handled is True and ping.replies == ["sfpong"]
    assert alias.handled is True and alias.replies == ["复读：你好"]
    assert auto.handled is True and auto.replies == ["cog-only ok"]
    assert host.chats == []


# --------------------------------------------------------------------------- #
# 消息分发
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_handle_message_matches_command_and_collects_replies(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.handle_message(_ctx("!hello 世界"))

    result = asyncio.run(scenario())
    assert result.handled is True
    assert result.plugin_id == PLUGIN_ID
    assert result.framework == "discord"
    assert result.command == "hello"
    assert result.replies == ["你好，世界！"]
    assert result.error == ""
    # 关键：分发路径上的发言只进 replies，绝不直接推进 N.E.K.O.
    assert host.chats == []


@_SKIP_MARK
def test_handle_message_prefixes_aliases_and_arguments(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, ...]:
        return (
            await loaded.handle_message(_ctx("/hi 娜娜")),
            await loaded.handle_message(_ctx("!你好 小明")),
            await loaded.handle_message(_ctx("!hello")),
            await loaded.handle_message(_ctx("!add 21 21")),
            await loaded.handle_message(_ctx('!quote "hello world"')),
        )

    slash, chinese_alias, default, add, quote = asyncio.run(scenario())
    assert slash.replies == ["你好，娜娜！"]
    assert chinese_alias.replies == ["你好，小明！"]
    assert default.replies == ["你好，世界！"]  # 默认值
    assert add.replies == ["21 + 21 = 42"]  # 类型转换
    assert quote.replies == ["你说的是：hello world"]  # 引号算一个参数


@_SKIP_MARK
def test_handle_message_textifies_embeds(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.handle_message(_ctx("!info"))

    result = asyncio.run(scenario())
    assert result.handled is True
    assert len(result.replies) == 1
    text = result.replies[0]
    assert "插件信息" in text and "来自 discord.py 兼容层" in text
    assert "作者：tests" in text and "bot_bridge" in text
    assert host.chats == []


@_SKIP_MARK
def test_handle_message_group_subcommand(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any]:
        return (
            await loaded.handle_message(_ctx("!admin ping 你好")),
            await loaded.handle_message(_ctx("!admin")),
        )

    subcommand, missing = asyncio.run(scenario())
    assert subcommand.handled is True
    assert subcommand.command == "admin ping"
    assert subcommand.replies == ["admin ping：你好"]
    assert missing.handled is True
    assert missing.command == "admin"
    assert "requires a subcommand" in missing.error


@_SKIP_MARK
def test_handle_message_no_match_is_not_handled(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any]:
        return (
            await loaded.handle_message(_ctx("随便说点什么")),
            await loaded.handle_message(_ctx("!不存在的命令")),
        )

    unmatched, unknown = asyncio.run(scenario())
    assert unmatched.handled is False
    assert unmatched.replies == []
    assert unmatched.command == ""
    # 前缀命中了但命令不存在：不算「处理了」，也只是 debug 日志（不是错误）
    assert unknown.handled is False
    assert unknown.replies == []
    assert host.chats == []


@_SKIP_MARK
def test_on_message_listener_runs_and_can_reply(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any]:
        replied = await loaded.handle_message(_ctx("ping 一下"))
        silent = await loaded.handle_message(_ctx("路过"))
        return replied, silent

    replied, silent = asyncio.run(scenario())
    assert replied.handled is True
    assert replied.command == "on_message"  # 只有监听器回话时报出隐藏入口 id
    assert replied.replies == ["ping -> pong"]
    assert any(name.startswith("message:") for name in replied.data["listeners"])
    # 监听器跑了但没回话：不算接管（别的插件还有机会）
    assert silent.handled is False
    assert silent.replies == []
    assert any(name.startswith("message:") for name in silent.data["listeners"])
    assert host.chats == []


@_SKIP_MARK
def test_message_context_objects(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any, Any]:
        group = await loaded.handle_message(_ctx("!whoami"))
        private = await loaded.handle_message(_ctx("!whoami", is_group=False, group_id=""))
        info = await loaded.handle_message(_ctx("!botinfo"))
        return group, private, info

    group, private, info = asyncio.run(scenario())
    assert group.replies == ["娜娜@neko:7"]
    # 私聊没有服务器（``ctx.guild`` 是 None），频道名取私聊对象的名字（上游 DMChannel 同义）
    assert private.replies[0].startswith("娜娜@") and private.replies[0].endswith(":（私聊）")
    assert "N.E.K.O." in info.replies[0]
    assert "ready=False" in info.replies[0]  # start 之前还没就绪


# --------------------------------------------------------------------------- #
# 结构化错误
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_missing_argument_is_structured_error(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.handle_message(_ctx("!needarg"))

    result = asyncio.run(scenario())
    assert result.handled is True  # 命令名命中了，失败要如实报告
    assert "MissingRequiredArgument" in result.error
    assert "target" in result.error
    assert result.data["command_errors"][0]["type"] == "MissingRequiredArgument"
    # 插件自己的 on_command_error 监听器也会收到这个错误并回话
    assert result.replies and "参数不对" in result.replies[0]


@_SKIP_MARK
def test_command_callback_error_is_structured(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.handle_message(_ctx("!boom"))

    result = asyncio.run(scenario())
    assert result.handled is True
    assert "CommandInvokeError" in result.error
    assert "RuntimeError: boom" in result.error


@_SKIP_MARK
def test_cooldown_is_enforced(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any, Any]:
        return (
            await loaded.handle_message(_ctx("!slow")),
            await loaded.handle_message(_ctx("!slow")),
            await loaded.handle_message(_ctx("!slow", user_id="43")),
        )

    first, second, other_user = asyncio.run(scenario())
    assert first.replies == ["slow ok"]
    assert first.error == ""
    assert "CommandOnCooldown" in second.error
    assert other_user.replies == ["slow ok"]  # 按用户分桶


@_SKIP_MARK
def test_unsupported_capability_is_structured(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.handle_message(_ctx("!fetch 1"))

    result = asyncio.run(scenario())
    assert result.handled is True
    assert "UnsupportedCapabilityError" in result.error
    assert "fetch_user" in result.error
    assert result.data["unsupported"], "能力缺失要结构化记录下来"
    assert result.data["unsupported"][0]["code"] == "unsupported_capability"
    assert "no N.E.K.O. equivalent" in str(result.data["unsupported"][0]["error"])
    # 能力缺失也会记进插件的 native（面板 / 报告用）
    assert any("fetch_user" in item["capability"] for item in loaded.native["unsupported_capabilities"])


@_SKIP_MARK
def test_owner_check_uses_payload_data(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any, Any]:
        unknown = await loaded.handle_message(_ctx("!owner"))
        denied = await loaded.handle_message(_ctx("!owner", raw={"is_owner": False}))
        allowed = await loaded.handle_message(_ctx("!owner", raw={"is_owner": True}))
        return unknown, denied, allowed

    unknown, denied, allowed = asyncio.run(scenario())
    # 没有 is_owner 数据也没有配置 owner_ids：如实报能力缺失，不猜
    assert "UnsupportedCapabilityError" in unknown.error
    assert "is_owner" in unknown.error
    assert denied.error.startswith("NotOwner")
    assert denied.data["check_failures"][0]["command"] == "owner"
    assert allowed.replies == ["owner 你好"]


@_SKIP_MARK
def test_owner_check_uses_configured_owner_ids(tmp_path: Path) -> None:
    host = FakeHost(tmp_path, {"discord_owner_ids": ["42"]})
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any]:
        return (
            await loaded.handle_message(_ctx("!owner")),
            await loaded.handle_message(_ctx("!owner", user_id="43")),
        )

    allowed, denied = asyncio.run(scenario())
    assert allowed.replies == ["owner 你好"]
    assert denied.error.startswith("NotOwner")


@_SKIP_MARK
def test_attachment_send_is_recorded_not_faked(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.handle_message(_ctx("!sendfile"))

    result = asyncio.run(scenario())
    assert result.replies == ["这是附件："]  # 文本部分照常发
    assert result.data["attachments"][0]["name"] == "report.txt"
    assert result.data["attachments"][0]["size"] == 6
    assert host.chats == []  # 没有假装投递附件
    # 附件能力缺失要被记下来，并且日志的 source 带 discord: 前缀（可定位到插件）
    assert any("Attachment" in item["capability"] for item in loaded.native["unsupported_capabilities"])
    assert any(
        level == "warning" and "附件" in message and source == f"discord:{PLUGIN_ID}"
        for level, source, message in host.logs
    )


@_SKIP_MARK
def test_mention_converter_uses_visible_members(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)
    raw = {"members": [{"id": "42", "name": "娜娜"}, {"id": "99", "name": "小明"}]}

    async def scenario() -> tuple[Any, Any]:
        known = await loaded.handle_message(_ctx("!mention <@99>", raw=raw))
        unknown = await loaded.handle_message(_ctx("!mention <@12345>", raw=raw))
        return known, unknown

    known, unknown = asyncio.run(scenario())
    assert known.replies == ["成员：小明（99）"]
    assert "MemberNotFound" in unknown.error
    assert unknown.data["command_errors"][0]["type"] == "MemberNotFound"


@_SKIP_MARK
def test_guild_only_check_follows_context(tmp_path: Path) -> None:
    """``guild_only`` 只看上下文，可以如实判断（不需要任何权限数据）。"""
    host = FakeHost(tmp_path)
    loaded = _load(host)
    state = BACKEND.state_for(PLUGIN_ID)
    assert state is not None
    commands_module = importlib.import_module("discord.ext.commands")

    @commands_module.command(name="checkctx")
    @commands_module.guild_only()
    async def checkctx(ctx: Any) -> None:
        await ctx.send("guild ok")

    state.bot.add_command(checkctx)

    async def scenario() -> tuple[Any, Any]:
        return (
            await loaded.handle_message(_ctx("!checkctx")),
            await loaded.handle_message(_ctx("!checkctx", is_group=False, group_id="")),
        )

    allowed, denied = asyncio.run(scenario())
    assert allowed.replies == ["guild ok"]
    assert denied.handled is True
    assert denied.error.startswith("NoPrivateMessage")
    assert denied.data["check_failures"][0]["command"] == "checkctx"


@_SKIP_MARK
def test_permission_check_uses_payload_data(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any, Any]:
        unknown = await loaded.handle_message(_ctx("!purge 3"))
        denied = await loaded.handle_message(_ctx("!purge 3", raw={"permissions": {}}))
        allowed = await loaded.handle_message(
            _ctx("!purge 3", raw={"permissions": {"manage_messages": True}})
        )
        return unknown, denied, allowed

    unknown, denied, allowed = asyncio.run(scenario())
    # 没有权限数据：如实报能力缺失（不假装允许 / 拒绝）
    assert "UnsupportedCapabilityError" in unknown.error
    assert "has_permissions" in unknown.error
    # 有数据：按数据判断
    assert denied.error.startswith("MissingPermissions")
    assert denied.data["check_failures"][0]["command"] == "purge"
    assert allowed.replies == ["清理 3 条（示例，不会真的删）"]


# --------------------------------------------------------------------------- #
# 工具 / API / 事件
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_call_tool_reaches_command_without_prefix(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)
    tool = _tool_for(loaded, "hello")

    async def scenario() -> tuple[Any, Any]:
        hit = await loaded.call_tool(tool.name, {"text": "hello 娜娜"})
        prefixed = await loaded.call_tool(tool.name, {"text": "!hi 小明", "user_id": "99"})
        return hit, prefixed

    hit, prefixed = asyncio.run(scenario())
    assert hit["success"] is True
    assert hit["handled"] is True
    assert hit["replies"] == ["你好，娜娜！"]
    assert hit["text"] == "!hello 娜娜"  # 没带前缀时自动补第一个前缀
    assert prefixed["replies"] == ["你好，小明！"]
    assert host.chats == []


@_SKIP_MARK
def test_call_tool_and_api_failures_are_structured(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> list[BaseException]:
        errors_: list[BaseException] = []
        for coro in (
            loaded.call_tool("hello_discord_not_a_tool", {}),
            loaded.call_api("anything", {}),
        ):
            try:
                await coro
            except BaseException as exc:  # noqa: BLE001 — 测试要收集所有异常类型
                errors_.append(exc)
        return errors_

    collected = asyncio.run(scenario())
    assert [type(item).__name__ for item in collected] == [
        "UnsupportedCapabilityError",
        "UnsupportedCapabilityError",
    ]
    assert all(getattr(item, "code", "") == "unsupported_capability" for item in collected)
    assert "discord.tool.hello_discord_not_a_tool" in str(collected[0])
    assert "discord.api.anything" in str(collected[1])


@_SKIP_MARK
def test_dispatch_event_reaches_listeners(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any, Any]:
        join = await loaded.dispatch_event("on_member_join", {"user_id": "99", "user_name": "小明", "group_id": "7"})
        try:
            message_alias = await loaded.dispatch_event("message.received", {"text": "hi"})
        except BaseException as exc:  # noqa: BLE001 — 记录结构化失败
            message_alias = exc
        try:
            audio = await loaded.dispatch_event("audio", {})
        except BaseException as exc:  # noqa: BLE001
            audio = exc
        return join, message_alias, audio

    join, message_alias, audio = asyncio.run(scenario())
    assert join["success"] is True
    assert join["handled"] is True
    assert join["listeners"] == ["member_join:HelloDiscord.on_member_join"]
    assert getattr(_state_module(), "STATE")["joins"] == 1
    assert isinstance(message_alias, ERRORS.UnsupportedCapabilityError)
    assert "handle_message" in str(message_alias)
    assert isinstance(audio, ERRORS.UnsupportedCapabilityError)
    assert "discord.event.audio" in str(audio)


@_SKIP_MARK
def test_out_of_run_send_uses_host_chat(tmp_path: Path) -> None:
    """分发之外的发言（后台任务）走 HostServices.send_chat，而不是静默丢弃。"""
    host = FakeHost(tmp_path)
    loaded = _load(host)
    state = BACKEND.state_for(PLUGIN_ID)
    assert state is not None

    async def scenario() -> Any:
        message = state.factory.build(_ctx("!hello 世界"), state.bot)
        return await message.channel.send("后台播报")

    asyncio.run(scenario())
    assert host.chats == [
        {
            "text": "后台播报",
            "source": f"discord:{PLUGIN_ID}",
            "visibility": [],
            "ai_behavior": "",
            "priority": 5,
            "metadata": {"discord_out_of_run": True},
        }
    ]


# --------------------------------------------------------------------------- #
# 生命周期
# --------------------------------------------------------------------------- #


@_SKIP_MARK
def test_max_concurrency_blocks_overlapping_invocation(tmp_path: Path) -> None:
    """``max_concurrency`` 真实生效：同名命令并发第二次会被结构化拒绝。"""
    host = FakeHost(tmp_path)
    loaded = _load(host)
    commands_module = importlib.import_module("discord.ext.commands")
    state = BACKEND.state_for(PLUGIN_ID)
    assert state is not None

    async def scenario() -> tuple[Any, Any]:
        gate = asyncio.Event()
        entered = asyncio.Event()

        @commands_module.command(name="gated")
        @commands_module.max_concurrency(1, commands_module.BucketType.default)
        async def gated(ctx: Any) -> None:
            entered.set()
            await gate.wait()
            await ctx.send("gated done")

        state.bot.add_command(gated)
        first_task = asyncio.create_task(loaded.handle_message(_ctx("!gated")))
        await entered.wait()
        second = await loaded.handle_message(_ctx("!gated"))
        gate.set()
        return await first_task, second

    first, second = asyncio.run(scenario())
    assert first.handled is True and first.replies == ["gated done"]
    assert second.error.startswith("MaxConcurrencyReached")
    assert second.data["command_errors"][0]["type"] == "MaxConcurrencyReached"


@_SKIP_MARK
def test_hybrid_command_is_prefix_only_and_recorded(tmp_path: Path) -> None:
    """``hybrid_command`` 的前缀调用可用；斜杠部分如实登记为能力缺失。"""
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> Any:
        return await loaded.handle_message(_ctx("!hybrid 你好"))

    result = asyncio.run(scenario())
    assert result.handled is True
    assert result.replies == ["hybrid：你好"]
    capabilities = {item["capability"] for item in loaded.native["unsupported_capabilities"]}
    assert "commands.hybrid_command" in capabilities


@_SKIP_MARK
def test_load_extension_in_cog_load_refreshes_specs(tmp_path: Path) -> None:
    """``bot.load_extension``（上游常见装配方式）：``start`` 之后子扩展的命令也要进规格。"""
    host = FakeHost(tmp_path)
    loaded = _load(host, "extension_discord")
    assert {command.id for command in loaded.commands} == {"extping"}  # 子扩展还没装配

    async def scenario() -> tuple[Any, Any, Any, Any, Any, Any]:
        await BACKEND.start(loaded)
        state = BACKEND.state_for("extension_discord")
        assert state is not None
        sub = await loaded.handle_message(_ctx("!subping"))
        extra = await loaded.handle_message(_ctx("!extraping"))
        await BACKEND.stop(loaded)
        return (
            [command.id for command in loaded.commands],
            sub,
            extra,
            loaded.native,
            state.bot.get_command("subping"),
            getattr(_state_module("extension_discord"), "STATE"),
        )

    ids, sub, extra, native, removed, extension_state = asyncio.run(scenario())
    # 顺序 = 装配顺序（extping → subping → extraping）
    assert ids == ["extping", "subping", "extraping"]
    assert sub.replies == ["sub pong"]
    assert extra.replies == ["extra pong"]
    assert native["extensions"] == ["cogs.extra", "cogs.sub"]
    assert native["specs"]["commands"] == ids
    assert native["lifecycle"] == ["started", "stopped"]
    assert removed is None  # teardown 真的把子扩展摘走了
    assert extension_state["loaded"] == ["cogs.sub", "cogs.extra"]
    assert extension_state["unloaded"] == ["cogs.extra", "cogs.sub"]


@_SKIP_MARK
def test_start_and_stop_run_cog_lifecycle(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any, Any, Any, Any]:
        await BACKEND.start(loaded)
        after_start = await loaded.handle_message(_ctx("!states"))
        ready = await loaded.handle_message(_ctx("!botinfo"))
        await BACKEND.stop(loaded)
        after_stop = await loaded.handle_message(_ctx("!states"))
        return after_start, ready, after_stop, loaded.started, loaded.native

    after_start, ready, after_stop, started, native = asyncio.run(scenario())
    assert "loads=1" in after_start.replies[0]
    assert "ready=1" in after_start.replies[0]  # on_ready 监听器跑过一次
    assert "ready=True" in ready.replies[0]
    assert "unloads=1" in after_stop.replies[0]
    assert started is False
    assert native["started"] is False
    assert native["errors"] == []
    assert native["lifecycle"] == ["started", "stopped"]


@_SKIP_MARK
def test_stop_tolerates_half_started_plugin(tmp_path: Path) -> None:
    """没跑过 ``start`` 就 ``stop``：不能崩，也不能谎称跑过 ``cog_unload``。"""
    host = FakeHost(tmp_path)
    loaded = _load(host)

    async def scenario() -> tuple[Any, Any]:
        await BACKEND.stop(loaded)
        return await loaded.handle_message(_ctx("!states")), loaded.native

    result, native = asyncio.run(scenario())
    assert "unloads=0" in result.replies[0]
    assert native["started"] is False


# --------------------------------------------------------------------------- #
# 真机目录布局（桥接层把插件放进 <插件目录>/data/plugins/<框架>/）
# --------------------------------------------------------------------------- #


LIVE_PLUGIN = LIVE_PLUGINS / PLUGIN_ID


@pytest.mark.skipif(not LIVE_PLUGIN.is_dir(), reason="live discord fixture is missing")
def test_live_plugin_round_trip(tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    manifest = BACKEND.detect(LIVE_PLUGIN)
    assert manifest is not None
    assert manifest.plugin_id == PLUGIN_ID
    loaded = BACKEND.load(manifest, host)
    ids = {command.id for command in loaded.commands}
    assert {"hello", "echo", "dice"} <= ids

    async def scenario() -> tuple[Any, ...]:
        await BACKEND.start(loaded)
        return (
            await loaded.handle_message(_ctx("!hello 世界")),
            await loaded.handle_message(_ctx("/echo 复读一下")),
            await loaded.handle_message(_ctx("!dice 6")),
        )

    hello, echo, dice = asyncio.run(scenario())
    assert hello.handled is True and "世界" in hello.replies[0]
    assert echo.replies == ["复读：复读一下"]
    assert dice.replies[0].startswith("掷出了")


@pytest.mark.skipif(not (PLUGIN_ROOT / "data" / "plugins" / "discord").is_dir(), reason="live sample is not installed")
def test_live_sample_is_installed_next_to_other_frameworks() -> None:
    """``data/plugins/discord/<样例>`` 与其它框架的样例同构（真机验收用）。"""
    installed = PLUGIN_ROOT / "data" / "plugins" / "discord" / LIVE_PLUGIN.name
    assert installed.is_dir()
    assert BACKEND.detect(installed) is not None
