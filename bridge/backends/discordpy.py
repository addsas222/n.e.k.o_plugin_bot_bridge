"""discord.py（cogs）兼容层：把 ``discord.ext.commands`` 插件加载进 N.E.K.O.

插件形态（与上游一致，没有自己的清单文件）：

* **单文件扩展**：``some_cog.py``，里面 ``async def setup(bot)``（或同步 ``def setup(client)``）；
* **包扩展**：``mycog/__init__.py`` + ``setup(bot)``，也可以用 ``bot.load_extension("sub.cog")``
  加载同目录下的子扩展；
* **只有 Cog 类**：模块里定义了 ``class X(commands.Cog)`` 却没有 ``setup`` 时，兼容层会
  自己实例化并 ``add_cog``（这是 N.E.K.O. 侧的便利，上游要求写 ``setup``）。

识别（``detect``）：只读源码、不导入。判据见 :func:`_source_verdict`：源码里出现
discord 导入 / ``X.Cog`` 基类 / ``@X.command`` 等装饰器就是本框架插件；只有
``def setup(client)`` 这种弱信号时，还要求源码里出现 discord / cog / command 任一 token，
避免把别的框架的 ``setup`` 误判成 discord.py 插件。

能力映射（真实实现）：

=======================================  ======================================================
discord.py API                           桥到 N.E.K.O.
=======================================  ======================================================
``ctx.send`` / ``channel.send`` / ``message.reply``
                                         文本进 ``CommandResult.replies``（由 N.E.K.O. 决定显示）；
                                         分发之外（后台任务）的发言走 ``HostServices.send_chat``
``embed`` / ``embeds``                   ``Embed.to_dict()`` 文本化后同样进 replies
``File`` / ``Attachment`` 发送           N.E.K.O. 的 ``send_chat`` 只承载文本：结构化记录
                                         （``CommandResult.data["attachments"]``）+ 警告日志
``View`` / ``Button`` / ``Select``       N.E.K.O. 没有消息组件面：结构化记录 + 警告日志
``@commands.command`` / ``group``        ``CommandSpec``（同时导出成 ``ToolSpec``，按文本调用）
``@bot.listen`` / ``@commands.Cog.listener``
                                         ``EventSpec`` + ``dispatch_event``
``@commands.hybrid_command``             前缀调用可用；斜杠命令部分登记为能力缺失
``@commands.cooldown`` / ``max_concurrency``
                                         真实生效（进程内令牌桶 / 信号量）
``has_permissions`` / ``is_owner`` / ``has_role``
                                         按 ``CommandContext.raw`` 提供的
                                         ``permissions`` / ``bot_permissions`` / ``is_owner`` /
                                         ``roles`` / ``bot_roles`` 判断；没数据时抛能力缺失
``guild_only`` / ``dm_only``             按上下文判断（群聊 = 服务器，私聊 = 私信）
``cog_load`` / ``cog_unload``            ``start()`` / ``stop()``
``bot.load_extension``                   按插件目录解析并执行其 ``setup(bot)``
=======================================  ======================================================

没有 N.E.K.O. 等价实现的能力**一律**抛 ``discord._runtime.UnsupportedCapabilityError``
（在兼容层转成带 ``code="unsupported_capability"`` 的结构化失败），并登记到
``LoadedPlugin.native["unsupported_capabilities"]``。绝不假装成功。清单：

* 网关 / HTTP：``Client.run`` / ``start`` / ``login`` / ``close``、``fetch_*``、
  ``change_presence``、``Guild.fetch_members`` …
* 交互：``discord.app_commands``（``CommandTree.sync`` / ``fetch_commands`` 抛错；
  装饰器只做元数据记录）、``discord.Interaction`` 的一切响应动作
* 消息组件：``View`` / ``Button`` / ``Select`` / ``Modal`` 的交互回调
* 附件与 CDN：``File`` 上传、``Attachment.read/save``、``Message.edit/delete/pin``
* 目录数据：成员 / 频道 / 角色 / 用户的查询（只认本次消息上下文里已知的对象）
* 其它模块（导入即 ``ImportError``）：``discord.ui``、``discord.ext.tasks``、
  ``discord.ext.commands.HelpCommand`` 体系、``FlagConverter``、``discord.webhook``、
  ``discord.voice_client`` / ``discord.player`` / ``discord.sinks``、``discord.ext.bridge``

有意的差异（不是能力缺失）：

* N.E.K.O. 的 ``user_id`` / ``group_id`` 不是 snowflake：非数字 id 会用 SHA-1 映射成
  一个**稳定**的合成 id（``Author.id`` 可反复比对），原始字符串保留在
  ``message.raw["user_id"]`` 里；合成 id 上的 ``created_at`` / ``jump_url`` 没有真实含义。
* 群聊映射成 Discord「服务器 + 文本频道」，私聊映射成「DM 频道」（因此 ``guild``
  在私聊里是 ``None``，``guild_only`` 会如实失败，``dm_only`` 会如实通过）。
* 带前缀的文本才会被当成命令；``bridge_call_plugin_tool`` 调用工具时如果没带前缀，
  兼容层会自动补上第一个前缀（方便 LLM 只给 ``"hello 世界"``）。
* ``@bot.event`` 覆盖 ``on_message`` 时插件必须自己再调 ``process_commands``
  （与上游一致，行为不隐藏）。
* ``cog_load`` / ``cog_unload`` 由 ``start()`` / ``stop()`` 驱动；``await bot.add_cog(cog)``
  只表示「已登记」（``cog_load`` 还没跑），因此 ``setup(bot)`` 里直接 ``asyncio.create_task``
  的后台任务不会被 N.E.K.O. 承载（加载时会记警告日志），而后台循环请放进 ``cog_load``。
* ``start()`` 结束时（``cog_load`` / ``setup_hook`` / ``load_extension`` 都跑完之后）会
  **重新整理一次规格**，这样 ``bot.load_extension("cogs.x")`` 装配进来的命令 / 监听器
  也能出现在 N.E.K.O. 的命令列表里（``register_surfaces`` 在注册表 ``start`` 之后才跑）。
* 桥接层不提供 ``HelpCommand`` 体系；``ctx.send_help()`` 用真实注册数据生成最小帮助。
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib
import importlib.util
import inspect
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Any

from ..contracts import (
    CommandContext,
    CommandResult,
    CommandSpec,
    EventSpec,
    LoadedPlugin,
    PluginManifest,
    ToolSpec,
)
from ..errors import (
    BackendUnavailableError,
    BridgeError,
    PluginLoadError,
    UnsupportedCapabilityError,
)

__all__ = ["BACKEND", "DiscordPyBackend"]

#: 兼容 shim 的根目录：``<plugin>/shims/discordpy``（里面是 ``discord/`` 包）。
SHIM_ROOT = Path(__file__).resolve().parents[2] / "shims" / "discordpy"
#: 导入插件时使用的私有模块名前缀（避免与 N.E.K.O. 自己的模块同名）。
MODULE_PREFIX = "neko_bridge_discord_"
#: 单文件插件的文件后缀。
PYTHON_SUFFIX = ".py"
#: 扫描一个插件目录时最多读取的 Python 文件数（detect 要保持便宜）。
MAX_SCAN_FILES = 256
#: 扫描时最大的目录深度（插件目录本身为 0）。
MAX_SCAN_DEPTH = 2
#: 扫描时跳过的目录名（测试 / 文档 / 虚拟环境 / 桥接层自己的数据目录等）。
SKIP_DIR_NAMES = frozenset(
    {
        "__pycache__",
        ".git",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "tests",
        "test",
        "docs",
        "doc",
        "examples",
        "example",
        "build",
        "dist",
        "scripts",
        "website",
        "data",
        "cache",
        "store",
        "shims",
        "vendor",
    }
)

#: 默认前缀（用户可以用 ``discord_command_prefix`` 覆盖）。
DEFAULT_PREFIXES: tuple[str, ...] = ("!", "/")
#: 默认的机器人 id / 名字（N.E.K.O. 侧没有 Discord 账号，这些是配置里的替身）。
DEFAULT_BOT_ID = "10000"
DEFAULT_BOT_NAME = "N.E.K.O."

#: 支持的事件类型（``dispatch_event`` 的入参；``on_`` 前缀与大小写都不敏感）。
_SUPPORTED_EVENTS = (
    "message",
    "ready",
    "member_join",
    "member_remove",
    "member_update",
    "reaction_add",
    "reaction_remove",
    "guild_join",
    "guild_remove",
    "connect",
    "disconnect",
)
#: ``def setup(...)`` 弱信号里认定的参数名（说明它接的是客户端对象）。
_SETUP_PARAM_NAMES = frozenset({"bot", "client", "neko", "self"})
#: 装饰器名 -> 标记（discord.py 的命令装饰器）。
_DECORATOR_MARKERS = {
    "command": "command",
    "group": "command",
    "hybrid_command": "command",
    "hybrid_group": "command",
    "listener": "listener",
    "event": "listener",
    "listen": "listener",
}
#: 粗匹配用的正则（源码无法解析成 AST 时的 fallback，只认顶格导入）。
_IMPORT_RE = re.compile(r"(?m)^(?:from|import)\s+discord(?:\.|\s|$)")
_TOKEN_RE = re.compile(r"\b(discord|cog|command)\b", re.IGNORECASE)
_NON_ALNUM = re.compile(r"[^A-Za-z0-9_]+")
_SHIM_LOCK = threading.RLock()
_SETUP_TIMEOUT = 120.0


# --------------------------------------------------------------------------- #
# shim 加载
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Shim:
    """已导入的 shim 对象集合（缓存，避免反复 getattr）。"""

    discord: ModuleType
    runtime: Any
    bot_cls: type
    command_cls: type
    cog_cls: type
    message_cls: type
    embed_cls: type
    colour_cls: type
    attachment_cls: type
    partial_emoji_cls: type
    guild_cls: type
    role_cls: type
    text_channel_cls: type
    dm_channel_cls: type
    member_cls: type
    user_cls: type
    client_user_cls: type
    intents_cls: type
    permissions_cls: type


_shim_cache: _Shim | None = None


def _is_our_shim(module: object) -> bool:
    """判断 ``sys.modules['discord']`` 是不是本项目的 shim。"""
    return bool(getattr(module, "_NEKO_BRIDGE_SHIM", False))


def _ensure_shim() -> _Shim:
    """把 ``bot_bridge/shims/discordpy`` 放进 ``sys.path`` 并导入兼容 shim（幂等、线程安全）。"""
    global _shim_cache
    with _SHIM_LOCK:
        if _shim_cache is not None:
            return _shim_cache
        package_init = SHIM_ROOT / "discord" / "__init__.py"
        if not package_init.is_file():
            raise BackendUnavailableError("discord", f"compatibility shim not found at {package_init}")
        existing = sys.modules.get("discord")
        if existing is not None and not _is_our_shim(existing):
            raise BackendUnavailableError(
                "discord",
                "another 'discord' package is already imported in this process; "
                "the compatibility shim cannot take over",
            )
        root_str = str(SHIM_ROOT)
        if root_str not in sys.path:
            sys.path.insert(0, root_str)
        discord = importlib.import_module("discord")
        if not _is_our_shim(discord):
            raise BackendUnavailableError(
                "discord",
                f"imported 'discord' is not the bot_bridge shim: {getattr(discord, '__file__', '?')}",
            )
        commands = importlib.import_module("discord.ext.commands")
        runtime = importlib.import_module("discord._runtime")
        _shim_cache = _Shim(
            discord=discord,
            runtime=runtime,
            bot_cls=commands.Bot,
            command_cls=commands.Command,
            cog_cls=commands.Cog,
            message_cls=discord.Message,
            embed_cls=discord.Embed,
            colour_cls=discord.Colour,
            attachment_cls=discord.Attachment,
            partial_emoji_cls=discord.PartialEmoji,
            guild_cls=discord.Guild,
            role_cls=discord.Role,
            text_channel_cls=discord.TextChannel,
            dm_channel_cls=discord.DMChannel,
            member_cls=discord.Member,
            user_cls=discord.User,
            client_user_cls=discord.ClientUser,
            intents_cls=discord.Intents,
            permissions_cls=discord.Permissions,
        )
        return _shim_cache


# --------------------------------------------------------------------------- #
# 宿主能力面
# --------------------------------------------------------------------------- #


class _HostBridge:
    """把 ``discord._runtime`` 需要的宿主能力接到 ``HostServices``。"""

    def __init__(self, host: Any, plugin_id: str) -> None:
        self._host = host
        self._plugin_id = plugin_id

    def log(self, level: str, message: str, *, plugin_id: str = "", source: str = "") -> None:
        pid = plugin_id or self._plugin_id
        origin = f"discord:{pid}" if pid else "discord"
        if source and source not in ("discord", origin):
            origin = f"{origin}.{source}"
        self._host.log(level, str(message), source=origin)

    def data_dir(self, plugin_id: str) -> Path:
        return Path(self._host.data_dir(plugin_id or self._plugin_id))

    def cache_dir(self, plugin_id: str) -> Path:
        return Path(self._host.cache_dir(plugin_id or self._plugin_id))

    def settings(self) -> dict[str, Any]:
        return dict(getattr(self._host, "settings", None) or {})

    async def send_chat(self, text: str, *, plugin_id: str = "", metadata: dict[str, Any] | None = None) -> bool:
        pid = plugin_id or self._plugin_id
        try:
            return bool(
                await self._host.send_chat(
                    str(text),
                    source=f"discord:{pid}" if pid else "discord",
                    priority=5,
                    metadata=dict(metadata or {}),
                )
            )
        except BridgeError:
            raise
        except Exception as exc:  # noqa: BLE001 — 投递失败要如实记录，不能静默
            self.log("error", f"分发之外的发言投递失败：{type(exc).__name__}: {exc}", source=pid)
            return False


# --------------------------------------------------------------------------- #
# 清单 / 源码扫描
# --------------------------------------------------------------------------- #


@dataclass
class _SourceScan:
    """一次源码扫描的结果（detect 的判据，全部来自 AST / 粗匹配）。"""

    imports_discord: bool = False
    decorator_markers: set[str] = field(default_factory=set)
    cog_names: list[str] = field(default_factory=list)
    command_names: list[str] = field(default_factory=list)
    listener_names: list[str] = field(default_factory=list)
    setup_param: str = ""
    parse_error: bool = False
    token_hit: bool = False

    @property
    def has_setup(self) -> bool:
        return bool(self.setup_param)


class _ImportCollector(ast.NodeVisitor):
    """第一遍：判断源码是否 import 了 discord，并收集导入的装饰器名。"""

    def __init__(self) -> None:
        self.imports_discord = False
        self.decorator_markers: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "discord" or alias.name.startswith("discord."):
                self.imports_discord = True
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if module == "discord" or module.startswith("discord."):
            self.imports_discord = True
            for alias in node.names:
                if alias.name in _DECORATOR_MARKERS:
                    self.decorator_markers.add(_DECORATOR_MARKERS[alias.name])
        self.generic_visit(node)


#: 装饰器前缀里认可的「命令客户端」名字（避免把别的框架的 ``@app.command`` 误判成 discord）。
_DECORATOR_HEADS = frozenset({"commands", "bot", "client", "self", "discord", "cog"})
#: 装饰器链上允许出现的中间属性（``commands.Cog.listener``）。
_DECORATOR_MIDDLES = frozenset({"cog", "ext", "commands"})


class _SignalCollector(ast.NodeVisitor):
    """第二遍：收集 Cog 基类、命令 / 监听器装饰器、``setup`` 入口。"""

    def __init__(self, scan: _SourceScan) -> None:
        self.scan = scan

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if any(_is_cog_base(base) for base in node.bases):
            self.scan.decorator_markers.add("cog")
            name = ""
            for keyword in node.keywords:
                if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                    name = str(keyword.value.value)
            self.scan.cog_names.append(name or node.name)
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "Cog":
            self.scan.decorator_markers.add("cog")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def _visit_function(self, node: Any) -> None:
        arguments = list(getattr(node.args, "posonlyargs", []) or []) + list(node.args.args or [])
        if node.name == "setup" and arguments and arguments[0].arg in _SETUP_PARAM_NAMES:
            self.scan.setup_param = arguments[0].arg
        for decorator in node.decorator_list:
            marker = _decorator_marker(decorator)
            if marker is None:
                continue
            kind, name = marker
            self.scan.decorator_markers.add(kind)
            if kind == "command" and name:
                self.scan.command_names.append(name)
            if kind == "listener" and name:
                self.scan.listener_names.append(name)
        self.generic_visit(node)


def _is_cog_base(node: ast.AST) -> bool:
    """``commands.Cog`` / ``Cog`` / ``discord.ext.commands.Cog``。"""
    if isinstance(node, ast.Attribute):
        return node.attr == "Cog"
    if isinstance(node, ast.Name):
        return node.id == "Cog"
    return False


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _dotted_name(node.func)
    return ""


def _decorator_marker(decorator: ast.AST) -> tuple[str, str] | None:
    """装饰器 → ``(标记, 名字)``；不是 discord 的命令/监听器装饰器时返回 ``None``。

    只认 ``@commands.command`` / ``@bot.command`` / ``@client.command`` /
    ``@commands.Cog.listener`` / ``@bot.listen`` / ``@bot.event`` 这类写法（前缀必须是
    命令客户端；``@filter.command`` 这种别的框架的装饰器不算），避免误判框架。
    """
    target = decorator.func if isinstance(decorator, ast.Call) else decorator
    dotted = _dotted_name(target)
    if not dotted:
        return None
    parts = dotted.split(".")
    leaf = parts[-1]
    kind = _DECORATOR_MARKERS.get(leaf)
    if kind is None:
        return None
    heads = parts[:-1]
    if not heads:
        # 裸名（``from discord.ext.commands import command`` 之后用 ``@command``）：
        # 由「是否 import 了 discord」来兜底判断，这里也算标记。
        return kind, ""
    head = heads[0]
    if head not in _DECORATOR_HEADS:
        return None
    if any(middle for middle in heads[1:] if middle not in _DECORATOR_MIDDLES):
        return None
    name = ""
    if isinstance(decorator, ast.Call):
        for keyword in decorator.keywords:
            if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                name = str(keyword.value.value)
        if not name and decorator.args and isinstance(decorator.args[0], ast.Constant):
            name = str(decorator.args[0].value)
    return kind, name


def _scan_source(source: str) -> _SourceScan:
    """扫描一段源码（两遍 AST：先导入别名，再语法信号；解析失败就退回粗匹配）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        scan = _SourceScan(parse_error=True)
        scan.imports_discord = bool(_IMPORT_RE.search(source))
        if scan.imports_discord:
            scan.decorator_markers.add("command")
        scan.token_hit = bool(_TOKEN_RE.search(source))
        return scan
    imports = _ImportCollector()
    imports.visit(tree)
    scan = _SourceScan(
        imports_discord=imports.imports_discord,
        decorator_markers=set(imports.decorator_markers),
    )
    _SignalCollector(scan).visit(tree)
    scan.token_hit = bool(_TOKEN_RE.search(source))
    return scan


def _read_source(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _source_verdict(scan: _SourceScan) -> bool:
    """一份源码是不是 discord.py 插件（detect 的判据）。

    强信号（任一成立即认定）：

    * 源码里 import 了 ``discord`` / ``discord.*``；
    * 出现 ``<X>.Cog``（基类或 ``isinstance`` 判断）；
    * 出现 ``@<X>.command`` / ``.group`` / ``.hybrid_command`` / ``.listener`` / ``.listen``
      这类命令与监听器装饰器。

    弱信号（必须同时满足）：

    * 定义了 ``def setup(bot|client|neko|self)``，**并且**
    * 源码里出现 ``discord`` / ``cog`` / ``command`` 任一 token。
    """
    if scan.imports_discord:
        return True
    if scan.decorator_markers & {"cog", "command", "listener"}:
        return True
    return scan.has_setup and scan.token_hit


def _scan_token_summary(scan: _SourceScan) -> dict[str, Any]:
    return {
        "imports_discord": scan.imports_discord,
        "markers": sorted(scan.decorator_markers),
        "cog_names": list(scan.cog_names),
        "command_names": list(scan.command_names),
        "listener_names": list(scan.listener_names),
        "setup": scan.setup_param,
        "parse_error": scan.parse_error,
    }


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #


def _sanitize_identifier(raw: str) -> str:
    return _NON_ALNUM.sub("_", str(raw or "").strip()).strip("_")


def _stable_id(value: Any, prefix: str = "neko") -> int:
    """把 N.E.K.O. 的字符串 id 映射成一个**稳定**的正整数（当 snowflake 用）。

    Discord 的 id 是 snowflake（整数），N.E.K.O. 的 ``user_id`` / ``group_id`` 是任意
    字符串。数字原样用；其余用 SHA-1 前缀映射 —— 稳定、可反复比对，但 ``created_at``
    之类由 snowflake 推导出来的字段在这类 id 上没有真实含义（docstring 里写明了）。
    """
    text = str(value or "").strip()
    if text.isdigit():
        return int(text)
    seed = f"{prefix}:{text}" if text else f"{prefix}:{prefix}"
    return int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:14], 16)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    dumper = getattr(value, "to_dict", None) or getattr(value, "model_dump", None)
    if callable(dumper):
        try:
            return _jsonable(dumper())
        except Exception:  # noqa: BLE001 — 导出失败时退回 repr
            return repr(value)
    return repr(value)


# --------------------------------------------------------------------------- #
# 入站消息 → discord 对象
# --------------------------------------------------------------------------- #


class _ObjectFactory:
    """按一次 ``CommandContext`` 合成 Discord 侧的对象（消息 / 频道 / 服务器 / 成员）。

    N.E.K.O. 只知道「这条消息」以及载荷里带上来的提及/成员/频道/角色数据，因此这里
    只合成**看得见的部分**：当前用户、当前频道、当前群（如果载荷说是群聊），以及
    payload 里出现的成员 / 频道 / 角色。查不到的不会凭空出现。
    """

    def __init__(self, shim: _Shim, plugin_id: str, *, bot_id: int, bot_name: str, guild_name: str = "") -> None:
        self._shim = shim
        self._plugin_id = plugin_id
        self._bot_id = bot_id
        self._bot_name = bot_name
        self._guild_name = guild_name

    # ---- 入口 ------------------------------------------------------------ #

    def build(self, ctx: CommandContext, bot: Any) -> Any:
        """合成一条消息（并把它登记进 ``bot`` 的可见对象表）。"""
        raw = dict(ctx.raw or {})
        author = self._user(ctx)
        members = [author, *self._members(raw)]
        roles: list[Any] = []
        channels: list[Any] = []
        guild = None
        if ctx.is_group or raw.get("group_id"):
            guild_id = _stable_id(ctx.group_id or raw.get("group_id"), "guild")
            guild_name = str(raw.get("guild_name") or raw.get("group_name") or self._guild_name or ctx.group_id or "N.E.K.O.")
            guild = self._shim.guild_cls(guild_id, name=guild_name, owner_id=author.id, plugin_id=self._plugin_id)
            roles = self._roles(raw, guild)
            channels = self._channels(raw, guild, ctx)
            bot_member = self._bot_member(guild)
            members = [member for member in members if member.id != bot_member.id]
            members.append(bot_member)
            guild._members = members
            guild._roles = roles
            guild._channels = channels
            guild.me = bot_member
            guild.owner = author
            channel = self._current_guild_channel(raw, ctx, guild, channels)
        else:
            channel = self._dm_channel(ctx, author)
            guild = None
        self._attach_permissions(author, raw)
        for member in members:
            bot._bridge_register_user(member)
        bot._bridge_register_user(author)
        if bot.user is not None:
            bot._bridge_register_user(bot.user)
        bot._bridge_register_channel(channel)
        for item in channels:
            bot._bridge_register_channel(item)
        if guild is not None:
            bot.guilds = [guild]
        message = self._message(ctx, raw, author=author, channel=channel, guild=guild, members=members)
        return message

    def empty_message(self, ctx: CommandContext, bot: Any, *, event: str = "") -> Any:
        """给事件分发用：合成一条只有文本的消息（不建服务器）。"""
        del event
        raw = dict(ctx.raw or {})
        author = self._user(ctx)
        channel = self._dm_channel(ctx, author)
        bot._bridge_register_user(author)
        bot._bridge_register_channel(channel)
        return self._message(ctx, raw, author=author, channel=channel, guild=None, members=[author])

    # ---- 具体对象 -------------------------------------------------------- #

    def _user(self, ctx: CommandContext) -> Any:
        raw = dict(ctx.raw or {})
        user_id = _stable_id(ctx.user_id or raw.get("user_id"), "user")
        user = self._shim.member_cls(
            user_id,
            name=str(ctx.user_name or raw.get("user_name") or ctx.user_id or "user"),
            guild=None,
            plugin_id=self._plugin_id,
        )
        return user

    def _bot_member(self, guild: Any) -> Any:
        return self._shim.member_cls(
            self._bot_id,
            name=self._bot_name,
            guild=guild,
            bot=True,
            plugin_id=self._plugin_id,
        )

    def _members(self, raw: dict[str, Any]) -> list[Any]:
        members: list[Any] = []
        for item in raw.get("members") or ():
            if not isinstance(item, dict):
                continue
            member = self._shim.member_cls(
                _stable_id(item.get("id") or item.get("user_id"), "user"),
                name=str(item.get("name") or item.get("nickname") or item.get("display_name") or ""),
                nick=item.get("nick"),
                bot=bool(item.get("bot", False)),
                plugin_id=self._plugin_id,
            )
            members.append(member)
        return members

    def _roles(self, raw: dict[str, Any], guild: Any) -> list[Any]:
        roles: list[Any] = []
        for index, item in enumerate(raw.get("roles") or ()):
            if isinstance(item, str):
                roles.append(
                    self._shim.role_cls(_stable_id(item, "role"), name=item, guild=guild, position=index + 1)
                )
            elif isinstance(item, dict):
                roles.append(
                    self._shim.role_cls(
                        _stable_id(item.get("id") or item.get("name"), "role"),
                        name=str(item.get("name") or ""),
                        guild=guild,
                        position=int(item.get("position") or index + 1),
                        colour=self._colour(item.get("color") or item.get("colour")),
                    )
                )
        roles.insert(0, self._shim.role_cls(1, name="@everyone", guild=guild, position=0, is_default=True))
        return roles

    def _colour(self, value: Any) -> Any:
        if value is None:
            return None
        try:
            return self._shim.colour_cls(int(str(value).lstrip("#"), 16))
        except (TypeError, ValueError):
            return None

    def _channels(self, raw: dict[str, Any], guild: Any, ctx: CommandContext) -> list[Any]:
        channels: list[Any] = []
        for item in raw.get("channels") or ():
            if isinstance(item, str):
                channels.append(self._shim.text_channel_cls(_stable_id(item, "channel"), name=item, guild=guild))
            elif isinstance(item, dict):
                channels.append(
                    self._shim.text_channel_cls(
                        _stable_id(item.get("id") or item.get("name"), "channel"),
                        name=str(item.get("name") or ""),
                        guild=guild,
                        nsfw=bool(item.get("nsfw", False)),
                    )
                )
        channels.append(self._guild_text_channel(raw, guild, ctx))
        return channels

    def _guild_text_channel(self, raw: dict[str, Any], guild: Any, ctx: CommandContext) -> Any:
        name = str(raw.get("channel_name") or ctx.channel or "neko")
        channel_id = _stable_id(raw.get("channel_id") or f"{ctx.group_id}:{name}", "channel")
        channel = self._shim.text_channel_cls(
            channel_id, name=name, guild=guild, nsfw=bool(raw.get("nsfw", False))
        )
        channel._bridge_plugin_id = self._plugin_id
        return channel

    def _current_guild_channel(self, raw: dict[str, Any], ctx: CommandContext, guild: Any, channels: list[Any]) -> Any:
        current = self._guild_text_channel(raw, guild, ctx)
        for item in channels:
            if item.id == current.id:
                return item
        return current

    def _dm_channel(self, ctx: CommandContext, author: Any) -> Any:
        raw = dict(ctx.raw or {})
        channel_id = _stable_id(raw.get("channel_id") or ctx.user_id or ctx.channel, "dm")
        channel = self._shim.dm_channel_cls(channel_id, recipient=author)
        channel._bridge_plugin_id = self._plugin_id
        return channel

    def _attach_permissions(self, author: Any, raw: dict[str, Any]) -> None:
        value = raw.get("permissions")
        if value is None:
            return
        permissions = self._permissions(value)
        if permissions is not None:
            author._bridge_permissions = permissions

    def _permissions(self, value: Any) -> Any:
        try:
            if isinstance(value, dict):
                return self._shim.permissions_cls(**{str(key): bool(item) for key, item in value.items()})
            if isinstance(value, (list, tuple, set)):
                return self._shim.permissions_cls(**{str(item): True for item in value})
            if isinstance(value, int):
                return self._shim.permissions_cls._from_value(int(value))
        except TypeError:
            return None
        return None

    def _message(
        self,
        ctx: CommandContext,
        raw: dict[str, Any],
        *,
        author: Any,
        channel: Any,
        guild: Any,
        members: list[Any],
    ) -> Any:
        mentions = self._mentions(raw, members)
        message = self._shim.message_cls(
            _stable_id(raw.get("message_id") or f"{self._plugin_id}:{ctx.text}", "message"),
            content=ctx.text,
            channel=channel,
            author=author,
            guild=guild,
            created_at=datetime.now(timezone.utc),
            attachments=self._attachments(raw),
            embeds=self._embeds(raw),
            mentions=mentions,
            mention_everyone=bool(raw.get("mention_everyone", False)),
            plugin_id=self._plugin_id,
        )
        message.raw = dict(raw)
        message.raw.setdefault("user_id", ctx.user_id)
        message.raw.setdefault("group_id", ctx.group_id)
        message.raw.setdefault("is_group", ctx.is_group)
        return message

    def _mentions(self, raw: dict[str, Any], members: list[Any]) -> list[Any]:
        wanted = {_stable_id(item, "user") for item in raw.get("mentions") or () if isinstance(item, (str, int))}
        for item in raw.get("mention_ids") or ():
            wanted.add(_stable_id(item, "user"))
        if not wanted:
            return []
        known = [member for member in members if member.id in wanted]
        for user_id in sorted(wanted - {member.id for member in known}):
            user = self._shim.user_cls(user_id, name="", plugin_id=self._plugin_id)
            known.append(user)
        return known

    def _attachments(self, raw: dict[str, Any]) -> list[Any]:
        result: list[Any] = []
        for index, item in enumerate(raw.get("attachments") or ()):
            if isinstance(item, str):
                result.append(self._shim.attachment_cls(index + 1, filename=item, url=item))
            elif isinstance(item, dict):
                result.append(
                    self._shim.attachment_cls(
                        _stable_id(item.get("id") or index + 1, "attachment"),
                        filename=str(item.get("filename") or item.get("name") or ""),
                        url=str(item.get("url") or ""),
                        size=int(item.get("size") or 0),
                        content_type=item.get("content_type"),
                    )
                )
        return result

    def _embeds(self, raw: dict[str, Any]) -> list[Any]:
        result: list[Any] = []
        for item in raw.get("embeds") or ():
            if isinstance(item, dict):
                try:
                    result.append(self._shim.embed_cls.from_dict(item))
                except Exception:  # noqa: BLE001 — 载荷里的 embed 结构不对就当没有
                    continue
        return result


# --------------------------------------------------------------------------- #
# 兼容层
# --------------------------------------------------------------------------- #


@dataclass
class _PluginState:
    """一个已加载插件的运行期状态。"""

    manifest: PluginManifest
    bot: Any = None
    module: ModuleType | None = None
    host: Any = None
    factory: _ObjectFactory | None = None
    prefixes: tuple[str, ...] = DEFAULT_PREFIXES
    spec_ids: dict[str, str] = field(default_factory=dict)
    tools: dict[str, str] = field(default_factory=dict)
    listeners: dict[str, tuple[str, ...]] = field(default_factory=dict)
    loaded: Any = None


class DiscordPyBackend:
    """discord.py cogs 兼容层。"""

    name = "discord"
    display_name = "discord.py cogs"
    framework = "discord"
    #: 兼容 shim 只用标准库，因此没有第三方依赖要求。
    requires: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        #: ``plugin_id -> 最近一次加载的状态``（按 id 查询用）
        self._states: dict[str, _PluginState] = {}
        #: 全部加载过的状态（按加载顺序；``start`` / ``stop`` 按 LoadedPlugin 身份来找）
        self._all_states: list[_PluginState] = []
        self._shim: _Shim | None = None

    # ------------------------------------------------------------------ 识别

    def detect(self, path: Path) -> PluginManifest | None:
        """判断 ``path`` 是不是 discord.py 插件；不是则返回 ``None``（绝不抛异常）。"""
        try:
            return self._detect(Path(path))
        except Exception:  # noqa: BLE001 — detect 的契约就是「便宜 + 永不抛」
            return None

    def _detect(self, path: Path) -> PluginManifest | None:
        if path.is_file():
            if path.suffix != PYTHON_SUFFIX or path.name.startswith("_"):
                return None
            scan = _scan_source(_read_source(path))
            if not _source_verdict(scan):
                return None
            return self._manifest(path, path, "file", scan)
        if not path.is_dir():
            return None
        entry = _pick_entry(path)
        if entry is None:
            return None
        scan = self._scan_tree(path, entry)
        if scan is None:
            return None
        return self._manifest(path, entry, "package", scan)

    def _scan_tree(self, path: Path, entry: Path) -> _SourceScan | None:
        """入口文件 + 有限深度的子目录里找 discord 信号（先入口，命中就返回）。"""
        scan = _scan_source(_read_source(entry))
        if _source_verdict(scan):
            return scan
        scanned = 0
        for item in _iter_plugin_python_files(path, entry):
            scanned += 1
            if scanned > MAX_SCAN_FILES:
                break
            scan = _scan_source(_read_source(item))
            if _source_verdict(scan):
                return scan
        return None

    def _manifest(self, path: Path, entry: Path, kind: str, scan: _SourceScan) -> PluginManifest:
        plugin_id = _sanitize_identifier(path.stem if kind == "file" else path.name) or "discord_plugin"
        digest = hashlib.sha1(str(entry).encode("utf-8")).hexdigest()[:8]
        name = ""
        if len(scan.cog_names) == 1:
            name = scan.cog_names[0]
        manifest = PluginManifest(
            path=path,
            plugin_id=plugin_id,
            framework=self.name,
            name=name,
            entry=str(entry),
            extra={
                "kind": kind,
                "module_name": f"{MODULE_PREFIX}{plugin_id}_{digest}",
                "entry_file": entry.name,
                "scan": _scan_token_summary(scan),
            },
        )
        return manifest

    # ------------------------------------------------------------------ 加载

    def load(self, manifest: PluginManifest, host: Any) -> LoadedPlugin:
        """导入插件模块、构造 shim Bot、调用 ``setup(bot)`` 并整理出规格。"""
        plugin_id = manifest.plugin_id
        shim = self._ensure_shim()
        runtime = shim.runtime
        state = _PluginState(manifest=manifest, host=host)
        prefixes = _prefixes(getattr(host, "settings", None))
        state.prefixes = prefixes
        bridge = _HostBridge(host, plugin_id)
        runtime.reset_plugin(plugin_id)
        runtime.bind_host_bridge(plugin_id, bridge)
        fallback = self._make_reply_fallback(bridge, plugin_id)
        runtime.bind_reply_fallback(plugin_id, fallback)

        settings = dict(getattr(host, "settings", None) or {})
        bot_id = _stable_id(settings.get("discord_bot_id") or DEFAULT_BOT_ID, "bot")
        bot_name = str(settings.get("discord_bot_name") or DEFAULT_BOT_NAME)
        bot = shim.bot_cls(
            command_prefix=list(prefixes),
            intents=shim.intents_cls.default(),
            case_insensitive=bool(settings.get("discord_case_insensitive", False)),
            owner_ids=set(),
        )
        bot.user = shim.client_user_cls(bot_id, name=bot_name, bot=True, plugin_id=plugin_id)
        bot._bridge_plugin_id = plugin_id
        bot._bridge_plugin_dir = str(manifest.path)
        bot._bridge_module_prefix = str(manifest.extra.get("module_name") or MODULE_PREFIX)
        bot._bridge_owner_ids = {
            _stable_id(item, "user")
            for item in _as_sequence(settings.get("discord_owner_ids"))
            if str(item).strip()
        }
        bot.owner_ids = set(bot._bridge_owner_ids)
        state.bot = bot
        bot._bridge_sender = fallback
        state.factory = _ObjectFactory(
            shim,
            plugin_id,
            bot_id=bot_id,
            bot_name=bot_name,
            guild_name=str(settings.get("discord_guild_name") or ""),
        )

        module_name = str(manifest.extra.get("module_name") or f"{MODULE_PREFIX}{plugin_id}")
        task_note = ""
        _reset_pending_tasks()
        with runtime.activate(plugin_id):
            module = self._import_module(manifest, module_name, host)
            self._run_setup(bot, module, host, plugin_id)
            task_note = _pending_task_note()
            self._auto_add_cogs(bot, module, host)
        state.module = module
        self._refine_manifest(manifest, module, bot)

        with self._lock:
            self._states[plugin_id] = state
            self._all_states.append(state)

        loaded = LoadedPlugin(
            manifest=manifest,
            instance=bot,
            loaded_at=time.time(),
        )
        state.loaded = loaded
        self._fill_specs(loaded, state)
        loaded.native = self._native(state, loaded, task_note)
        loaded.handle_message = self._make_message_handler(state)
        loaded.call_tool = self._make_tool_handler(state)
        loaded.call_api = self._make_api_handler(state)
        loaded.dispatch_event = self._make_event_handler(state)
        return loaded

    def _ensure_shim(self) -> _Shim:
        shim = _ensure_shim()
        self._shim = shim
        return shim

    def _make_reply_fallback(self, bridge: _HostBridge, plugin_id: str) -> Any:
        async def _fallback(text: str) -> bool:
            return await bridge.send_chat(text, plugin_id=plugin_id, metadata={"discord_out_of_run": True})

        return _fallback

    def _import_module(self, manifest: PluginManifest, module_name: str, host: Any) -> ModuleType:
        path = Path(manifest.path)
        entry = Path(manifest.entry) if manifest.entry else path
        if path.is_file():
            entry = path
        if module_name in sys.modules:
            sys.modules.pop(module_name, None)
            host.log("debug", f"重新导入 discord 插件模块 {module_name}", source=manifest.plugin_id)
        try:
            if path.is_dir():
                spec = importlib.util.spec_from_file_location(
                    module_name, entry, submodule_search_locations=[str(path)]
                )
            else:
                spec = importlib.util.spec_from_file_location(module_name, entry)
            if spec is None or spec.loader is None:
                raise PluginLoadError(manifest.plugin_id, f"cannot build an import spec for {entry}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except PluginLoadError:
            sys.modules.pop(module_name, None)
            raise
        except BaseException as exc:  # noqa: BLE001 — 导入期异常要转成结构化错误
            sys.modules.pop(module_name, None)
            raise PluginLoadError(manifest.plugin_id, f"{type(exc).__name__}: {exc}") from exc
        return module

    def _run_setup(self, bot: Any, module: ModuleType, host: Any, plugin_id: str) -> None:
        """调用插件的 ``setup(bot)``（同步 / 异步都支持）。"""
        setup = getattr(module, "setup", None)
        if setup is None:
            return
        if not callable(setup):
            raise PluginLoadError(plugin_id, f"module attribute 'setup' is not callable: {type(setup).__name__}")
        host.log("debug", "调用插件 setup()", source=plugin_id)
        try:
            result = setup(bot)
        except Exception as exc:  # noqa: BLE001 — setup 里的异常要转成结构化错误
            raise PluginLoadError(plugin_id, f"setup() raised {type(exc).__name__}: {exc}") from exc
        if not inspect.isawaitable(result):
            return
        try:
            _run_coroutine(result)
        except PluginLoadError:
            raise
        except Exception as exc:  # noqa: BLE001 — await setup(bot) 失败同样是加载失败
            raise PluginLoadError(plugin_id, f"await setup(bot) raised {type(exc).__name__}: {exc}") from exc

    def _auto_add_cogs(self, bot: Any, module: ModuleType, host: Any) -> None:
        """模块里定义了 Cog 类却没有 ``setup`` 时，兼容层代劳 ``add_cog``。"""
        if bot.cogs:
            return
        shim = self._shim
        assert shim is not None
        errors: list[str] = []
        for _name, value in sorted(vars(module).items()):
            if not inspect.isclass(value) or not issubclass(value, shim.cog_cls) or value is shim.cog_cls:
                continue
            try:
                cog = value(bot)
                bot.add_cog(cog)
            except Exception as exc:  # noqa: BLE001 — 单个 Cog 失败要如实记录并继续试下一个
                detail = f"{value.__name__}: {type(exc).__name__}: {exc}"
                errors.append(detail)
                host.log("error", f"自动挂载 Cog 失败：{detail}", source=module.__name__)
        if errors and not bot.cogs:
            raise PluginLoadError(", ".join(errors), "no cog could be instantiated")

    def _refine_manifest(self, manifest: PluginManifest, module: ModuleType, bot: Any) -> None:
        cogs = bot.cogs
        if cogs and not manifest.name:
            manifest.name = next(iter(cogs))
        doc = (getattr(module, "__doc__", "") or "").strip()
        if doc and not manifest.description:
            manifest.description = doc.splitlines()[0]
        if not manifest.name:
            manifest.name = manifest.plugin_id

    # -------------------------------------------------------------- 规格映射

    def _fill_specs(self, loaded: LoadedPlugin, state: _PluginState) -> None:
        bot = state.bot
        prefixes = state.prefixes
        seen: set[str] = set()
        spec_ids: dict[str, str] = {}
        tools: dict[str, str] = {}
        for command in _unique_commands(bot):
            spec_id = _unique_identifier(_spec_identifier(command), seen)
            qualified = command.qualified_name
            patterns = _command_patterns(command, prefixes)
            description = _command_description(command)
            loaded.commands.append(
                CommandSpec(
                    id=spec_id,
                    name=patterns[0] if patterns else qualified,
                    description=description,
                    patterns=patterns,
                    usage=_command_usage(command, prefixes),
                    aliases=tuple(command.aliases),
                    priority=0,
                    hidden=bool(getattr(command, "hidden", False)),
                )
            )
            spec_ids[qualified] = spec_id
            tool_name = _unique_identifier(_tool_name(loaded.plugin_id, spec_id), set(tools.values()))
            tools[tool_name] = spec_id
            loaded.tools.append(
                ToolSpec(
                    name=tool_name,
                    description=_tool_description(description, qualified, patterns),
                    parameters={
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": (
                                    "发给这条 discord 命令的完整消息文本"
                                    f"（形如 {_command_usage(command, prefixes) or qualified}）"
                                ),
                            },
                            "user_id": {"type": "string", "description": "可选的调用者 id（影响 user 桶的冷却与权限判断）"},
                            "user_name": {"type": "string", "description": "可选的调用者显示名"},
                            "group_id": {"type": "string", "description": "可选的群 id；填了就按服务器上下文执行"},
                        },
                        "required": ["text"],
                    },
                    origin=f"{self.name}:{loaded.plugin_id}",
                )
            )
        listeners: dict[str, tuple[str, ...]] = {}
        for name, funcs in sorted(bot._listeners.items()):
            event = f"on_{name}"
            listeners[name] = tuple(getattr(func, "__qualname__", repr(func)) for func in funcs)
            loaded.events.append(
                EventSpec(
                    event_type=event,
                    description=f"discord.py 监听器 {event}（{len(funcs)} 个处理者）",
                    blocking=False,
                    priority=0,
                )
            )
        if not loaded.commands and listeners.get("message"):
            # 只有 on_message 监听器的插件：给一个隐藏入口，让桥接层还会把消息交给它
            # （监听器自己会跑；如果它回了话，``handled`` 才为真）。
            loaded.commands.append(
                CommandSpec(
                    id="on_message",
                    name="on_message",
                    description="on_message 监听器（隐藏入口，仅供桥接层把消息送进来）",
                    patterns=(),
                    usage="",
                    hidden=True,
                )
            )
            spec_ids["on_message"] = "on_message"
        with self._lock:
            state.spec_ids = spec_ids
            state.tools = tools
            state.listeners = listeners

    def _native(self, state: _PluginState, loaded: LoadedPlugin, task_note: str) -> dict[str, Any]:
        shim = self._shim
        bot = state.bot
        cogs = []
        for name, cog in bot.cogs.items():
            cogs.append(
                {
                    "name": name,
                    "class": type(cog).__name__,
                    "commands": [
                        item.qualified_name for item in cog.get_commands() if isinstance(item, shim.command_cls)
                    ],
                    "listeners": [event for event, _func in cog.get_listeners()],
                    "loaded": bool(getattr(cog, "_bridge_loaded", False)),
                }
            )
        return {
            "framework": self.name,
            "shim": str(SHIM_ROOT),
            "shim_version": str(getattr(shim.discord, "__version__", "")),
            "plugin_id": state.manifest.plugin_id,
            "module": str(state.manifest.extra.get("module_name") or ""),
            "entry": str(state.manifest.entry or state.manifest.path),
            "kind": str(state.manifest.extra.get("kind") or ""),
            "scan": dict(state.manifest.extra.get("scan") or {}),
            "bot": {"id": getattr(bot.user, "id", 0), "name": getattr(bot.user, "name", ""), "ready": bot.is_ready()},
            "prefixes": list(state.prefixes),
            "cogs": cogs,
            "extensions": sorted(bot.extensions),
            "listeners": {f"on_{event}": list(handlers) for event, handlers in state.listeners.items()},
            "specs": {
                "commands": [item.id for item in loaded.commands],
                "tools": [item.name for item in loaded.tools],
                "events": [item.event_type for item in loaded.events],
            },
            "unsupported_capabilities": _runtime_capabilities(state.manifest.plugin_id),
            "warnings": [task_note] if task_note else [],
            "errors": [],
            "started": False,
            "lifecycle": [],
        }

    # ------------------------------------------------------------ 回调绑定

    def _make_message_handler(self, state: _PluginState) -> Any:
        shim = self._shim
        bot = state.bot
        plugin_id = state.manifest.plugin_id
        runtime = shim.runtime

        async def handle_message(ctx: CommandContext) -> CommandResult:
            result = CommandResult(plugin_id=plugin_id, framework=self.name)
            report = runtime.begin_run(plugin_id)
            try:
                message = _ensure_message(state, ctx, bot)
                with runtime.activate(plugin_id, report):
                    await bot._bridge_dispatch("message", report, message)
            except BaseException as exc:  # noqa: BLE001 — 插件异常要转成结构化错误
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                detail = f"{type(exc).__name__}: {exc}"
                report.errors.append(detail)
                _log_error(state, f"处理消息失败：{detail}")
                self._refresh_capabilities(state)
                return self._result(result, report, handled=False)
            self._refresh_capabilities(state)
            return self._result(result, report, handled=report.handled)

        return handle_message

    def _result(self, result: CommandResult, report: Any, *, handled: bool) -> CommandResult:
        result.handled = bool(handled)
        result.replies = list(report.replies)
        if report.commands:
            result.command = report.commands[0]
        elif report.listeners and report.replies:
            # 命令没命中、但监听器回话了：报出触发它的事件名（面板 / 日志更好读）。
            first = str(report.listeners[0])
            event = first.split(":", 1)[0] if ":" in first else first
            result.command = f"on_{event}"
        result.data = report.snapshot()
        if report.command_errors:
            result.error = str(report.command_errors[0].get("error") or "")
        elif report.errors:
            result.error = str(report.errors[0])
        return result

    def _refresh_capabilities(self, state: _PluginState) -> None:
        """把运行期发现的能力缺失刷进 ``LoadedPlugin.native``（面板 / 报告能看到）。"""
        loaded = state.loaded
        if loaded is None:
            return
        native = loaded.native if isinstance(loaded.native, dict) else {}
        native["unsupported_capabilities"] = _runtime_capabilities(state.manifest.plugin_id)
        loaded.native = native

    def _make_tool_handler(self, state: _PluginState) -> Any:
        plugin_id = state.manifest.plugin_id
        runtime = self._shim.runtime
        bot = state.bot

        async def call_tool(name: str, args: dict[str, Any]) -> Any:
            with self._lock:
                spec_id = state.tools.get(name)
            if spec_id is None:
                raise UnsupportedCapabilityError(
                    f"discord.tool.{name}", "this plugin does not expose such a command"
                )
            text = str(args.get("text") or args.get("message") or "").strip()
            command = None
            for item in _unique_commands(bot):
                if _spec_identifier(item) == spec_id:
                    command = item
                    break
            if not text:
                text = _command_usage(command, state.prefixes) if command is not None else spec_id
            if not text.startswith(tuple(state.prefixes)):
                text = f"{state.prefixes[0]}{text}"
            ctx = _context_from_args(args, text)
            report = runtime.begin_run(plugin_id)
            try:
                message = _ensure_message(state, ctx, bot)
                with runtime.activate(plugin_id, report):
                    await bot._bridge_dispatch("message", report, message)
            except BaseException as exc:  # noqa: BLE001 — 工具调用失败要如实返回
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                detail = f"{type(exc).__name__}: {exc}"
                report.errors.append(detail)
                _log_error(state, f"工具调用失败：{detail}")
            payload = {
                "success": bool(report.commands) and not report.errors,
                "tool": name,
                "command": spec_id,
                "plugin_id": plugin_id,
                "framework": self.name,
                "handled": report.handled,
                "replies": list(report.replies),
                "error": str(report.command_errors[0].get("error") if report.command_errors else (report.errors[0] if report.errors else "")),
            }
            payload.update({key: value for key, value in report.snapshot().items() if key not in payload})
            payload["text"] = text
            self._refresh_capabilities(state)
            return payload

        return call_tool

    def _make_api_handler(self, state: _PluginState) -> Any:
        async def call_api(name: str, args: dict[str, Any]) -> Any:
            del args
            raise UnsupportedCapabilityError(
                f"discord.api.{name}",
                "discord.py 插件没有对外 API 声明（上游只有命令 / 监听器 / 扩展）",
            )

        return call_api

    def _make_event_handler(self, state: _PluginState) -> Any:
        shim = self._shim
        bot = state.bot
        plugin_id = state.manifest.plugin_id
        runtime = shim.runtime

        async def dispatch_event(event_type: str, payload: dict[str, Any]) -> Any:
            name = _resolve_event(event_type)
            if name is None:
                raise UnsupportedCapabilityError(
                    f"discord.event.{event_type}",
                    "只能分发 message / ready / member_* / reaction_* / guild_* 事件；"
                    "message.received 由 handle_message 承载（避免同一监听器跑两次）",
                )
            body = dict(payload or {})
            report = runtime.begin_run(plugin_id)
            try:
                args = _event_args(state, name, body)
                with runtime.activate(plugin_id, report):
                    await bot._bridge_dispatch(name, report, *args)
            except BaseException as exc:  # noqa: BLE001 — 事件失败要结构化返回
                if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                    raise
                detail = f"{type(exc).__name__}: {exc}"
                report.errors.append(detail)
            return {
                "success": not report.errors,
                "handled": bool(report.listeners),
                "plugin_id": plugin_id,
                "framework": self.name,
                "event": f"on_{name}",
                "listeners": list(report.listeners),
                "replies": list(report.replies),
                "error": report.errors[0] if report.errors else "",
            }

        return dispatch_event

    # ------------------------------------------------------------ 生命周期

    async def start(self, plugin: LoadedPlugin) -> None:
        """绑定事件循环、await ``setup_hook``、跑 ``cog_load`` 并分发 ``ready``。

        容忍半初始化：单个钩子失败只记录到 ``native``，不影响 N.E.K.O. 启动。
        """
        state = self._state_for_plugin(plugin)
        runtime = self._shim.runtime if self._shim is not None else None
        loop = asyncio.get_running_loop()
        if runtime is not None:
            runtime.bind_loop(loop)
        errors: list[str] = []
        bot = state.bot
        if runtime is not None:
            with runtime.activate(plugin.plugin_id):
                errors.extend(await self._run_setup_hook(bot, state))
                for cog in bot._bridge_cogs():
                    try:
                        await bot._load_cog(cog)
                    except Exception as exc:  # noqa: BLE001 — 单个 Cog 的 cog_load 失败不阻断其它
                        detail = f"cog_load {type(cog).__name__}: {type(exc).__name__}: {exc}"
                        errors.append(detail)
                        _log_error(state, detail)
                bot._bridge_mark_ready()
                try:
                    # 上游 2.x 的 ``on_ready()`` 不接受参数，这里保持一致。
                    await bot._bridge_dispatch("ready", None)
                except Exception as exc:  # noqa: BLE001 — ready 监听器失败只记录
                    detail = f"ready 监听器出错：{type(exc).__name__}: {exc}"
                    errors.append(detail)
                    _log_error(state, detail)
        else:  # pragma: no cover — 正常路径一定会先 load
            bot._bridge_mark_ready()
        # ``cog_load`` / ``setup_hook`` / ``load_extension`` 里可能又挂上了 Cog 或扩展：
        # 重新整理一次规格，让它们的命令 / 监听器也进 N.E.K.O.（``register_surfaces`` 在
        # 注册表 ``start`` 之后才跑，所以这里刷新是有效的）。
        self._refill_specs(plugin, state)
        self._patch_native(plugin, {"started": True, "errors": errors, "lifecycle": ["started"]})
        plugin.started = True

    def _refill_specs(self, plugin: LoadedPlugin, state: _PluginState) -> None:
        """按当前的命令 / 监听器注册表重建规格（幂等：先清空再填）。"""
        plugin.commands = []
        plugin.tools = []
        plugin.events = []
        self._fill_specs(plugin, state)
        native = plugin.native if isinstance(plugin.native, dict) else {}
        native["specs"] = {
            "commands": [item.id for item in plugin.commands],
            "tools": [item.name for item in plugin.tools],
            "events": [item.event_type for item in plugin.events],
        }
        native["extensions"] = sorted(state.bot.extensions)
        native["listeners"] = {f"on_{event}": list(handlers) for event, handlers in state.listeners.items()}
        plugin.native = native

    async def _run_setup_hook(self, bot: Any, state: _PluginState) -> list[str]:
        """``setup_hook``：插件覆盖了才跑（上游在登录前调用它）。"""
        hook = getattr(type(bot), "setup_hook", None)
        if hook is None or hook is getattr(self._shim.discord.Client, "setup_hook", None):
            return []
        try:
            result = bot.setup_hook()
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 — setup_hook 失败只记录
            detail = f"setup_hook: {type(exc).__name__}: {exc}"
            _log_error(state, detail)
            return [detail]
        return []

    async def stop(self, plugin: LoadedPlugin) -> None:
        """跑 ``cog_unload``（逆序）并标记未就绪；必须容忍插件半启动。"""
        state = self._state_for_plugin(plugin)
        errors: list[str] = []
        if state is not None and state.bot is not None:
            bot = state.bot
            runtime = self._shim.runtime if self._shim is not None else None
            report = runtime.begin_run(plugin.plugin_id) if runtime is not None else None
            for cog in reversed(bot._bridge_cogs()):
                try:
                    if runtime is not None and report is not None:
                        with runtime.activate(plugin.plugin_id, report):
                            await bot._unload_cog(cog)
                    else:  # pragma: no cover — 与上面等价，只是没有运行时
                        await bot._unload_cog(cog)
                except Exception as exc:  # noqa: BLE001 — 卸载失败只记录，继续卸其它 Cog
                    detail = f"cog_unload {type(cog).__name__}: {type(exc).__name__}: {exc}"
                    errors.append(detail)
                    _log_error(state, detail)
            bot._bridge_reset_ready()
        self._patch_native(plugin, {"started": False, "errors": errors, "lifecycle": ["stopped"]})
        plugin.started = False

    def _state(self, plugin_id: str) -> _PluginState:
        with self._lock:
            state = self._states.get(plugin_id)
        if state is None:
            raise PluginLoadError(plugin_id, "plugin state is missing (was it loaded by this backend?)")
        return state

    def _state_for_plugin(self, plugin: LoadedPlugin) -> _PluginState:
        """按 ``LoadedPlugin`` **身份**找状态。

        同一个 ``plugin_id`` 被加载两次时（测试 / 手动重载），``_states`` 里只剩最后一次；
        ``start`` / ``stop`` 必须操作调用方手上那个实例，所以这里按身份匹配。
        """
        with self._lock:
            for state in self._all_states:
                if state.loaded is plugin:
                    return state
        return self._state(plugin.plugin_id)

    def _patch_native(self, plugin: LoadedPlugin, patch: dict[str, Any]) -> None:
        native = plugin.native if isinstance(plugin.native, dict) else {}
        errors = patch.pop("errors", None)
        lifecycle = patch.pop("lifecycle", None)
        native.update(patch)
        if isinstance(errors, list) and errors:
            bucket = native.setdefault("errors", [])
            if isinstance(bucket, list):
                bucket.extend(str(item) for item in errors)
        if isinstance(lifecycle, list):
            bucket = native.setdefault("lifecycle", [])
            if isinstance(bucket, list):
                bucket.extend(str(item) for item in lifecycle)
        plugin.native = native

    # ------------------------------------------------------------------ 杂项

    def state_for(self, plugin_id: str) -> _PluginState | None:
        """该插件的运行期状态（测试 / 面板用）。"""
        with self._lock:
            return self._states.get(plugin_id)


BACKEND = DiscordPyBackend()
"""桥接注册表读取的模块级单例。"""


# --------------------------------------------------------------------------- #
# 辅助函数
# --------------------------------------------------------------------------- #


def _pending_task_note() -> str:
    """加载期在临时事件循环里留下的后台任务：如实告诉用户它们不会被承载。"""
    count = _PENDING_TASKS
    if not count:
        return ""
    return (
        f"setup() 期间创建了 {count} 个后台任务：N.E.K.O. 不承载它们（临时事件循环已关闭）；"
        "请把后台循环放进 cog_load"
    )


#: 上次 ``_run_coroutine`` 关掉临时循环时留下的未完成任务数（模块级，供日志提示）。
_PENDING_TASKS = 0


def _reset_pending_tasks() -> None:
    """清掉上一次加载遗留的「后台任务」计数（每次 load 重新统计）。"""
    global _PENDING_TASKS
    _PENDING_TASKS = 0


def _run_coroutine(coro: Any) -> Any:
    """在一个**独占**事件循环里跑协程（``load`` 是同步接口，但 ``setup`` 常是协程）。

    * 当前线程没有运行中的循环 → 直接新建循环跑；
    * 当前线程**正在**跑循环（例如有人从 N.E.K.O. 主循环里同步调 ``load``）→
      放到一个工作线程里跑，避免 ``RuntimeError: This event loop is already running``。

    临时循环关闭前会取消未完成的任务，并把数量记到 ``_PENDING_TASKS``：`setup` 里
    ``create_task`` 出来的后台任务不会被 N.E.K.O. 承载（日志里会说明）。
    """
    global _PENDING_TASKS

    def _execute() -> Any:
        global _PENDING_TASKS
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result = loop.run_until_complete(coro)
            pending = [task for task in asyncio.all_tasks(loop) if not task.done()]
            if pending:
                _PENDING_TASKS += len(pending)
                for task in pending:
                    task.cancel()
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            return result
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _execute()
    outcome: dict[str, Any] = {}

    def _worker() -> None:
        try:
            outcome["result"] = _execute()
        except BaseException as exc:  # noqa: BLE001 — 线程里的异常要带回主线程
            outcome["error"] = exc

    thread = threading.Thread(target=_worker, name="neko-discord-setup", daemon=True)
    thread.start()
    thread.join(_SETUP_TIMEOUT)
    if thread.is_alive():
        raise PluginLoadError("discord", "await setup(bot) 超时（120s）")
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def _pick_entry(path: Path) -> Path | None:
    """挑出插件包的入口文件。

    上游扩展是模块（单文件）或包（有 ``__init__.py``），因此目录形态只认
    ``__init__.py``：散落在目录里的 ``*.py`` 由调用方按文件路径逐个探测。
    这样 ``data/plugins/discord/`` 这类分类目录也不会被误判成插件。
    """
    init_file = path / "__init__.py"
    return init_file if init_file.is_file() else None


def _iter_plugin_python_files(path: Path, entry: Path, *, max_depth: int = MAX_SCAN_DEPTH) -> Any:
    """广度有限地列出插件目录里的 Python 文件（跳过非插件目录与私有模块）。"""
    pending: list[tuple[Path, int]] = [(path, 0)]
    while pending:
        current, depth = pending.pop(0)
        try:
            children = sorted(current.iterdir())
        except OSError:
            continue
        for item in children:
            if item.is_dir():
                if depth + 1 <= max_depth and not _skip_dir(item):
                    pending.append((item, depth + 1))
                continue
            if item.suffix != PYTHON_SUFFIX or item == entry:
                continue
            if item.name.startswith("_") and item.name != "__init__.py":
                continue
            yield item


def _skip_dir(path: Path) -> bool:
    name = path.name
    return name.startswith(".") or name.startswith("_") or name.lower() in SKIP_DIR_NAMES


def _prefixes(settings: Any) -> tuple[str, ...]:
    """从桥接配置里取前缀（``discord_command_prefix``，字符串或列表）。"""
    raw = None
    if isinstance(settings, dict):
        raw = settings.get("discord_command_prefix")
    elif settings is not None:
        try:
            raw = settings.get("discord_command_prefix")
        except Exception:  # noqa: BLE001 — 配置对象行为不可预期时退回默认
            raw = None
    values: list[str] = []
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, (list, tuple, set)):
        values = [str(item) for item in raw]
    cleaned = tuple(dict.fromkeys(item for item in values if item))
    return cleaned or DEFAULT_PREFIXES


def _as_sequence(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _normalise_event(event_type: str) -> str:
    """事件名归一化：去掉 ``on`` 前缀 / ``event`` 后缀、忽略大小写与非字母字符。

    ``on_member_join`` / ``member_join`` / ``MEMBER_JOIN`` / ``memberJoin`` 都归一到
    ``memberjoin``，用它去查 :data:`_EVENT_LOOKUP`。
    """
    text = "".join(ch for ch in str(event_type or "").lower() if ch.isalnum())
    if text.startswith("on"):
        text = text[2:]
    if text.endswith("event"):
        text = text[: -len("event")]
    return text


#: 归一化后的事件名 -> 规范名（``dispatch_event`` 用它把各种写法收敛到一处）。
_EVENT_LOOKUP: dict[str, str] = {_normalise_event(item): item for item in _SUPPORTED_EVENTS}
_EVENT_LOOKUP[_normalise_event("on_message")] = "message"
_EVENT_LOOKUP[_normalise_event("ready")] = "ready"


def _resolve_event(event_type: str) -> str | None:
    """把 ``dispatch_event`` 的入参解析成规范事件名；不支持时返回 ``None``。"""
    return _EVENT_LOOKUP.get(_normalise_event(event_type))


def _unique_commands(bot: Any) -> list[Any]:
    """按注册顺序返回去重后的命令（组会展开成自己 + 全部子命令，不含别名重复项）。"""
    seen: set[int] = set()
    result: list[Any] = []
    for command in bot._commands.values():
        if isinstance(command, self_group_cls()) and command.parent is None:
            # 组：``walk_commands`` 会先产出组自己，再产出子命令。
            for nested in command.walk_commands():
                key = id(nested)
                if key in seen:
                    continue
                seen.add(key)
                result.append(nested)
            continue
        key = id(command)
        if key in seen:
            continue
        seen.add(key)
        result.append(command)
    return result


def self_group_cls() -> Any:
    """``Group`` 类（延迟取，避免模块级 import shim）。"""
    return importlib.import_module("discord.ext.commands").Group


def _spec_identifier(command: Any) -> str:
    """命令 → 稳定入口 id（``admin ping`` → ``admin_ping``）。"""
    base = _sanitize_identifier(str(getattr(command, "qualified_name", "") or getattr(command, "name", "")))
    return (base or "command")[:64]


def _unique_identifier(candidate: str, seen: set[str]) -> str:
    identifier = candidate
    suffix = 2
    while identifier in seen:
        identifier = f"{candidate}_{suffix}"
        suffix += 1
    seen.add(identifier)
    return identifier


def _command_patterns(command: Any, prefixes: tuple[str, ...]) -> tuple[str, ...]:
    """命令的触发形式：每个前缀 + 限定名，以及每个别名。"""
    names = [str(command.qualified_name), *[f"{command.full_parent_name} {alias}".strip() for alias in command.aliases]]
    patterns: list[str] = []
    for prefix in prefixes:
        for name in names:
            item = f"{prefix}{name}"
            if item not in patterns:
                patterns.append(item)
    return tuple(patterns)


def _command_usage(command: Any, prefixes: tuple[str, ...]) -> str:
    prefix = prefixes[0] if prefixes else "!"
    signature = str(getattr(command, "signature", "") or "")
    return f"{prefix}{command.qualified_name} {signature}".strip()


def _command_description(command: Any) -> str:
    help_text = str(getattr(command, "help", "") or "").strip()
    if help_text:
        return help_text.splitlines()[0]
    brief = str(getattr(command, "brief", "") or "").strip()
    return brief


def _tool_name(plugin_id: str, spec_id: str) -> str:
    cleaned = _sanitize_identifier(f"{plugin_id}_{spec_id}")
    return cleaned[:64] or "discord_tool"


def _tool_description(description: str, qualified: str, patterns: tuple[str, ...]) -> str:
    parts: list[str] = []
    if description:
        parts.append(description)
    parts.append(f"discord.py 命令 {qualified}")
    if patterns:
        parts.append("触发：" + "、".join(patterns))
    return "；".join(parts)[:512]


def _runtime_capabilities(plugin_id: str) -> list[dict[str, Any]]:
    shim = _shim_cache
    if shim is None:
        return []
    try:
        return list(shim.runtime.plugin_capabilities(plugin_id))
    except Exception:  # noqa: BLE001 — 能力清单拿不到不影响插件本身
        return []


def _log_error(state: _PluginState, message: str) -> None:
    host = state.host
    if host is None:
        return
    try:
        host.log("error", message, source=f"discord:{state.manifest.plugin_id}")
    except Exception:  # noqa: BLE001 — 日志通道故障不能影响插件逻辑
        pass


def _context_from_args(args: dict[str, Any], text: str) -> CommandContext:
    extra = {
        str(key): value
        for key, value in args.items()
        if key not in {"text", "message", "user_id", "user_name", "group_id", "channel", "raw"}
    }
    raw = args.get("raw")
    body = dict(raw) if isinstance(raw, dict) else {}
    body.setdefault("tool_call", True)
    body.update(extra)
    group_id = str(args.get("group_id") or "")
    return CommandContext(
        text=text,
        user_id=str(args.get("user_id") or "discord-tool"),
        user_name=str(args.get("user_name") or ""),
        group_id=group_id,
        channel=str(args.get("channel") or "neko"),
        source="bot_bridge",
        is_group=bool(group_id),
        raw=body,
    )


def _ensure_message(state: _PluginState, ctx: CommandContext, bot: Any) -> Any:
    """按上下文合成（或复用）一条消息。

    ``CommandContext.raw`` 里已经带了合成消息（例如事件分发自己造的）就直接用，
    避免同一轮里对象不一致。
    """
    existing = (ctx.raw or {}).get("discord_message")
    if existing is not None:
        return existing
    factory = state.factory
    assert factory is not None
    message = factory.build(ctx, bot)
    if isinstance(ctx.raw, dict):
        ctx.raw["discord_message"] = message
    return message


def _event_args(state: _PluginState, name: str, payload: dict[str, Any]) -> tuple[Any, ...]:
    """事件分发时合成的对象（按上游各事件的回调签名给出参数）。

    N.E.K.O. 目前只会分发 ``message.received``（由 ``handle_message`` 承载，不走这里），
    因此这里的其它事件主要供脚本 / 面板手动触发：能造出上游同名的对象，字段只填载荷里
    给的那部分，缺的留空。
    """
    shim = _shim_cache
    assert shim is not None
    factory = state.factory
    assert factory is not None
    ctx = _context_from_args(payload, str(payload.get("text") or payload.get("content") or ""))
    bot = state.bot
    if name == "message":
        return (factory.empty_message(ctx, bot, event=name),)
    if name in ("ready", "connect", "disconnect"):
        return ()
    if name in ("guild_join", "guild_remove"):
        guild = factory.build(ctx, bot).guild if (payload.get("group_id") or payload.get("guild_id")) else None
        if guild is None:
            message = factory.empty_message(ctx, bot, event=name)
            guild = message.guild
        return (guild,) if guild is not None else ()
    if name in ("member_join", "member_remove", "member_update"):
        author = factory.build(ctx, bot).author
        return (author,)
    if name in ("reaction_add", "reaction_remove"):
        message = factory.empty_message(ctx, bot, event=name)
        emoji = shim.partial_emoji_cls(name=str(payload.get("emoji") or ""))
        reaction = shim.discord.Reaction(emoji=emoji, count=int(payload.get("count") or 1))
        return (reaction, message.author)
    return (factory.empty_message(ctx, bot, event=name),)
