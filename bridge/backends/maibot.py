"""MaiBot 兼容层。

把 MaiBot 生态的插件（新版 ``maibot_sdk`` 2.x 与旧版 ``src.plugin_system``）
加载进 N.E.K.O.：它们的命令 / 工具 / 事件 / API 变成桥接层的统一描述，
它们的 ``self.ctx`` 能力调用回落到 :class:`~bridge.contracts.HostServices`。

加载路径与官方 Runner 一致（依据 ``maibot-sdk-full`` + ``botfw/maibot`` 的
``src/plugin_runtime/runner`` 源码）：

1. 确认插件目录里有入口 ``plugin.py``（``PluginLoader._discover_single_candidate``）。
2. 安装 ``maibot_sdk.compat._import_hook``（``PluginLoader._ensure_compat_hook``）。
3. ``importlib.util.spec_from_file_location`` 导入模块，并把插件目录的父目录
   临时放进 ``sys.path``（``PluginLoader._load_single_plugin``）。
4. 优先调用模块级 ``create_plugin()``，否则扫描 ``BasePlugin`` 子类并用
   ``maibot_sdk.compat.legacy_adapter.LegacyPluginAdapter`` 包装。
5. ``PluginContext(plugin_id, rpc_call=..., paths=PluginPaths(data_dir, runtime_dir))``
   通过 ``instance._set_context(ctx)`` 注入。
6. ``plugin.set_plugin_config(...)`` 推送配置；``plugin.get_components()`` 收集组件声明。
7. 组件调用 = ``getattr(instance, metadata["handler_name"])(**kwargs)``；插件类本身没有
   Host 侧派发器，所以兼容层必须自己解析处理函数。
8. 生命周期 = ``await plugin.on_load()`` / ``await plugin.on_unload()``。

能力映射表（``cap.call`` 的 capability -> N.E.K.O. 侧实现）：

============================  ==========================================
send.text                     ``HostServices.send_chat``
send.hybrid / send.forward    文本段拼接后 ``send_chat``（含非文本段时带 degraded 标记）
send.custom（文本载荷）        ``send_chat``
llm.generate / _with_tools    ``HostServices.llm_generate``
llm.embed                     ``HostServices.llm_embed``
database.*                    每插件独立的 sqlite3 存储（``HostServices.data_dir``）
config.*                      ``HostServices.plugin_config``
api.call / api.get / api.list 本模块维护的已加载插件 API 索引 -> ``HostServices.call_plugin_entry``
component.*（查询类）          ``HostServices.list_plugins``
component.enable / disable    兼容层自己的组件启用位（真实作用于派发）
message.* / chat.*            ``HostServices.recent_messages``（聊天流由消息快照推导）
tool.get_definitions          本模块收集的 MaiBot 工具声明
host.route_message            结构化结果：不接受（N.E.K.O. 侧没有入站注入面）
host.update_message_gateway_state  记录到插件 native 状态并接受
============================  ==========================================

没有等价实现的能力（``emoji.*`` / ``render.*`` / ``statistics.*`` / ``maisaka.*`` /
``knowledge.*`` / ``frequency.*`` / ``person.*`` / ``gateway.*`` / ``send.image`` /
``send.emoji`` / ``send.command`` / ``llm.transcribe_audio`` / ``llm.get_available_models`` /
``chat.open_session`` / 插件装卸载类 ``component.*``）一律返回结构化失败
``{"success": False, "code": "unsupported_capability", "error": ...}`` 并记 warning，
绝不假装成功。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import importlib.util
import inspect
import json
import logging
import re
import sqlite3
import sys
import tempfile
import threading
import time
import tomllib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts import (
    ApiSpec,
    CommandContext,
    CommandResult,
    CommandSpec,
    EventSpec,
    HostServices,
    LoadedPlugin,
    PluginManifest,
    ToolSpec,
)
from ..errors import BackendUnavailableError, BridgeError, PluginLoadError, UnsupportedCapabilityError

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

BACKEND_NAME = "maibot"
DISPLAY_NAME = "MaiBot"
REQUIRES: tuple[str, ...] = ("pydantic",)

MANIFEST_FILE = "_manifest.json"
ENTRY_FILE = "plugin.py"
CONFIG_FILE = "config.toml"

PLUGIN_DIR = Path(__file__).resolve().parents[2]  # plugins/bot_bridge
VENDOR_ROOT = PLUGIN_DIR / "vendor"

#: 入口文件里出现任一标记，就认为它是 MaiBot 生态的插件源码。
_MAIBOT_SOURCE_MARKERS: tuple[str, ...] = (
    "maibot_sdk",
    "src.plugin_system",
    "register_plugin",
    "MaiBotPlugin",
    "BasePlugin",
    "BaseCommand",
    "BaseAction",
    "BaseTool",
    "BaseEventHandler",
)

#: 允许插件直接调用的 Host 原始 RPC 方法（与 SDK ``_ALLOWED_RAW_HOST_METHODS`` 对齐）。
_ALLOWED_RAW_HOST_METHODS: frozenset[str] = frozenset(
    {"cap.call", "host.route_message", "host.update_message_gateway_state"}
)

#: 事件别名：桥接层入站分发使用 ``message.received``，MaiBot 侧对应 ``on_message``。
_MESSAGE_EVENT_ALIASES: dict[str, tuple[str, ...]] = {
    "message.received": ("message.received", "on_message"),
}

_HOOK_PREFIX = "hook:"

#: 明确没有 N.E.K.O. 等价实现的能力（后缀匹配 ``.<name>`` 或完整匹配）。
_UNSUPPORTED_CAPABILITIES: frozenset[str] = frozenset(
    {
        "send.image",
        "send.emoji",
        "send.command",
        "llm.transcribe_audio",
        "llm.get_available_models",
        "chat.open_session",
        "emoji.get_random",
        "emoji.get_by_description",
        "emoji.get_count",
        "emoji.get_info",
        "emoji.get_emotions",
        "emoji.get_all",
        "emoji.register",
        "emoji.delete",
        "render.html2png",
        "statistics.local.models",
        "statistics.local.model_trend",
        "statistics.local.token_trend",
        "statistics.local.token_distribution",
        "statistics.local.message_trend",
        "statistics.local.tool_trend",
        "statistics.local.online_time_trend",
        "maisaka.context.append",
        "maisaka.proactive.trigger",
        "knowledge.search",
        "frequency.get_current_talk_value",
        "frequency.set_adjust",
        "frequency.get_adjust",
        "person.get_id",
        "person.get_value",
        "person.get_id_by_name",
        "component.get_plugin_config_schema",
        "component.load_plugin",
        "component.unload_plugin",
        "component.reload_plugin",
    }
)

#: 前缀命中即视为不支持的能力族（``gateway.*`` 走原始 RPC，这里兜住误用 ``cap.call`` 的情况）。
_UNSUPPORTED_CAPABILITY_PREFIXES: tuple[str, ...] = ("gateway.", "render.", "statistics.", "maisaka.", "emoji.")

#: Hook 顺序槽位 -> 事件优先级（越大越先执行，与 MaiBot ``weight`` 语义一致）。
_HOOK_ORDER_PRIORITY: dict[str, int] = {"early": 300, "normal": 200, "late": 100}

_UNSUPPORTED_ERROR_TEMPLATE = "{capability} is not mapped to N.E.K.O."

logger = logging.getLogger("bot_bridge.backends.maibot")


class MaiBotCallError(BridgeError):
    """MaiBot 插件组件调用失败。"""

    code = "maibot_call_failed"

    def __init__(self, target: str, detail: str = "") -> None:
        super().__init__(f"maibot component '{target}' failed: {detail or 'unknown reason'}")
        self.target = target
        self.detail = detail


# --------------------------------------------------------------------------- #
# SDK 装载（用户安装优先，缺失时回落到 vendor）
# --------------------------------------------------------------------------- #

_SDK_LOCK = threading.RLock()
_SDK_BUNDLE: "_SdkBundle | None" = None


@dataclass(slots=True)
class _SdkBundle:
    """已解析的 ``maibot_sdk`` 模块集合。"""

    source: str
    path: str
    version: str
    module: Any
    context_module: Any
    plugin_module: Any
    components_module: Any
    types_module: Any
    config_module: Any
    compat_module: Any
    import_hook_module: Any | None
    legacy_adapter_module: Any | None
    plugin_class: type | None

    def has_compat(self) -> bool:
        """旧版兼容层是否可用（``maibot_sdk.compat``）。"""
        return self.import_hook_module is not None and self.legacy_adapter_module is not None


def _ensure_vendor_sys_path() -> bool:
    """把 ``vendor`` 放进 ``sys.path``，返回 vendor 里是否真的有 SDK。"""
    if not (VENDOR_ROOT / "maibot_sdk" / "__init__.py").is_file():
        return False
    entry = str(VENDOR_ROOT)
    if entry not in sys.path:
        sys.path.insert(0, entry)
    return True


def load_sdk_bundle() -> _SdkBundle:
    """解析 ``maibot_sdk``。

    只在用户环境里 ``import maibot_sdk`` 失败时，才把本插件自带的
    ``vendor`` 目录加进 ``sys.path``（幂等 + 线程安全）。

    Raises:
        BackendUnavailableError: 既没有用户安装的 SDK，vendor 副本也不可用。
    """
    global _SDK_BUNDLE
    with _SDK_LOCK:
        if _SDK_BUNDLE is not None:
            return _SDK_BUNDLE

        try:
            importlib.import_module("pydantic")
        except ImportError as exc:  # pragma: no cover - 运行环境缺依赖
            raise BackendUnavailableError(BACKEND_NAME, f"缺少 pydantic：{exc}") from exc

        source = "user"
        user_error: BaseException | None = None
        try:
            importlib.import_module("maibot_sdk")
        except ImportError as exc:
            user_error = exc
            if not _ensure_vendor_sys_path():
                raise BackendUnavailableError(
                    BACKEND_NAME,
                    f"未安装 maibot_sdk（{exc}），且 vendor 副本不存在：{VENDOR_ROOT / 'maibot_sdk'}",
                ) from exc
            source = "vendored"
            try:
                importlib.import_module("maibot_sdk")
            except ImportError as vendor_exc:
                raise BackendUnavailableError(
                    BACKEND_NAME,
                    f"用户安装的 maibot_sdk 不可用（{user_error}），vendor 副本也不可用（{vendor_exc}）",
                ) from vendor_exc

        module = importlib.import_module("maibot_sdk")
        context_module = importlib.import_module("maibot_sdk.context")
        plugin_module = importlib.import_module("maibot_sdk.plugin")
        components_module = importlib.import_module("maibot_sdk.components")
        types_module = importlib.import_module("maibot_sdk.types")
        config_module = importlib.import_module("maibot_sdk.config")

        compat_module: Any | None = None
        import_hook_module: Any | None = None
        legacy_adapter_module: Any | None = None
        try:
            compat_module = importlib.import_module("maibot_sdk.compat")
            import_hook_module = importlib.import_module("maibot_sdk.compat._import_hook")
            legacy_adapter_module = importlib.import_module("maibot_sdk.compat.legacy_adapter")
        except ImportError as exc:
            # 兼容层缺失不影响新版插件，只记录。
            logger.warning("maibot_sdk 兼容层不可用，旧版插件将无法加载：%s", exc)

        plugin_class = getattr(plugin_module, "MaiBotPlugin", None)
        bundle = _SdkBundle(
            source=source,
            path=str(getattr(module, "__file__", "") or ""),
            version=str(getattr(module, "__version__", "") or ""),
            module=module,
            context_module=context_module,
            plugin_module=plugin_module,
            components_module=components_module,
            types_module=types_module,
            config_module=config_module,
            compat_module=compat_module,
            import_hook_module=import_hook_module,
            legacy_adapter_module=legacy_adapter_module,
            plugin_class=plugin_class if isinstance(plugin_class, type) else None,
        )
        _SDK_BUNDLE = bundle
        logger.info("maibot_sdk %s 就绪（来源：%s，路径：%s）", bundle.version, bundle.source, bundle.path)
        return bundle


_HOOK_LOCK = threading.RLock()
_HOOK_INSTALLED = False


def ensure_legacy_import_hook(bundle: _SdkBundle) -> bool:
    """安装 ``src.plugin_system`` 导入钩子（幂等 + 线程安全）。

    与官方 ``PluginLoader._ensure_compat_hook`` 一样，在导入 **任何** 插件之前
    安装：旧版插件需要它才能 ``from src.plugin_system import ...``。

    Returns:
        bool: 钩子是否可用（兼容层缺失时为 ``False``）。
    """
    global _HOOK_INSTALLED
    if bundle.import_hook_module is None:
        return False
    with _HOOK_LOCK:
        if _HOOK_INSTALLED:
            return True
        install = getattr(bundle.import_hook_module, "install_hook", None)
        if not callable(install):
            logger.warning("maibot_sdk 兼容层没有 install_hook()，旧版插件导入可能失败")
            return False
        install()
        _HOOK_INSTALLED = True
        logger.debug("已安装 maibot 旧版插件导入钩子")
        return True


# --------------------------------------------------------------------------- #
# 组件条目
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _ComponentEntry:
    """兼容层内部的一条组件记录。"""

    name: str
    kind: str
    handler_name: str
    metadata: dict[str, Any] = field(default_factory=dict)
    dynamic: bool = False
    enabled: bool = True
    spec: CommandSpec | ToolSpec | EventSpec | ApiSpec | None = None

    @property
    def api_version(self) -> str:
        """API 组件的版本号（非 API 组件为空串）。"""
        return str(self.metadata.get("version") or "1") if self.kind == "API" else ""


# --------------------------------------------------------------------------- #
# 每插件 sqlite 存储（database.* 能力）
# --------------------------------------------------------------------------- #

_FILTER_OPERATORS: dict[str, str] = {
    "__gt": "gt",
    "__gte": "gte",
    "__lt": "lt",
    "__lte": "lte",
    "__ne": "ne",
    "__in": "in",
    "__contains": "contains",
}


class _SqliteStore:
    """按插件隔离的 sqlite3 文档存储。

    表名 = ``model_name``（只允许标识符字符），一行一条 JSON 文档，
    过滤在 Python 侧做（MaiBot 的 filters 是字段 -> 值的字典）。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(str(self._path), check_same_thread=False)
            connection.row_factory = sqlite3.Row
            self._connection = connection
        return connection

    @staticmethod
    def _table_name(model_name: object) -> str:
        name = str(model_name or "").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", name):
            raise ValueError(f"非法的 model_name：{model_name!r}（只允许字母/数字/下划线）")
        return name

    def _ensure_table(self, connection: sqlite3.Connection, table: str) -> None:
        connection.execute(
            f'CREATE TABLE IF NOT EXISTS "{table}" ('
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "doc_key TEXT, "
            "doc TEXT NOT NULL, "
            "created_at REAL NOT NULL, "
            "updated_at REAL NOT NULL)"
        )
        connection.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS "{table}_key" ON "{table}" (doc_key)')
        connection.commit()

    # -------------------------------------------------------------- 读写

    def rows(self, model_name: object) -> list[dict[str, Any]]:
        """读取某张表的全部文档。"""
        table = self._table_name(model_name)
        with self._lock:
            connection = self._connect()
            self._ensure_table(connection, table)
            cursor = connection.execute(f'SELECT seq, doc, created_at, updated_at FROM "{table}" ORDER BY seq ASC')
            result: list[dict[str, Any]] = []
            for row in cursor.fetchall():
                document = self._decode(row["doc"])
                document.setdefault("_seq", row["seq"])
                document.setdefault("_created_at", row["created_at"])
                document.setdefault("_updated_at", row["updated_at"])
                result.append(document)
            return result

    def insert(self, model_name: object, data: Mapping[str, Any], *, key_field: str = "", key_value: Any = None) -> dict[str, Any]:
        """插入一条文档。"""
        table = self._table_name(model_name)
        document = dict(data)
        doc_key = self._document_key(document, key_field, key_value)
        now = time.time()
        with self._lock:
            connection = self._connect()
            self._ensure_table(connection, table)
            cursor = connection.execute(
                f'INSERT INTO "{table}" (doc_key, doc, created_at, updated_at) VALUES (?, ?, ?, ?)',
                (doc_key, json.dumps(document, ensure_ascii=False, default=str), now, now),
            )
            connection.commit()
            document.setdefault("_seq", int(cursor.lastrowid or 0))
        document.setdefault("_created_at", now)
        document["_updated_at"] = now
        return document

    def replace_by_key(self, model_name: object, key_field: str, key_value: Any, data: Mapping[str, Any]) -> dict[str, Any]:
        """按键替换/插入文档。"""
        table = self._table_name(model_name)
        document = dict(data)
        doc_key = str(key_value)
        now = time.time()
        with self._lock:
            connection = self._connect()
            self._ensure_table(connection, table)
            existing = connection.execute(
                f'SELECT seq FROM "{table}" WHERE doc_key = ? ORDER BY seq ASC LIMIT 1', (doc_key,)
            ).fetchone()
            if existing is None:
                cursor = connection.execute(
                    f'INSERT INTO "{table}" (doc_key, doc, created_at, updated_at) VALUES (?, ?, ?, ?)',
                    (doc_key, json.dumps(document, ensure_ascii=False, default=str), now, now),
                )
                document.setdefault("_seq", int(cursor.lastrowid or 0))
                document.setdefault("_created_at", now)
            else:
                sequence = int(existing["seq"])
                connection.execute(
                    f'UPDATE "{table}" SET doc = ?, updated_at = ? WHERE seq = ?',
                    (json.dumps(document, ensure_ascii=False, default=str), now, sequence),
                )
                document["_seq"] = sequence
                document.setdefault("_created_at", now)
            connection.commit()
        document[key_field] = key_value
        document["_updated_at"] = now
        return document

    def delete(self, model_name: object, filters: Mapping[str, Any]) -> int:
        """按过滤器删除文档，返回删除条数。"""
        table = self._table_name(model_name)
        with self._lock:
            connection = self._connect()
            self._ensure_table(connection, table)
            cursor = connection.execute(f'SELECT seq, doc FROM "{table}" ORDER BY seq ASC')
            doomed = [int(row["seq"]) for row in cursor.fetchall() if _doc_matches(self._decode(row["doc"]), filters)]
            for sequence in doomed:
                connection.execute(f'DELETE FROM "{table}" WHERE seq = ?', (sequence,))
            connection.commit()
            return len(doomed)

    @staticmethod
    def _document_key(document: Mapping[str, Any], key_field: str, key_value: Any) -> str | None:
        if key_value is not None:
            return str(key_value)
        if key_field and key_field in document:
            return str(document[key_field])
        if "id" in document:
            return str(document["id"])
        return None

    @staticmethod
    def _decode(raw: object) -> dict[str, Any]:
        if not isinstance(raw, str):
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            logger.warning("maibot 存储里存在无法解析的文档，已跳过：%.120s", raw)
            return {}
        return parsed if isinstance(parsed, dict) else {}


def _doc_matches(document: Mapping[str, Any], filters: Mapping[str, Any]) -> bool:
    """按 MaiBot 的 filters 语义匹配一条文档。"""
    for raw_key, expected in filters.items():
        field = str(raw_key)
        operator = "eq"
        for suffix, name in _FILTER_OPERATORS.items():
            if field.endswith(suffix):
                field = field[: -len(suffix)]
                operator = name
                break
        actual = document.get(field)
        if operator == "eq":
            if actual != expected:
                return False
        elif operator == "ne":
            if actual == expected:
                return False
        elif operator in {"gt", "gte", "lt", "lte"}:
            if not _compare(actual, expected, operator):
                return False
        elif operator == "in":
            if not isinstance(expected, (list, tuple, set)):
                return False
            if actual not in expected:
                return False
        elif operator == "contains":
            if isinstance(actual, str):
                if str(expected) not in actual:
                    return False
            elif isinstance(actual, (list, tuple, set)):
                if expected not in actual:
                    return False
            else:
                return False
    return True


def _compare(actual: object, expected: object, operator: str) -> bool:
    try:
        left = float(actual)  # type: ignore[arg-type]
        right = float(expected)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if operator == "gt":
        return left > right
    if operator == "gte":
        return left >= right
    if operator == "lt":
        return left < right
    return left <= right


# --------------------------------------------------------------------------- #
# 插件运行时
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _PluginRuntime:
    """一个已加载 MaiBot 插件的运行时。

    持有 SDK 上下文、组件索引与能力派发；所有对外回调都绑定在这个对象上，
    所以插件之间互不共享可变状态。
    """

    manifest: PluginManifest
    host: HostServices
    bundle: _SdkBundle
    instance: Any
    module: Any
    legacy: bool
    directory: Path
    module_name: str
    native_id: str
    data_dir: Path
    cache_dir: Path
    context: Any = None
    entries: list[_ComponentEntry] = field(default_factory=list)
    command_specs: list[CommandSpec] = field(default_factory=list)
    tool_specs: list[ToolSpec] = field(default_factory=list)
    event_specs: list[EventSpec] = field(default_factory=list)
    api_specs: list[ApiSpec] = field(default_factory=list)
    llm_providers: list[dict[str, Any]] = field(default_factory=list)
    config_reload_subscriptions: list[str] = field(default_factory=list)
    home_cards: list[dict[str, Any]] = field(default_factory=list)
    message_gateways: list[dict[str, Any]] = field(default_factory=list)
    config_data: dict[str, Any] = field(default_factory=dict)
    config_error: str = ""
    config_pushed: bool = False
    enabled: bool = True
    started: bool = False
    start_error: str = ""
    gateway_state: dict[str, dict[str, Any]] = field(default_factory=dict)
    native: dict[str, Any] = field(default_factory=dict)
    _store: _SqliteStore | None = None
    _store_lock: threading.RLock = field(default_factory=threading.RLock)

    # ------------------------------------------------------------ 只读视图

    @property
    def plugin_id(self) -> str:
        """桥接层使用的插件 id（= 插件目录名）。"""
        return self.manifest.plugin_id

    @property
    def source(self) -> str:
        """日志来源标记。"""
        return f"{BACKEND_NAME}:{self.plugin_id}"

    def components_by_kind(self, kind: str) -> list[_ComponentEntry]:
        """返回某种类型的所有组件。"""
        return [entry for entry in self.entries if entry.kind == kind]

    def find_entry(self, name: str, kinds: Sequence[str] | None = None) -> _ComponentEntry | None:
        """按组件名（或处理函数名）查找一条组件记录。"""
        wanted = str(name or "").strip()
        if not wanted:
            return None
        for entry in self.entries:
            if kinds is not None and entry.kind not in kinds:
                continue
            if wanted in {entry.name, entry.handler_name}:
                return entry
        return None

    def find_api(self, name: str, version: str = "") -> _ComponentEntry | None:
        """按 ``name`` / ``name@version`` / ``plugin_id.name`` 查找 API 组件。"""
        wanted = str(name or "").strip()
        if not wanted:
            return None
        short = wanted.split(".", 1)[-1] if "." in wanted else wanted
        for entry in self.components_by_kind("API"):
            if wanted in {entry.name, f"{entry.name}@{entry.api_version}", f"{self.plugin_id}.{entry.name}"}:
                return entry
            if short == entry.name and (not version or version == entry.api_version):
                return entry
        return None

    def active_entries(self, kind: str) -> list[_ComponentEntry]:
        """返回启用中的某种组件。"""
        return [entry for entry in self.components_by_kind(kind) if entry.enabled]

    # ------------------------------------------------------------ 收集声明

    def collect(self) -> None:
        """调用 ``get_components()`` 并整理成桥接层的能力描述。

        Raises:
            PluginLoadError: 声明的组件结构无法解析到处理函数。
        """
        collector = getattr(self.instance, "get_components", None)
        if not callable(collector):
            self.host.log("warning", f"插件 {self.plugin_id} 没有 get_components()，按无组件处理", source=self.source)
            declarations: list[Any] = []
        else:
            declarations = list(collector() or [])

        for raw in declarations:
            if not isinstance(raw, Mapping):
                self.host.log("warning", f"插件 {self.plugin_id} 的组件声明不是字典，已跳过：{raw!r}", source=self.source)
                continue
            entry = self._build_entry(raw)
            if entry is None:
                continue
            duplicate = self.find_entry(entry.name)
            if duplicate is not None:
                self.host.log(
                    "warning",
                    f"插件 {self.plugin_id} 重复声明组件 {entry.name}（{duplicate.kind}/{entry.kind}），保留先声明的那个",
                    source=self.source,
                )
                continue
            self.entries.append(entry)

        for entry in self.entries:
            if entry.kind == "COMMAND" and isinstance(entry.spec, CommandSpec):
                self.command_specs.append(entry.spec)
            elif entry.kind in {"TOOL", "ACTION"} and isinstance(entry.spec, ToolSpec):
                self.tool_specs.append(entry.spec)
            elif entry.kind in {"EVENT_HANDLER", "HOOK_HANDLER"} and isinstance(entry.spec, EventSpec):
                self.event_specs.append(entry.spec)
            elif entry.kind == "API" and isinstance(entry.spec, ApiSpec):
                self.api_specs.append(entry.spec)

        self.llm_providers = self._collect_llm_providers()
        self.config_reload_subscriptions = self._collect_config_subscriptions()
        if self.llm_providers:
            self.host.log(
                "warning",
                f"插件 {self.plugin_id} 声明了 LLM Provider {[item.get('client_type') for item in self.llm_providers]}，"
                "N.E.K.O. 侧不支持注册 Provider，这些声明只做展示",
                source=self.source,
            )
        if self.home_cards:
            self.host.log(
                "debug",
                f"插件 {self.plugin_id} 声明了 {len(self.home_cards)} 个 HomeCard，桥接层不做 WebUI 渲染",
                source=self.source,
            )
        if self.message_gateways:
            self.host.log(
                "warning",
                f"插件 {self.plugin_id} 声明了消息网关 {[item.get('name') for item in self.message_gateways]}，"
                "N.E.K.O. 侧没有入站网关面，网关能力会返回结构化失败",
                source=self.source,
            )
        self.native.update(self._native_component_state())

    def _build_entry(self, raw: Mapping[str, Any]) -> _ComponentEntry | None:
        name = str(raw.get("name") or "").strip()
        raw_type = raw.get("type") or raw.get("component_type") or ""
        if not name:
            self.host.log("warning", f"插件 {self.plugin_id} 的组件声明缺少 name，已跳过：{raw!r}", source=self.source)
            return None
        try:
            kind = _normalize_component_type(self.bundle, raw_type)
        except ValueError as exc:
            self.host.log("warning", f"插件 {self.plugin_id} 组件 {name} 类型无法识别（{exc}），已跳过", source=self.source)
            return None
        raw_metadata = raw.get("metadata")
        metadata: dict[str, Any] = dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {}
        for key, value in raw.items():
            if key in {"name", "type", "component_type", "metadata"}:
                continue
            metadata.setdefault(key, value)
        if not metadata.get("description") and raw.get("description"):
            metadata["description"] = raw.get("description")

        handler_name = str(metadata.get("handler_name") or "").strip()
        dynamic = bool(metadata.get("dynamic")) or (not handler_name)
        if not dynamic and not callable(getattr(self.instance, handler_name, None)):
            invoke_component = getattr(self.instance, "invoke_component", None)
            if callable(invoke_component):
                dynamic = True
            elif not self.legacy:
                self.host.log(
                    "warning",
                    f"插件 {self.plugin_id} 组件 {name} 的处理函数 {handler_name} 不存在，调用时会失败",
                    source=self.source,
                )

        entry = _ComponentEntry(name=name, kind=kind, handler_name=handler_name, metadata=metadata, dynamic=dynamic)
        spec = self._build_spec(entry)
        if spec is None:
            return None
        entry.spec = spec
        return entry

    def _build_spec(self, entry: _ComponentEntry) -> CommandSpec | ToolSpec | EventSpec | ApiSpec | None:
        metadata = entry.metadata
        description = str(metadata.get("description") or "")
        if entry.kind == "COMMAND":
            pattern = str(metadata.get("command_pattern") or metadata.get("pattern") or "").strip()
            aliases = _string_tuple(metadata.get("aliases"))
            return CommandSpec(
                id=entry.name,
                name=entry.name,
                description=description,
                patterns=(pattern,) if pattern else (),
                aliases=aliases,
                priority=_as_int(metadata.get("weight") or metadata.get("priority"), 0),
                hidden=bool(metadata.get("hidden", False)),
                parameters=None,
            )
        if entry.kind in {"TOOL", "ACTION"}:
            origin = "maibot:ACTION" if str(metadata.get("legacy_component_type") or "").upper() == "ACTION" or metadata.get("legacy_action") else "maibot:TOOL"
            return ToolSpec(
                name=entry.name,
                description=_tool_description(self.bundle, entry, description),
                parameters=_tool_parameters_schema(self.bundle, entry),
                timeout=_as_float(metadata.get("timeout"), 30.0),
                origin=origin,
            )
        if entry.kind == "EVENT_HANDLER":
            event_type = str(metadata.get("event_type") or "on_message").strip() or "on_message"
            return EventSpec(
                event_type=event_type,
                description=description,
                blocking=bool(metadata.get("intercept_message", False)),
                priority=_as_int(metadata.get("weight"), 0),
            )
        if entry.kind == "HOOK_HANDLER":
            hook = str(metadata.get("hook") or "").strip() or entry.name
            mode = str(metadata.get("mode") or "blocking").strip().lower()
            order = str(metadata.get("order") or "normal").strip().lower()
            return EventSpec(
                event_type=hook,
                description=description or f"hook {hook}",
                blocking=mode != "observe",
                priority=_HOOK_ORDER_PRIORITY.get(order, 200),
            )
        if entry.kind == "API":
            return ApiSpec(
                name=entry.name,
                description=description,
                version=entry.api_version,
                parameters=None,
            )
        if entry.kind == "HOME_CARD":
            self.home_cards.append({"name": entry.name, "title": metadata.get("title", ""), "order": metadata.get("order", 0)})
            return None
        if entry.kind == "MESSAGE_GATEWAY":
            self.message_gateways.append(
                {
                    "name": entry.name,
                    "route_type": str(metadata.get("route_type") or ""),
                    "platform": str(metadata.get("platform") or ""),
                    "protocol": str(metadata.get("protocol") or ""),
                }
            )
            return None
        self.host.log("warning", f"插件 {self.plugin_id} 的组件 {entry.name} 类型 {entry.kind} 没有桥接映射，已记录但不可调用", source=self.source)
        return None

    def _collect_llm_providers(self) -> list[dict[str, Any]]:
        collector = getattr(self.instance, "get_llm_providers", None)
        providers: list[Any] = []
        if callable(collector):
            try:
                providers = list(collector() or [])
            except Exception as exc:  # noqa: BLE001 — 声明收集失败不能拖垮加载
                self.host.log("warning", f"插件 {self.plugin_id} 收集 LLM Provider 失败：{exc!r}", source=self.source)
                providers = []
        if not providers:
            declared = getattr(self.instance, "_llm_providers", None)
            if isinstance(declared, Mapping):
                providers = [{"client_type": key, "name": str(key)} for key in declared]
        result: list[dict[str, Any]] = []
        for item in providers:
            if isinstance(item, Mapping):
                result.append({str(key): _jsonable(value) for key, value in item.items()})
        return result

    def _collect_config_subscriptions(self) -> list[str]:
        collector = getattr(self.instance, "get_config_reload_subscriptions", None)
        if not callable(collector):
            return []
        try:
            return [str(item) for item in (collector() or [])]
        except (TypeError, ValueError) as exc:
            self.host.log("warning", f"插件 {self.plugin_id} 的配置热重载订阅非法：{exc}", source=self.source)
            return []

    # ------------------------------------------------------------ 上下文注入

    def build_context(self) -> Any:
        """按官方 Runner 的方式构造并注入 ``PluginContext``。"""
        plugin_paths = self.bundle.context_module.PluginPaths(data_dir=self.data_dir, runtime_dir=self.cache_dir)
        context = self.bundle.context_module.PluginContext(
            plugin_id=self.plugin_id,
            rpc_call=self.rpc_call,
            paths=plugin_paths,
        )
        setter = getattr(self.instance, "_set_context", None)
        if callable(setter):
            setter(context)
        else:
            self.host.log(
                "warning",
                f"插件 {self.plugin_id} 的实例没有 _set_context()，能力调用会因为缺少上下文而失败",
                source=self.source,
            )
        self.context = context
        return context

    # ------------------------------------------------------------ 配置

    def apply_config(self, data: Mapping[str, Any] | None, *, origin: str) -> bool:
        """把一份配置推给插件（失败时回落到默认配置）。"""
        setter = getattr(self.instance, "set_plugin_config", None)
        if not callable(setter):
            return False
        payload = dict(data) if isinstance(data, Mapping) else {}
        if not payload:
            self.native["config_present"] = False
            return False
        try:
            setter(payload)
        except Exception as exc:  # noqa: BLE001 — 配置模型校验失败只记录
            if self._apply_default_config():
                self.config_error = f"{origin}: {type(exc).__name__}: {exc}（已回落默认配置）"
                self.host.log(
                    "warning",
                    f"插件 {self.plugin_id} 的配置（{origin}）校验失败，已回落默认配置：{type(exc).__name__}: {exc}",
                    source=self.source,
                )
                return False
            self.config_error = f"{origin}: {type(exc).__name__}: {exc}"
            self.host.log(
                "error",
                f"插件 {self.plugin_id} 的配置（{origin}）无法应用且默认配置也不可用：{type(exc).__name__}: {exc}",
                source=self.source,
            )
            return False
        self.config_error = ""
        self.config_data = dict(self.read_instance_config())
        self.native["config_present"] = True
        self.native["config"] = _jsonable(self.config_data)
        return True

    def _apply_default_config(self) -> bool:
        builder = getattr(self.instance, "get_default_config", None)
        if not callable(builder):
            return False
        try:
            defaults = builder()
        except Exception as exc:  # noqa: BLE001
            logger.debug("插件 %s 构造默认配置失败：%r", self.plugin_id, exc)
            return False
        if not isinstance(defaults, Mapping) or not defaults:
            return False
        setter = getattr(self.instance, "set_plugin_config", None)
        if not callable(setter):
            return False
        try:
            setter(dict(defaults))
        except Exception as exc:  # noqa: BLE001
            logger.debug("插件 %s 推送默认配置失败：%r", self.plugin_id, exc)
            return False
        self.config_data = dict(self.read_instance_config())
        self.native["config"] = _jsonable(self.config_data)
        return True

    def read_instance_config(self) -> dict[str, Any]:
        """读取插件实例当前持有的配置字典。"""
        reader = getattr(self.instance, "get_plugin_config_data", None)
        if callable(reader):
            try:
                data = reader()
            except Exception as exc:  # noqa: BLE001
                logger.debug("插件 %s 读取自身配置失败：%r", self.plugin_id, exc)
                return dict(self.config_data)
            if isinstance(data, Mapping):
                return {str(key): value for key, value in data.items()}
        return dict(self.config_data)

    def read_plugin_toml(self) -> dict[str, Any]:
        """同步读取插件目录里的 ``config.toml``（官方 Robot 的插件级配置）。"""
        path = self.directory / CONFIG_FILE
        if not path.is_file():
            return {}
        try:
            with path.open("rb") as handle:
                parsed = tomllib.load(handle)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            self.host.log("warning", f"插件 {self.plugin_id} 的 {CONFIG_FILE} 解析失败：{exc}", source=self.source)
            return {}
        return {str(key): value for key, value in parsed.items()} if isinstance(parsed, Mapping) else {}

    async def push_host_config(self) -> None:
        """从 N.E.K.O. 读取用户配置并推给插件（尽力而为）。"""
        try:
            data = await self.host.plugin_config(self.plugin_id)
        except Exception as exc:  # noqa: BLE001 — 配置读取失败不能挡住启动
            self.config_error = f"host.plugin_config: {type(exc).__name__}: {exc}"
            self.host.log("warning", f"读取插件 {self.plugin_id} 的 N.E.K.O. 配置失败：{exc!r}", source=self.source)
            return
        if isinstance(data, Mapping) and data:
            self.apply_config(data, origin="host.plugin_config")
        self.config_pushed = True

    async def ensure_config_for_call(self) -> None:
        """首次派发前补一次 N.E.K.O. 配置（避免 load 时只能读插件目录的 TOML）。"""
        if self.config_pushed:
            return
        await self.push_host_config()

    # ------------------------------------------------------------ 生命周期

    async def call_lifecycle(self, name: str) -> None:
        """调用插件的生命周期方法，容忍基类占位实现与半启动状态。"""
        handler = getattr(self.instance, name, None)
        if not callable(handler):
            logger.debug("插件 %s 没有 %s()", self.plugin_id, name)
            return
        if not self.legacy and self.bundle.plugin_class is not None:
            base_handler = getattr(self.bundle.plugin_class, name, None)
            if base_handler is not None and getattr(type(self.instance), name, None) is base_handler:
                self.host.log(
                    "warning",
                    f"插件 {self.plugin_id} 没有覆盖 {name}()（SDK 基类占位实现），跳过",
                    source=self.source,
                )
                return
        try:
            result = handler()
        except NotImplementedError as exc:
            self.host.log("warning", f"插件 {self.plugin_id} 的 {name}() 未实现：{exc}", source=self.source)
            return
        if inspect.isawaitable(result):
            await result

    # ------------------------------------------------------------ 组件调用

    async def invoke(self, entry: _ComponentEntry, kwargs: Mapping[str, Any]) -> Any:
        """调用一条组件（sync/async 处理函数都支持）。

        Raises:
            MaiBotCallError: 组件没有可调用的处理函数，或调用本身抛错。
        """
        if not entry.enabled:
            raise MaiBotCallError(entry.name, f"组件 {entry.name} 已被禁用")
        payload = dict(kwargs)
        handler: Any = None
        if not self.legacy and not entry.dynamic and entry.handler_name:
            handler = getattr(self.instance, entry.handler_name, None)
        if not callable(handler):
            invoker = getattr(self.instance, "invoke_component", None)
            if callable(invoker):
                try:
                    return await _maybe_await(invoker(entry.name, **payload))
                except MaiBotCallError:
                    raise
                except Exception as exc:  # noqa: BLE001 — 统一转成结构化桥接错误
                    raise MaiBotCallError(entry.name, f"{type(exc).__name__}: {exc}") from exc
            raise MaiBotCallError(entry.name, "组件没有可调用的处理函数")
        try:
            if inspect.iscoroutinefunction(handler):
                return await handler(**payload)
            # 同步处理函数放到线程里，避免阻塞桥接层事件循环（与官方 Runner 一致）。
            return await _maybe_await(await asyncio.to_thread(handler, **payload))
        except MaiBotCallError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MaiBotCallError(entry.name, f"{type(exc).__name__}: {exc}") from exc

    # ------------------------------------------------------------ 命令

    def _match_command(self, text: str) -> tuple[_ComponentEntry, dict[str, str]] | None:
        """按 MaiBot 的语义匹配命令：先正则 ``search``，再别名 ``startswith``。"""
        candidates = sorted(self.active_entries("COMMAND"), key=lambda item: (-_as_int(item.metadata.get("weight"), 0), item.name))
        for entry in candidates:
            pattern = str(entry.metadata.get("command_pattern") or entry.metadata.get("pattern") or "").strip()
            if pattern:
                try:
                    compiled = re.compile(pattern)
                except re.error as exc:
                    self.host.log(
                        "warning",
                        f"插件 {self.plugin_id} 命令 {entry.name} 的正则无法编译：{exc}",
                        source=self.source,
                    )
                else:
                    match = compiled.search(text)
                    if match is not None:
                        return entry, {str(key): str(value) for key, value in match.groupdict().items()}
            for alias in _string_tuple(entry.metadata.get("aliases")):
                if alias and text.startswith(alias):
                    return entry, {}
        return None

    def _command_kwargs(self, context: CommandContext, matched_groups: Mapping[str, str]) -> dict[str, Any]:
        """构造与官方 Runner 一致的命令调用参数。"""
        raw = context.raw if isinstance(context.raw, Mapping) else {}
        message = raw.get("message")
        stream_id = str(raw.get("stream_id") or context.group_id or context.user_id or context.channel or BACKEND_NAME)
        if not isinstance(message, Mapping):
            message = {
                "plain_text": context.text,
                "raw_message": context.text,
                "session_id": stream_id,
                "stream_id": stream_id,
                "is_group_message": context.is_group,
                "user_id": context.user_id,
                "user_name": context.user_name,
                "group_id": context.group_id,
                "platform": str(raw.get("platform") or context.channel or BACKEND_NAME),
            }
        invoke_args: dict[str, Any] = {
            "text": context.text,
            "stream_id": stream_id,
            "group_id": context.group_id,
            "platform": str(raw.get("platform") or context.channel or BACKEND_NAME),
            "user_id": context.user_id,
            "is_local_operator": bool(raw.get("is_local_operator", False)),
            "matched_groups": dict(matched_groups),
            "message": dict(message),
        }
        if self.config_data:
            invoke_args["plugin_config"] = dict(self.config_data)
        return invoke_args

    async def handle_message(self, context: CommandContext) -> CommandResult:
        """在一条入站消息上跑本插件的命令。"""
        result = CommandResult(
            handled=False,
            plugin_id=self.plugin_id,
            framework=BACKEND_NAME,
        )
        if not self.enabled:
            return result
        text = str(getattr(context, "text", "") or "")
        if not text.strip():
            return result
        try:
            await self.ensure_config_for_call()
            matched = self._match_command(text)
            if matched is None:
                return result
            entry, matched_groups = matched
            raw_result = await self.invoke(entry, self._command_kwargs(context, matched_groups))
            replies, data = _extract_command_result(raw_result)
            result.handled = True
            result.command = entry.name
            result.replies = replies
            result.data = {
                **data,
                "matched_groups": dict(matched_groups),
                "component": entry.name,
                "handler": entry.handler_name,
                **_handler_result_fields(raw_result),
            }
            if data.get("success") is False and not result.error:
                result.error = _result_error_text(raw_result) or f"命令 {entry.name} 返回失败"
            return result
        except MaiBotCallError as exc:
            result.handled = True
            result.error = str(exc)
            self.host.log("error", f"{self.source} 处理消息失败：{exc.detail or exc}", source=self.source)
            return result
        except Exception as exc:  # noqa: BLE001 — 外来插件的异常要变成结构化结果
            result.handled = True
            result.error = f"{type(exc).__name__}: {exc}"
            self.host.log("error", f"{self.source} 处理消息抛错：{result.error}", source=self.source)
            return result

    # ------------------------------------------------------------ 工具 / API / 事件

    async def call_tool(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """调用插件注册的工具（含旧版 Action）。"""
        entry = self.find_entry(name, kinds=["TOOL", "ACTION"])
        if entry is None:
            raise MaiBotCallError(name, f"插件 {self.plugin_id} 没有工具 {name}")
        await self.ensure_config_for_call()
        payload = dict(arguments or {})
        if self.legacy:
            payload = {
                **payload,
                "function_args": dict(payload),
                "action_data": dict(payload),
                "plugin_config": dict(self.config_data),
            }
        return await self.invoke(entry, payload)

    async def call_api(self, name: str, arguments: Mapping[str, Any]) -> Any:
        """调用插件暴露的 API（供其它插件互调）。"""
        entry = self.find_api(name)
        if entry is None:
            raise MaiBotCallError(name, f"插件 {self.plugin_id} 没有 API {name}")
        await self.ensure_config_for_call()
        return await self.invoke(entry, dict(arguments or {}))

    async def dispatch_event(self, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        """把一条事件派发给订阅了它的处理器。"""
        wanted = _canonical_event_type(event_type)
        event_kwargs = _event_kwargs(payload)
        results: list[dict[str, Any]] = []
        errors: list[str] = []
        delivered = 0
        for spec, entry in self._matching_events(wanted):
            try:
                value = await self.invoke(entry, event_kwargs)
            except MaiBotCallError as exc:
                errors.append(str(exc))
                self.host.log("warning", f"{self.source} 事件 {event_type}（{entry.name}）失败：{exc.detail or exc}", source=self.source)
                continue
            delivered += 1
            results.append({"component": entry.name, "event_type": spec.event_type, "result": _jsonable(value)})
        return {
            "event_type": event_type,
            "plugin_id": self.plugin_id,
            "delivered": delivered,
            "results": results,
            "errors": errors,
        }

    def _matching_events(self, event_type: str) -> list[tuple[EventSpec, _ComponentEntry]]:
        wanted_names = {event_type}
        for canonical, aliases in _MESSAGE_EVENT_ALIASES.items():
            if event_type == canonical:
                wanted_names.update(aliases)
        matched: list[tuple[EventSpec, _ComponentEntry]] = []
        for entry in self.entries:
            if entry.kind not in {"EVENT_HANDLER", "HOOK_HANDLER"} or not isinstance(entry.spec, EventSpec):
                continue
            if not entry.enabled:
                continue
            candidates = {entry.spec.event_type, entry.spec.event_type.removeprefix(_HOOK_PREFIX)}
            if entry.kind == "HOOK_HANDLER":
                candidates.add(_HOOK_PREFIX + entry.spec.event_type)
            if wanted_names & candidates:
                matched.append((entry.spec, entry))
        matched.sort(key=lambda item: (-item[0].priority, item[1].name))
        return matched

    # ------------------------------------------------------------ native 状态

    def _native_component_state(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for entry in self.entries:
            counts[entry.kind] = counts.get(entry.kind, 0) + 1
        return {
            "sdk": {"name": "maibot_sdk", "version": self.bundle.version, "source": self.bundle.source, "path": self.bundle.path},
            "native_id": self.native_id,
            "directory": str(self.directory),
            "layout": str(self.manifest.extra.get("layout") or ""),
            "manifest_version": self.manifest.extra.get("manifest_version"),
            "manifest_error": str(self.manifest.extra.get("manifest_error") or ""),
            "legacy_adapter": self.legacy,
            "module": self.module_name,
            "component_counts": counts,
            "component_names": [entry.name for entry in self.entries],
            "commands": [spec.id for spec in self.command_specs],
            "tools": [spec.name for spec in self.tool_specs],
            "events": [spec.event_type for spec in self.event_specs],
            "apis": [f"{spec.name}@{spec.version}" for spec in self.api_specs],
            "llm_providers": list(self.llm_providers),
            "config_reload_subscriptions": list(self.config_reload_subscriptions),
            "home_cards": list(self.home_cards),
            "message_gateways": list(self.message_gateways),
            "capabilities_declared": list(self.manifest.extra.get("capabilities") or []),
            "plugin_type": str(self.manifest.extra.get("plugin_type") or ""),
            "data_dir": str(self.data_dir),
            "cache_dir": str(self.cache_dir),
        }

    def refresh_native(self) -> None:
        """把运行期状态写回 ``LoadedPlugin.native``（bridge_status 用）。"""
        self.native.update(self._native_component_state())
        self.native["enabled"] = self.enabled
        self.native["disabled_components"] = [entry.name for entry in self.entries if not entry.enabled]
        self.native["started"] = self.started
        self.native["start_error"] = self.start_error
        self.native["config_error"] = self.config_error
        self.native["config_present"] = bool(self.config_data)
        self.native["gateway_state"] = _jsonable(self.gateway_state)

    def set_enabled(self, enabled: bool) -> bool:
        """启用/停用整个插件（桥接层调度时会跳过停用插件）。"""
        self.enabled = bool(enabled)
        self.refresh_native()
        return self.enabled

    def set_component_enabled(self, name: str, enabled: bool) -> _ComponentEntry | None:
        """启用/停用单个组件（真实影响匹配与调用）。"""
        entry = self.find_entry(name)
        if entry is None:
            return None
        entry.enabled = bool(enabled)
        self.refresh_native()
        return entry

    # ------------------------------------------------------------ 存储

    @property
    def store(self) -> _SqliteStore:
        """本插件独立的 sqlite 存储。"""
        with self._store_lock:
            if self._store is None:
                self._store = _SqliteStore(self.data_dir / "maibot_store.sqlite3")
            return self._store

    # ------------------------------------------------------------ RPC

    async def rpc_call(
        self,
        method: str,
        plugin_id: str = "",
        payload: Mapping[str, Any] | None = None,
        timeout_ms: int | None = None,
    ) -> Any:
        """``PluginContext`` 的 Host RPC 实现。

        与官方 Runner 一样强制绑定当前插件身份：插件无法以别的身份发起调用。

        Raises:
            PermissionError: 调用了不在白名单里的 Host 原始方法。
            ValueError: ``timeout_ms`` 非法。
        """
        normalized_method = str(method or "").strip()
        if normalized_method not in _ALLOWED_RAW_HOST_METHODS:
            raise PermissionError(
                f"maibot 插件 {self.plugin_id} 不允许直接调用 Host 原始 RPC 方法：{normalized_method or '<empty>'}"
            )
        if plugin_id and plugin_id != self.plugin_id:
            self.host.log(
                "warning",
                f"maibot 插件 {self.plugin_id} 尝试以 {plugin_id} 身份发起 RPC，已强制绑定回自身身份",
                source=self.source,
            )
        if timeout_ms is not None:
            try:
                parsed_timeout = int(timeout_ms)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"RPC timeout_ms 无效：{timeout_ms!r}") from exc
            if parsed_timeout <= 0:
                raise ValueError(f"RPC timeout_ms 必须大于 0：{timeout_ms!r}")

        arguments = dict(payload) if isinstance(payload, Mapping) else {}
        if normalized_method == "cap.call":
            capability = str(arguments.get("capability") or "").strip()
            raw_args = arguments.get("args")
            capability_args: dict[str, Any] = dict(raw_args) if isinstance(raw_args, Mapping) else {}
            return await self.call_capability(capability, capability_args)
        if normalized_method == "host.route_message":
            gateway_name = str(arguments.get("gateway_name") or "")
            self.host.log(
                "warning",
                f"maibot 插件 {self.plugin_id} 请求通过网关 {gateway_name or '<unnamed>'} 注入外部消息："
                "N.E.K.O. 桥接层没有入站注入面，已返回未接受",
                source=self.source,
            )
            return {
                "success": True,
                "accepted": False,
                "routed": False,
                "reason": "N.E.K.O. bridge has no inbound gateway injection surface",
            }
        state = {
            "ready": bool(arguments.get("ready", False)),
            "platform": str(arguments.get("platform") or ""),
            "account_id": str(arguments.get("account_id") or ""),
            "scope": str(arguments.get("scope") or ""),
            "metadata": _jsonable(arguments.get("metadata") or {}),
            "updated_at": time.time(),
        }
        gateway_name = str(arguments.get("gateway_name") or "")
        self.gateway_state[gateway_name or f"gateway_{len(self.gateway_state) + 1}"] = state
        self.refresh_native()
        return {"success": True, "accepted": True, "state": state}

    # ------------------------------------------------------------ 能力派发

    async def call_capability(self, capability: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """执行一次 ``cap.call``。

        Returns:
            dict[str, Any]: SDK 期望的字典；成功时含 ``success`` 与该能力对应的键，
            没有等价实现时返回 ``unsupported_capability`` 结构化失败。
        """
        name = str(capability or "").strip()
        if not name:
            return _failure("cap.call", "capability name is empty", code="bad_request")
        arguments = dict(args) if isinstance(args, Mapping) else {}
        if _is_unsupported_capability(name):
            return self._unsupported(name, "declared unsupported in the bridge capability table")
        try:
            if name.startswith("send."):
                return await self._cap_send(name, arguments)
            if name.startswith("llm."):
                return await self._cap_llm(name, arguments)
            if name.startswith("database."):
                return await self._cap_database(name, arguments)
            if name.startswith("config."):
                return await self._cap_config(name, arguments)
            if name.startswith("api."):
                return await self._cap_api(name, arguments)
            if name.startswith("component."):
                return await self._cap_component(name, arguments)
            if name.startswith("message."):
                return await self._cap_message(name, arguments)
            if name.startswith("chat."):
                return await self._cap_chat(name, arguments)
            if name == "tool.get_definitions":
                return self._cap_tool_definitions()
        except UnsupportedCapabilityError as exc:
            return self._unsupported(name, exc.detail or str(exc))
        except Exception as exc:  # noqa: BLE001 — 能力失败必须变成结构化结果，不能让插件收到异常栈
            detail = f"{type(exc).__name__}: {exc}"
            self.host.log("error", f"maibot 能力 {name} 执行失败：{detail}", source=self.source)
            return _failure(name, detail)
        return self._unsupported(name)

    def _unsupported(self, capability: str, detail: str = "") -> dict[str, Any]:
        """结构化失败：诚实告知没有等价实现。

        ``error`` 固定为契约约定的那句话（``"<capability> is not mapped to N.E.K.O."``），
        额外上下文只写进日志，保持返回结构稳定、便于调用方判断。
        """
        message = _UNSUPPORTED_ERROR_TEMPLATE.format(capability=capability)
        log_message = f"maibot 能力 {capability} 没有 N.E.K.O. 等价实现，已返回结构化失败"
        if detail:
            log_message = f"{log_message}（{detail}）"
        self.host.log("warning", log_message, source=self.source)
        return {"success": False, "code": "unsupported_capability", "error": message}

    # --------------------------------------------------------- send.*

    async def _cap_send(self, capability: str, args: Mapping[str, Any]) -> dict[str, Any]:
        stream_id = str(args.get("stream_id") or args.get("chat_id") or "")
        text, degraded, dropped = _send_text_payload(capability, args)
        if text is None:
            return self._unsupported(capability, "payload has no text N.E.K.O. can deliver")
        if not text.strip():
            return _failure(capability, "send payload is empty", value_key="sent")
        metadata: dict[str, Any] = {
            "maibot_capability": capability,
            "maibot_plugin_id": self.native_id,
            "maibot_bridge_plugin": self.plugin_id,
        }
        if stream_id:
            metadata["maibot_stream_id"] = stream_id
        if degraded:
            metadata["degraded"] = True
            metadata["dropped_segments"] = dropped
        delivered = await self.host.send_chat(text, source=f"{BACKEND_NAME}:{self.plugin_id}", metadata=metadata)
        if degraded:
            self.host.log(
                "warning",
                f"maibot {capability} 里的非文本段（{dropped} 个）无法投递到 N.E.K.O.，只发送了文本部分",
                source=self.source,
            )
        return {
            "success": bool(delivered),
            "sent": bool(delivered),
            "message_id": None,
            "degraded": degraded,
            "dropped_segments": dropped,
        }

    # --------------------------------------------------------- llm.*

    async def _cap_llm(self, capability: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if capability in {"llm.generate", "llm.generate_with_tools"}:
            prompt = args.get("prompt")
            if isinstance(prompt, str) and not prompt.strip():
                return _failure(capability, "prompt is empty")
            if not isinstance(prompt, str) and not isinstance(prompt, (list, tuple)):
                return _failure(capability, "prompt must be a string or a message list")
            task_name = str(args.get("task_name") or "")
            model = str(args.get("model_name") or args.get("model") or "")
            if task_name and not args.get("model_name"):
                self.host.log(
                    "debug",
                    f"maibot 插件请求模型任务 {task_name}：N.E.K.O. 侧只有一条对话模型通道，已忽略任务名",
                    source=self.source,
                )
            tools = args.get("tools") if capability == "llm.generate_with_tools" else None
            result = await self.host.llm_generate(
                prompt,
                model=model,
                system=str(args.get("system") or ""),
                temperature=_as_optional_float(args.get("temperature")),
                max_tokens=_as_optional_int(args.get("max_tokens")),
                tools=tools if isinstance(tools, (list, tuple)) else None,
            )
            return _llm_payload(capability, result)
        if capability == "llm.embed":
            single = args.get("text")
            raw_texts = args.get("texts")
            payload: list[str] = []
            single_text = str(single) if isinstance(single, str) and single.strip() else ""
            if single_text:
                payload.append(single_text)
            elif isinstance(raw_texts, (list, tuple)):
                payload.extend(str(item) for item in raw_texts)
            if not payload:
                return _failure(capability, "llm.embed needs text or texts")
            vectors = await self.host.llm_embed(payload)
            rows = [list(vector) for vector in (vectors or [])]
            if single_text:
                return {"success": True, "embedding": rows[0] if rows else [], "model": "", "count": len(rows)}
            return {
                "success": True,
                "results": [{"index": index, "embedding": vector} for index, vector in enumerate(rows)],
                "count": len(rows),
            }
        return self._unsupported(capability)

    # --------------------------------------------------------- database.*

    async def _cap_database(self, capability: str, args: Mapping[str, Any]) -> dict[str, Any]:
        enabled = getattr(self.host, "capability_enabled", None)
        if callable(enabled) and not enabled("storage"):
            return self._unsupported(capability, "已在 [bridge.capabilities].storage 关闭")
        model_name = args.get("model_name")
        if not model_name:
            return _failure(capability, "model_name is required", value_key="result")
        filters = args.get("filters") if isinstance(args.get("filters"), Mapping) else {}
        store = self.store
        if capability == "database.query":
            query_type = str(args.get("query_type") or "get").strip().lower()
            data = args.get("data") if isinstance(args.get("data"), Mapping) else {}
            if query_type in {"create", "insert", "save"}:
                return {"success": True, "result": await asyncio.to_thread(store.insert, model_name, data)}
            if query_type in {"update", "set"}:
                if not data:
                    return _failure(capability, "database.query(update) needs data", value_key="result")
                if not filters:
                    return _failure(capability, "database.query(update) needs filters", value_key="result")
                updated = await asyncio.to_thread(self._update_rows, store, model_name, filters, data)
                return {"success": True, "result": updated}
            if query_type in {"delete", "remove"}:
                deleted = await asyncio.to_thread(store.delete, model_name, filters)
                return {"success": True, "result": deleted, "count": deleted}
            if query_type in {"count"}:
                rows = await asyncio.to_thread(store.rows, model_name)
                matched = [row for row in rows if _doc_matches(row, filters)]
                return {"success": True, "count": len(matched), "result": len(matched)}
            return self._query_rows(capability, store, model_name, args)
        if capability == "database.get":
            return self._query_rows(capability, store, model_name, args)
        if capability == "database.save":
            data = args.get("data") if isinstance(args.get("data"), Mapping) else {}
            if not data:
                return _failure(capability, "database.save needs data", value_key="result")
            key_field = str(args.get("key_field") or "id")
            key_value = args.get("key_value")
            if key_value is None:
                saved = await asyncio.to_thread(store.insert, model_name, data, key_field=key_field)
            else:
                saved = await asyncio.to_thread(store.replace_by_key, model_name, key_field, key_value, data)
            return {"success": True, "result": saved}
        if capability == "database.delete":
            deleted = await asyncio.to_thread(store.delete, model_name, filters)
            return {"success": True, "result": deleted, "count": deleted}
        if capability == "database.count":
            count = await asyncio.to_thread(self._count_rows, store, model_name, filters)
            return {"success": True, "count": count, "result": count}
        return self._unsupported(capability)

    def _update_rows(
        self,
        store: _SqliteStore,
        model_name: object,
        filters: Mapping[str, Any],
        data: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        updated: list[dict[str, Any]] = []
        for row in store.rows(model_name):
            if not _doc_matches(row, filters):
                continue
            merged = {key: value for key, value in row.items() if not key.startswith("_")}
            merged.update(data)
            key_value = row.get("id")
            if key_value is None:
                key_value = row.get("_seq")
            updated.append(store.replace_by_key(model_name, "id", key_value, merged))
        return updated

    @staticmethod
    def _count_rows(store: _SqliteStore, model_name: object, filters: Mapping[str, Any]) -> int:
        return len([row for row in store.rows(model_name) if _doc_matches(row, filters)])

    def _query_rows(
        self,
        capability: str,
        store: _SqliteStore,
        model_name: object,
        args: Mapping[str, Any],
    ) -> dict[str, Any]:
        filters = args.get("filters") if isinstance(args.get("filters"), Mapping) else {}
        rows = [row for row in store.rows(model_name) if _doc_matches(row, filters)]
        order_by = args.get("order_by")
        rows = _apply_order(rows, order_by)
        limit = _as_optional_int(args.get("limit"))
        if limit is not None and limit >= 0:
            rows = rows[:limit]
        single = bool(args.get("single_result", False))
        payload_rows = [_strip_internal(row) for row in rows]
        if single:
            return {"success": True, "result": payload_rows[0] if payload_rows else None}
        return {"success": True, "result": payload_rows}

    # --------------------------------------------------------- config.*

    async def _cap_config(self, capability: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if capability == "config.get":
            data = await self.host.plugin_config(self.plugin_id)
            key = str(args.get("key") or "")
            value = _dotted_get(data, key) if key else None
            if value is None:
                value = args.get("default")
            return {"success": True, "value": value}
        if capability == "config.get_plugin":
            target = str(args.get("plugin_name") or "") or self.plugin_id
            data = await self.host.plugin_config(target)
            return {"success": True, "value": dict(data) if isinstance(data, Mapping) else {}}
        if capability == "config.get_all":
            data = await self.host.plugin_config(self.plugin_id)
            return {"success": True, "value": dict(data) if isinstance(data, Mapping) else {}}
        return self._unsupported(capability)

    # --------------------------------------------------------- api.*

    async def _cap_api(self, capability: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if capability == "api.list":
            provider = str(args.get("plugin_id") or "")
            return {"success": True, "apis": [meta for meta in list_registered_apis(self.host) if not provider or meta["plugin_id"] == provider]}
        if capability == "api.get":
            name = str(args.get("api_name") or "")
            version = str(args.get("version") or "")
            meta = find_registered_api(name, version, host=self.host)
            return {"success": True, "api": meta, "value": meta}
        if capability == "api.call":
            name = str(args.get("api_name") or "")
            version = str(args.get("version") or "")
            call_args = args.get("args") if isinstance(args.get("args"), Mapping) else {}
            if not name:
                return _failure(capability, "api_name is required", value_key="result")
            meta = find_registered_api(name, version, host=self.host)
            if meta is None:
                # 本桥没人暴露 → 落到 N.E.K.O. 插件：api_name 写成 "<插件id>:<入口>"（或最后一个 "." 分隔）
                neko_plugin, _, entry = name.partition(":") if ":" in name else name.rpartition(".")
                if not neko_plugin or not entry:
                    return _failure(capability, f"no loaded plugin exposes API {name!r}", code="api_not_found", value_key="result")
                result = await self.host.call_plugin_entry(neko_plugin, entry, dict(call_args))
                if result.get("success") is False:
                    return _failure(capability, str(result.get("error") or ""), value_key="result")
                return {"success": True, "result": result}
            # 本桥已加载的插件直接走它自己的 call_api，而不是拿外来 id 去问 N.E.K.O.
            runtime = _RUNTIMES.get(str(meta["plugin_id"]))
            if runtime is None:
                return _failure(capability, f"plugin {meta['plugin_id']!r} is not loaded", code="api_not_found", value_key="result")
            value = await runtime.call_api(str(meta["name"]), dict(call_args))
            return {"success": True, "result": _jsonable(value)}
        if capability == "api.replace_dynamic":
            apis = args.get("apis")
            if not isinstance(apis, (list, tuple)):
                return _failure(capability, "apis must be a list")
            count = replace_dynamic_apis(self, [item for item in apis if isinstance(item, Mapping)])
            self.host.log("info", f"maibot 插件 {self.plugin_id} 更新了 {count} 个动态 API 的登记", source=self.source)
            return {"success": True, "count": count}
        return self._unsupported(capability)

    # --------------------------------------------------------- component.*

    async def _cap_component(self, capability: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if capability in {"component.get_all_plugins", "component.list_loaded_plugins", "component.list_registered_plugins"}:
            plugins = await self._list_plugins()
            return {"success": True, "plugins": plugins}
        if capability == "component.get_plugin_info":
            wanted = str(args.get("plugin_name") or args.get("plugin_id") or "")
            for item in await self._list_plugins():
                if wanted and wanted in {
                    str(item.get("plugin_id") or ""),
                    str(item.get("key") or ""),
                    str(item.get("name") or ""),
                }:
                    return {"success": True, "plugin": item, "value": item}
            return _failure(capability, f"unknown plugin {wanted!r}", code="plugin_not_found", value_key="plugin", value=None)
        if capability in {"component.enable", "component.disable"}:
            name = str(args.get("name") or "")
            enabled = capability == "component.enable"
            entry = self.set_component_enabled(name, enabled)
            if entry is None:
                return _failure(capability, f"plugin {self.plugin_id} has no component {name!r}", value_key="result", value=False)
            self.host.log(
                "info",
                f"maibot 插件 {self.plugin_id} 的组件 {name} 已{'启用' if enabled else '停用'}",
                source=self.source,
            )
            return {"success": True, "result": True, "enabled": enabled}
        return self._unsupported(capability)

    async def _list_plugins(self) -> list[dict[str, Any]]:
        """列出 N.E.K.O. 已加载插件，并在可用时补上桥接注册表的原生信息。"""
        items: list[dict[str, Any]] = []
        try:
            listed = await self.host.list_plugins()
        except Exception as exc:  # noqa: BLE001 — 列举失败只记录
            self.host.log("warning", f"host.list_plugins() 失败：{exc!r}", source=self.source)
            listed = []
        for item in listed or []:
            if isinstance(item, Mapping):
                items.append({str(key): _jsonable(value) for key, value in item.items()})
        registry = _bridge_registry(self.host)
        plugins = getattr(registry, "plugins", None) if registry is not None else None
        if isinstance(plugins, Mapping):
            known = {str(item.get("plugin_id") or "") for item in items}
            for key, loaded in plugins.items():
                plugin_id = getattr(loaded, "plugin_id", "")
                if not plugin_id or plugin_id in known:
                    continue
                framework = getattr(loaded, "framework", "")
                if framework != BACKEND_NAME:
                    continue
                items.append(
                    {
                        "plugin_id": plugin_id,
                        "key": str(key),
                        "name": str(getattr(getattr(loaded, "manifest", None), "display_name", plugin_id)),
                        "version": str(getattr(getattr(loaded, "manifest", None), "version", "")),
                        "framework": framework,
                    }
                )
        return items

    # --------------------------------------------------------- message.* / chat.*

    async def _recent_messages(self, limit: int) -> list[dict[str, Any]]:
        try:
            messages = await self.host.recent_messages(limit)
        except UnsupportedCapabilityError:
            raise  # chat_query 开关关闭：交给 call_capability 转成结构化 unsupported
        except Exception as exc:  # noqa: BLE001 — 消息快照失败按空列表处理（结构化告知）
            self.host.log("warning", f"host.recent_messages() 失败：{exc!r}", source=self.source)
            return []
        result: list[dict[str, Any]] = []
        for item in messages or []:
            if isinstance(item, Mapping):
                result.append({str(key): _jsonable(value) for key, value in item.items()})
        return result

    async def _cap_message(self, capability: str, args: Mapping[str, Any]) -> dict[str, Any]:
        if capability == "message.get_recent":
            limit = _as_int(args.get("limit"), 10)
            return {"success": True, "messages": await self._recent_messages(limit if limit > 0 else 10)}
        if capability == "message.get_by_id":
            message_id = str(args.get("message_id") or "") or str(args.get("chat_id") or "")
            limit = _as_int(args.get("limit"), 50)
            for item in await self._recent_messages(max(limit, 50)):
                if message_id and message_id in _message_ids(item):
                    return {"success": True, "message": item}
            return _failure(capability, f"message {message_id!r} not found in the N.E.K.O. snapshot", value_key="message", value=None)
        if capability in {"message.get_by_time", "message.get_by_time_in_chat"}:
            limit = _as_int(args.get("limit"), 50)
            messages = await self._recent_messages(max(limit, 50))
            chat_id = str(args.get("chat_id") or "")
            window = _time_window(args.get("start_time"), args.get("end_time"))
            selected = [
                item
                for item in messages
                if (not chat_id or chat_id in _message_stream_ids(item))
                and _in_time_window(item, window)
            ]
            return {"success": True, "messages": selected[:limit]}
        if capability == "message.build_readable":
            raw_messages = args.get("messages")
            if isinstance(raw_messages, Mapping):
                raw_messages = [raw_messages]
            if not isinstance(raw_messages, (list, tuple)):
                return _failure(capability, "messages must be a list", value_key="text", value="")
            lines = [_message_text(item) for item in raw_messages if isinstance(item, Mapping)]
            return {"success": True, "text": "\n".join(line for line in lines if line), "value": "\n".join(line for line in lines if line)}
        if capability == "message.count_new":
            raw_messages = await self._recent_messages(_as_int(args.get("limit"), 50))
            since = _as_optional_float(args.get("since"))
            if since is None:
                return {"success": True, "count": 0, "value": 0}
            count = len([item for item in raw_messages if (_message_timestamp(item) or 0.0) > since])
            return {"success": True, "count": count, "value": count}
        return self._unsupported(capability)

    async def _cap_chat(self, capability: str, args: Mapping[str, Any]) -> dict[str, Any]:
        messages = await self._recent_messages(_as_int(args.get("limit"), 50))
        streams = _derive_streams(messages, str(args.get("platform") or ""))
        if capability == "chat.get_all_streams":
            return {"success": True, "streams": streams, "value": streams}
        if capability == "chat.get_group_streams":
            selected = [item for item in streams if item.get("is_group")]
            return {"success": True, "streams": selected, "value": selected}
        if capability == "chat.get_private_streams":
            selected = [item for item in streams if not item.get("is_group")]
            return {"success": True, "streams": selected, "value": selected}
        if capability == "chat.get_stream_by_group_id":
            wanted = str(args.get("group_id") or "")
            for item in streams:
                if wanted and wanted in {str(item.get("group_id") or ""), str(item.get("stream_id") or "")}:
                    return {"success": True, "stream": item, "value": item}
            return _failure(capability, f"no stream for group {wanted!r}", code="stream_not_found", value_key="stream", value=None)
        if capability == "chat.get_stream_by_user_id":
            wanted = str(args.get("user_id") or "")
            for item in streams:
                if wanted and wanted in {str(item.get("user_id") or ""), str(item.get("stream_id") or "")}:
                    return {"success": True, "stream": item, "value": item}
            return _failure(capability, f"no stream for user {wanted!r}", code="stream_not_found", value_key="stream", value=None)
        return self._unsupported(capability)

    # --------------------------------------------------------- tool.*

    def _cap_tool_definitions(self) -> dict[str, Any]:
        tools = [meta for meta in list_registered_tools(self.host)]
        return {"success": True, "tools": tools, "value": tools}


# --------------------------------------------------------------------------- #
# 能力派发辅助
# --------------------------------------------------------------------------- #


def _failure(
    capability: str,
    detail: str,
    *,
    code: str = "capability_failed",
    value_key: str = "",
    value: Any = None,
) -> dict[str, Any]:
    """结构化失败结果；``value_key`` 用于让 SDK 的归一化访问器拿到中性值。"""
    payload: dict[str, Any] = {"success": False, "code": code, "error": f"{capability}: {detail}"}
    if value_key:
        payload[value_key] = value
    return payload


def _is_unsupported_capability(capability: str) -> bool:
    if capability in _UNSUPPORTED_CAPABILITIES:
        return True
    return any(capability.startswith(prefix) for prefix in _UNSUPPORTED_CAPABILITY_PREFIXES)


def _normalize_component_type(bundle: _SdkBundle, raw_type: Any) -> str:
    """用 SDK 的归一化规则解析组件类型（大写协议值）。"""
    normalizer = getattr(bundle.types_module, "normalize_component_type_name", None)
    if callable(normalizer):
        return str(normalizer(raw_type))
    value = getattr(raw_type, "value", raw_type)
    return str(value or "").strip().upper()


def _tool_description(bundle: _SdkBundle, entry: _ComponentEntry, fallback: str) -> str:
    """尽力复用 SDK 的 ``ToolComponentInfo`` 生成工具描述。"""
    info = _tool_component_info(bundle, entry)
    if info is not None:
        try:
            return str(info.get_brief_description())
        except Exception as exc:  # noqa: BLE001 — 描述生成失败退回原始字段
            logger.debug("工具 %s 描述生成失败：%r", entry.name, exc)
    return fallback or str(entry.metadata.get("brief_description") or "") or f"工具 {entry.name}"


def _tool_parameters_schema(bundle: _SdkBundle, entry: _ComponentEntry) -> dict[str, Any] | None:
    """工具参数 Schema：优先用 SDK 描述符，失败则自己从元数据构造。"""
    info = _tool_component_info(bundle, entry)
    if info is not None:
        try:
            schema = info.get_parameters_schema()
        except Exception as exc:  # noqa: BLE001
            logger.debug("工具 %s 参数 Schema 生成失败：%r", entry.name, exc)
        else:
            if isinstance(schema, Mapping):
                return dict(schema)
    return _schema_from_metadata(entry.metadata)


def _tool_component_info(bundle: _SdkBundle, entry: _ComponentEntry) -> Any | None:
    info_class = getattr(bundle.types_module, "ToolComponentInfo", None)
    if not isinstance(info_class, type):
        return None
    payload: dict[str, Any] = {
        "name": entry.name,
        "type": "TOOL",
        "description": str(entry.metadata.get("description") or ""),
    }
    for key in (
        "brief_description",
        "detailed_description",
        "parameters",
        "parameters_raw",
        "invoke_method",
        "enabled",
    ):
        if key in entry.metadata:
            payload[key] = entry.metadata[key]
    try:
        return info_class.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — 描述符不合法就退回手写路径
        logger.debug("工具 %s 的 SDK 描述符构造失败：%r", entry.name, exc)
        return None


def _schema_from_metadata(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    """手写兜底：把 ``Tool`` / ``Action`` 的参数声明转成对象级 JSON Schema。"""
    raw = metadata.get("parameters_raw")
    if isinstance(raw, Mapping) and raw:
        if raw.get("type") == "object" or "properties" in raw:
            return dict(raw)
        properties: dict[str, Any] = {}
        required: list[str] = []
        for name, schema in raw.items():
            if not isinstance(schema, Mapping):
                continue
            item = dict(schema)
            if bool(item.pop("required", False)):
                required.append(str(name))
            properties[str(name)] = item
        result: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            result["required"] = required
        return result
    declared = metadata.get("parameters")
    if isinstance(declared, Mapping) and declared:
        return {key: _jsonable(value) for key, value in declared.items()}
    if isinstance(declared, (list, tuple)) and declared:
        properties = {}
        required = []
        for item in declared:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            param_type = str(item.get("param_type") or "string").lower()
            if param_type in {"float", "number"}:
                param_type = "number"
            schema: dict[str, Any] = {"type": param_type, "description": str(item.get("description") or "")}
            if item.get("enum_values"):
                schema["enum"] = list(item["enum_values"])
            if item.get("default") is not None:
                schema["default"] = item["default"]
            properties[name] = schema
            if bool(item.get("required", True)):
                required.append(name)
        if properties:
            result = {"type": "object", "properties": properties}
            if required:
                result["required"] = required
            return result
    return None


def _llm_payload(capability: str, result: object) -> dict[str, Any]:
    """把 ``HostServices.llm_generate`` 的返回值整理成 SDK 期望的键。"""
    payload: dict[str, Any] = {
        "success": False,
        "response": "",
        "reasoning": "",
        "model": "",
        "tool_calls": [],
        "error": "",
    }
    if isinstance(result, Mapping):
        for key, value in result.items():
            payload[str(key)] = value
        payload["success"] = bool(result.get("success", False))
        payload["response"] = str(result.get("response") or "")
        payload["reasoning"] = str(result.get("reasoning") or "")
        payload["model"] = str(result.get("model") or result.get("model_name") or "")
        tool_calls = result.get("tool_calls")
        payload["tool_calls"] = list(tool_calls) if isinstance(tool_calls, (list, tuple)) else []
        payload["error"] = str(result.get("error") or "")
        if not payload["success"] and not payload["error"]:
            payload["error"] = f"{capability} 未返回成功标记"
        return payload
    payload["error"] = f"{capability} 的返回值不是字典：{type(result).__name__}"
    return payload


def _send_text_payload(capability: str, args: Mapping[str, Any]) -> tuple[str | None, bool, int]:
    """把 send.* 的载荷压成可投递文本。

    Returns:
        ``(text, degraded, dropped_segments)``；``text`` 为 ``None`` 表示没有可投递内容。
    """
    if capability == "send.text":
        text = args.get("text")
        if text is None:
            text = args.get("content")
        return (str(text) if text is not None else None), False, 0
    if capability == "send.custom":
        message_type = str(args.get("message_type") or args.get("custom_type") or "").strip().lower()
        content = args.get("content")
        if isinstance(content, str) and message_type in {"", "text", "plain", "plain_text"}:
            return content, False, 0
        return None, False, 0
    if capability == "send.hybrid":
        segments = args.get("segments")
        if not isinstance(segments, (list, tuple)):
            return None, False, 0
        return _flatten_segments(segments)
    if capability == "send.forward":
        nodes = args.get("messages")
        if not isinstance(nodes, (list, tuple)):
            return None, False, 0
        texts: list[str] = []
        dropped = 0
        for node in nodes:
            if not isinstance(node, Mapping):
                continue
            node_segments = node.get("segments")
            if isinstance(node_segments, (list, tuple)):
                text, _degraded, node_dropped = _flatten_segments(node_segments)
                dropped += node_dropped
                if text:
                    texts.append(text)
                continue
            content = node.get("content") or node.get("text")
            if isinstance(content, str) and content.strip():
                texts.append(content)
            else:
                dropped += 1
        if not texts:
            return None, False, dropped
        return "\n".join(texts), dropped > 0, dropped
    return None, False, 0


def _flatten_segments(segments: Sequence[Any]) -> tuple[str | None, bool, int]:
    texts: list[str] = []
    dropped = 0
    for segment in segments:
        if isinstance(segment, str):
            if segment.strip():
                texts.append(segment)
            continue
        if not isinstance(segment, Mapping):
            dropped += 1
            continue
        segment_type = str(segment.get("type") or "text").strip().lower()
        content = segment.get("content")
        if segment_type in {"text", "plain", "plain_text"} and isinstance(content, str):
            if content.strip():
                texts.append(content)
            continue
        dropped += 1
    if not texts:
        return None, False, dropped
    return "\n".join(texts), dropped > 0, dropped


def _canonical_event_type(event_type: str) -> str:
    """规范化事件名：接受 ``hook:<name>`` 与裸 hook 名。"""
    name = str(event_type or "").strip()
    return name


def _event_kwargs(payload: Mapping[str, Any]) -> dict[str, Any]:
    """把桥接层的事件载荷整理成 MaiBot 事件处理器常见的入参。"""
    raw = payload.get("raw") if isinstance(payload.get("raw"), Mapping) else {}
    text = str(payload.get("text") or "")
    stream_id = str(
        raw.get("stream_id") or payload.get("group_id") or payload.get("user_id") or payload.get("channel") or BACKEND_NAME
    )
    message = raw.get("message")
    if not isinstance(message, Mapping):
        message = {
            "plain_text": text,
            "raw_message": text,
            "session_id": stream_id,
            "stream_id": stream_id,
            "is_group_message": bool(payload.get("is_group", False)),
            "user_id": str(payload.get("user_id") or ""),
            "user_name": str(payload.get("user_name") or ""),
            "group_id": str(payload.get("group_id") or ""),
            "platform": str(raw.get("platform") or payload.get("channel") or BACKEND_NAME),
        }
    kwargs: dict[str, Any] = {
        "message": dict(message),
        "text": text,
        "stream_id": stream_id,
        "user_id": str(payload.get("user_id") or ""),
        "user_name": str(payload.get("user_name") or ""),
        "group_id": str(payload.get("group_id") or ""),
        "channel": str(payload.get("channel") or ""),
        "source": str(payload.get("source") or ""),
        "is_group": bool(payload.get("is_group", False)),
        "event_type": str(payload.get("event_type") or ""),
        "payload": _jsonable(payload),
    }
    return kwargs


def _handler_result_fields(raw: object) -> dict[str, Any]:
    """把处理函数的原始返回值放进 ``CommandResult.data``（保证 JSON 可序列化）。

    ``dict`` / ``list`` / ``str`` / ``int`` / ``float`` / ``bool`` / ``None`` 原样保留
    （元组与映射键先过一次 JSON 归一化变成数组/字符串），其余对象（自定义实例、
    函数、异常等）只保留 ``repr`` 字符串，避免把不可序列化的东西带进桥接层状态。
    """
    if raw is None:
        return {"handler_result": None}
    if _is_json_serializable(raw) or isinstance(raw, (Mapping, list, tuple, set, frozenset)):
        return {"handler_result": _json_like(raw)}
    return {"handler_result_repr": repr(raw)[:2000]}


def _json_like(value: object) -> Any:
    """把容器压成纯 JSON 类型（元组 -> 列表、集合 -> 列表、对象 -> 字符串）。"""
    if _is_json_serializable(value):
        return _json_normalize(value)
    if isinstance(value, Mapping):
        return {str(key): _json_like(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_like(item) for item in value]
    return _jsonable(value)


def _json_normalize(value: object) -> Any:
    """用 json 往返把值压成 JSON 原生类型（元组 -> 列表、非字符串键 -> 字符串）。"""
    return json.loads(json.dumps(value, ensure_ascii=False))


def _extract_command_result(raw: object) -> tuple[list[str], dict[str, Any]]:
    """从命令返回值里取出回复文本与结构化摘要（与官方 Runner 的解析语义一致）。"""
    replies: list[str] = []
    data: dict[str, Any] = {}

    def add_text(value: object) -> None:
        if isinstance(value, str) and value.strip():
            replies.append(value.strip())

    def visit(value: object, depth: int = 0) -> None:
        if depth > 4 or value is None:
            return
        if isinstance(value, str):
            add_text(value)
            return
        if isinstance(value, bool):
            data["success"] = value
            return
        if isinstance(value, Mapping):
            for key in ("replies", "reply", "message", "messages", "text", "content", "response", "result", "segments"):
                if key in value:
                    visit(value[key], depth + 1)
            for key in ("success", "handled", "intercept_message_level", "intercept"):
                candidate = value.get(key)
                if isinstance(candidate, (bool, int, float, str)):
                    data[key] = candidate
            if not any(key in value for key in ("replies", "reply", "message", "messages", "text", "content", "response", "result", "segments")):
                data.setdefault("result", _jsonable(value))
            return
        if isinstance(value, (list, tuple)):
            items = list(value)
            if len(items) >= 3 and isinstance(items[0], bool):
                data["success"] = items[0]
                visit(items[1], depth + 1)
                data["intercept_message_level"] = items[2]
                return
            if len(items) == 2 and isinstance(items[0], bool):
                data["success"] = items[0]
                visit(items[1], depth + 1)
                return
            for item in items:
                visit(item, depth + 1)
            return
        if isinstance(value, (int, float)):
            data["result"] = value
            return
        data.setdefault("result", _jsonable(value))

    visit(raw)
    cleaned: list[str] = []
    for item in replies:
        if not cleaned or cleaned[-1] != item:
            cleaned.append(item)
    return cleaned, data


def _result_error_text(raw: object) -> str:
    if isinstance(raw, Mapping):
        for key in ("error", "message", "content", "response"):
            candidate = raw.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    if isinstance(raw, (list, tuple)) and len(raw) >= 2 and isinstance(raw[1], str):
        return raw[1].strip()
    return ""


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


async def _maybe_await(value: object) -> Any:
    """``await`` 兼容：同步返回值直接返回。"""
    if inspect.isawaitable(value):
        return await value
    return value


def _as_int(value: object, default: int) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _as_optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _as_optional_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item) for item in value if str(item).strip())
    return ()


def _is_json_serializable(value: object) -> bool:
    """``json.dumps`` 能否直接处理这个值。"""
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def _jsonable(value: object) -> Any:
    """尽力把外部对象变成可 JSON 序列化的结构。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if _is_json_serializable(value):
        return value
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)[:2000]


def _dotted_get(data: object, key: str) -> Any:
    current: Any = data
    for part in str(key).split("."):
        if isinstance(current, Mapping) and part in current:
            current = current[part]
            continue
        return None
    return current


def _apply_order(rows: list[dict[str, Any]], order_by: object) -> list[dict[str, Any]]:
    fields = _string_tuple(order_by)
    if not fields:
        return rows
    ordered = list(rows)
    for raw_field in reversed(fields):
        descending = raw_field.startswith("-")
        field = raw_field[1:] if descending else raw_field
        ordered.sort(key=lambda row, name=field: _sort_key(row.get(name)), reverse=descending)
    return ordered


def _sort_key(value: object) -> tuple[int, Any]:
    if value is None:
        return (0, "")
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, (int, float)):
        return (1, float(value))
    return (2, str(value))


def _strip_internal(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in row.items() if not str(key).startswith("_")}


def _message_ids(item: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("id", "message_id", "msg_id", "raw_message_id"):
        value = item.get(key)
        if value is not None and str(value):
            ids.add(str(value))
    return ids


def _message_stream_ids(item: Mapping[str, Any]) -> set[str]:
    ids: set[str] = set()
    for key in ("stream_id", "session_id", "chat_id", "channel", "conversation_id"):
        value = item.get(key)
        if value is not None and str(value):
            ids.add(str(value))
    return ids


def _message_text(item: Mapping[str, Any]) -> str:
    for key in ("text", "content", "plain_text", "message", "raw_message"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _message_timestamp(item: Mapping[str, Any]) -> float | None:
    for key in ("timestamp", "time", "created_at", "ts", "updated_at"):
        value = item.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _time_window(start: object, end: object) -> tuple[float | None, float | None]:
    return _as_optional_float(start), _as_optional_float(end)


def _in_time_window(item: Mapping[str, Any], window: tuple[float | None, float | None]) -> bool:
    start, end = window
    if start is None and end is None:
        return True
    timestamp = _message_timestamp(item)
    if timestamp is None:
        return False
    if start is not None and timestamp < start:
        return False
    if end is not None and timestamp > end:
        return False
    return True


def _derive_streams(messages: Sequence[Mapping[str, Any]], platform: str) -> list[dict[str, Any]]:
    """从 N.E.K.O. 的消息快照推导聊天流（best-effort，字段缺失时用兜底值）。"""
    streams: dict[str, dict[str, Any]] = {}
    for item in messages:
        stream_id = next(iter(_message_stream_ids(item)), "")
        if not stream_id:
            continue
        info = streams.get(stream_id)
        if info is None:
            group_id = str(item.get("group_id") or "")
            user_id = str(item.get("user_id") or "")
            is_group = bool(item.get("is_group") or item.get("is_group_message") or group_id)
            info = {
                "stream_id": stream_id,
                "chat_id": stream_id,
                "platform": str(item.get("platform") or platform or BACKEND_NAME),
                "is_group": is_group,
                "group_id": group_id,
                "user_id": user_id,
                "name": str(item.get("group_name") or item.get("user_name") or ""),
                "last_message_time": _message_timestamp(item),
                "message_count": 0,
            }
            streams[stream_id] = info
        info["message_count"] = int(info["message_count"]) + 1
        timestamp = _message_timestamp(item)
        if timestamp is not None and (info["last_message_time"] is None or timestamp > float(info["last_message_time"])):
            info["last_message_time"] = timestamp
    return list(streams.values())


# --------------------------------------------------------------------------- #
# 已加载插件注册表（API / 工具索引，供插件互调与 tool.get_definitions 使用）
# --------------------------------------------------------------------------- #

_REGISTRY_LOCK = threading.RLock()
_RUNTIMES: dict[str, _PluginRuntime] = {}
_RUNTIME_BY_INSTANCE: dict[int, _PluginRuntime] = {}


def register_runtime(runtime: _PluginRuntime, loaded: LoadedPlugin) -> None:
    """把运行时登记到模块级索引（幂等）。"""
    with _REGISTRY_LOCK:
        previous = _RUNTIMES.get(runtime.plugin_id)
        if previous is not None and previous is not runtime:
            previous_host = getattr(previous, "host", None)
            if previous_host is not None:
                with contextlib.suppress(Exception):
                    previous_host.log(
                        "info",
                        f"maibot 插件 {runtime.plugin_id} 被重新加载，旧运行时已被替换",
                        source=runtime.source,
                    )
        _RUNTIMES[runtime.plugin_id] = runtime
        _RUNTIME_BY_INSTANCE[id(loaded)] = runtime


def unregister_runtime(runtime: _PluginRuntime, loaded: LoadedPlugin | None = None) -> None:
    """从模块级索引里摘掉运行时（幂等）。"""
    with _REGISTRY_LOCK:
        if _RUNTIMES.get(runtime.plugin_id) is runtime:
            _RUNTIMES.pop(runtime.plugin_id, None)
        if loaded is not None:
            _RUNTIME_BY_INSTANCE.pop(id(loaded), None)
        else:
            for key, value in list(_RUNTIME_BY_INSTANCE.items()):
                if value is runtime:
                    _RUNTIME_BY_INSTANCE.pop(key, None)


def runtime_for(plugin: LoadedPlugin) -> _PluginRuntime | None:
    """按 ``LoadedPlugin`` 找回运行时。"""
    with _REGISTRY_LOCK:
        runtime = _RUNTIME_BY_INSTANCE.get(id(plugin))
        if runtime is not None:
            return runtime
        return _RUNTIMES.get(plugin.plugin_id)


def _bridge_registry(host: HostServices) -> Any | None:
    """探测桥接注册表（``BridgeRegistry``）——存在时用它做插件/API 解析。"""
    for attribute in ("registry", "bridge_registry", "_registry"):
        candidate = getattr(host, attribute, None)
        if candidate is not None and hasattr(candidate, "plugins") and hasattr(candidate, "get"):
            return candidate
    return None


def list_registered_apis(host: HostServices) -> list[dict[str, Any]]:
    """列出当前可见的 API：MaiBot 侧已加载插件的 API + 桥接注册表里其它框架的 API。"""
    metas: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    with _REGISTRY_LOCK:
        runtimes = list(_RUNTIMES.values())
    for runtime in runtimes:
        for spec in runtime.api_specs:
            key = (runtime.plugin_id, spec.name, spec.version)
            if key in seen:
                continue
            seen.add(key)
            entry = runtime.find_api(spec.name, spec.version)
            metas.append(
                {
                    "plugin_id": runtime.plugin_id,
                    "framework": BACKEND_NAME,
                    "name": spec.name,
                    "version": spec.version,
                    "description": spec.description,
                    "entry_id": (entry.handler_name or entry.name) if entry is not None else spec.name,
                    "dynamic": bool(entry.dynamic) if entry is not None else False,
                }
            )
    registry = _bridge_registry(host)
    plugins = getattr(registry, "plugins", None) if registry is not None else None
    if isinstance(plugins, Mapping):
        for key, loaded in plugins.items():
            framework = str(getattr(loaded, "framework", ""))
            plugin_id = str(getattr(loaded, "plugin_id", ""))
            if framework == BACKEND_NAME or not plugin_id:
                continue
            for spec in getattr(loaded, "apis", ()) or ():
                name = str(getattr(spec, "name", ""))
                version = str(getattr(spec, "version", ""))
                signature = (plugin_id, name, version)
                if not name or signature in seen:
                    continue
                seen.add(signature)
                metas.append(
                    {
                        "plugin_id": plugin_id,
                        "framework": framework,
                        "name": name,
                        "version": version,
                        "description": str(getattr(spec, "description", "")),
                        "entry_id": name,
                        "dynamic": False,
                        "bridge_key": str(key),
                    }
                )
    return metas


def find_registered_api(name: str, version: str = "", *, host: HostServices | None = None) -> dict[str, Any] | None:
    """按名字（或 ``plugin_id.name`` / ``name@version``）查找一个 API。"""
    wanted = str(name or "").strip()
    if not wanted:
        return None
    metas = list_registered_apis(host) if host is not None else []
    if not metas:
        with _REGISTRY_LOCK:
            metas = [
                {
                    "plugin_id": runtime.plugin_id,
                    "framework": BACKEND_NAME,
                    "name": spec.name,
                    "version": spec.version,
                    "description": spec.description,
                    "entry_id": spec.name,
                    "dynamic": False,
                }
                for runtime in _RUNTIMES.values()
                for spec in runtime.api_specs
            ]
    base, _, inline_version = wanted.partition("@")
    provider = ""
    api_name = base
    if "." in base:
        provider, _, api_name = base.rpartition(".")
    candidates = [
        meta
        for meta in metas
        if (meta["name"] == api_name or f"{meta['plugin_id']}.{meta['name']}" == base)
        and (not provider or meta["plugin_id"].endswith(provider))
        and (not version or meta["version"] == version)
        and (not inline_version or meta["version"] == inline_version)
    ]
    if not candidates:
        return None
    return candidates[0]


def replace_dynamic_apis(runtime: _PluginRuntime, apis: Sequence[Mapping[str, Any]]) -> int:
    """登记/替换某个插件的动态 API（``api.replace_dynamic``）。"""
    entries = [entry for entry in runtime.entries if entry.kind == "API" and entry.dynamic]
    for entry in entries:
        runtime.entries.remove(entry)
        if entry.spec in runtime.api_specs:
            runtime.api_specs.remove(entry.spec)  # type: ignore[arg-type]
    count = 0
    for item in apis:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        metadata = item.get("metadata")
        info: dict[str, Any] = dict(metadata) if isinstance(metadata, Mapping) else {}
        for key, value in item.items():
            if key not in {"name", "type", "metadata"}:
                info.setdefault(key, value)
        info["dynamic"] = True
        version = str(info.get("version") or "1")
        handler_name = str(info.get("handler_name") or "")
        entry = _ComponentEntry(
            name=name,
            kind="API",
            handler_name=handler_name,
            metadata={**info, "version": version},
            dynamic=True,
            spec=ApiSpec(
                name=name,
                description=str(info.get("description") or ""),
                version=version,
                parameters=None,
            ),
        )
        runtime.entries.append(entry)
        runtime.api_specs.append(entry.spec)  # type: ignore[arg-type]
        count += 1
    runtime.refresh_native()
    return count


def list_registered_tools(host: HostServices) -> list[dict[str, Any]]:
    """收集所有已加载 MaiBot 插件的工具声明（``tool.get_definitions``）。"""
    tools: list[dict[str, Any]] = []
    with _REGISTRY_LOCK:
        runtimes = list(_RUNTIMES.values())
    for runtime in runtimes:
        for spec in runtime.tool_specs:
            entry = runtime.find_entry(spec.name, kinds=["TOOL", "ACTION"])
            if entry is not None and not entry.enabled:
                continue
            tools.append(
                {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": dict(spec.parameters) if isinstance(spec.parameters, Mapping) else spec.parameters,
                    "plugin_id": runtime.plugin_id,
                    "framework": BACKEND_NAME,
                    "origin": spec.origin,
                }
            )
    return tools


# --------------------------------------------------------------------------- #
# 模块导入 / 清单解析
# --------------------------------------------------------------------------- #


def _read_manifest(directory: Path) -> tuple[dict[str, Any] | None, str]:
    """读取 ``_manifest.json``，返回 ``(数据, 错误说明)``。"""
    path = directory / MANIFEST_FILE
    if not path.is_file():
        return None, ""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return None, f"{MANIFEST_FILE} 读取失败：{type(exc).__name__}: {exc}"
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        return None, f"{MANIFEST_FILE} 解析失败：{exc}"
    if not isinstance(parsed, dict):
        return None, f"{MANIFEST_FILE} 顶层不是 JSON 对象"
    return {str(key): value for key, value in parsed.items()}, ""


def _looks_like_maibot_source(entry: Path, limit: int = 65536) -> bool:
    """入口文件是否带 MaiBot 生态痕迹（不导入模块，只看文本）。"""
    try:
        with entry.open("r", encoding="utf-8", errors="ignore") as handle:
            head = handle.read(limit)
    except OSError as exc:
        logger.debug("读取插件入口 %s 失败：%r", entry, exc)
        return False
    return any(marker in head for marker in _MAIBOT_SOURCE_MARKERS)


def _plugin_module_name(plugin_id: str, entry: Path) -> str:
    """插件私有模块名：按插件目录名 + 入口路径命名，避免和别的插件撞车。"""
    safe = re.sub(r"[^A-Za-z0-9_]", "_", plugin_id) or "plugin"
    digest = hashlib.sha1(str(entry.resolve()).encode("utf-8")).hexdigest()[:8]
    return f"bot_bridge_maibot_{safe}_{digest}"


@contextlib.contextmanager
def _temporary_sys_path(prepend: Iterable[Path], append: Iterable[Path] = ()) -> Iterator[None]:
    """临时调整 ``sys.path``，退出时精确还原（幂等，不影响别的插件）。

    与官方 Runner 一样把插件目录的父目录放到最前（``import <插件目录>.utils``
    可用）；插件目录本身只**追加**到最后，这样 ``import utils`` 也能用，
    又不会因为插件里的 ``types.py`` / ``copy.py`` 之类文件遮蔽标准库。
    """
    inserted: list[tuple[str, bool]] = []
    for path in prepend:
        normalized = str(path)
        if normalized and normalized not in sys.path:
            sys.path.insert(0, normalized)
            inserted.append((normalized, True))
    for path in append:
        normalized = str(path)
        if normalized and normalized not in sys.path:
            sys.path.append(normalized)
            inserted.append((normalized, False))
    try:
        yield
    finally:
        for normalized, _was_prepended in inserted:
            with contextlib.suppress(ValueError):
                sys.path.remove(normalized)


_IMPORT_LOCK = threading.RLock()


def _import_plugin_module(directory: Path, entry: Path, plugin_id: str) -> Any:
    """按官方 Runner 的方式从文件路径导入插件模块。"""
    module_name = _plugin_module_name(plugin_id, entry)
    spec = importlib.util.spec_from_file_location(
        module_name,
        str(entry),
        submodule_search_locations=[str(directory)],
    )
    if spec is None or spec.loader is None:
        raise PluginLoadError(plugin_id, f"无法为 {entry} 创建模块 spec")
    with _IMPORT_LOCK:
        # 允许热加载：同名模块先摘掉，保证重新执行源码。
        sys.modules.pop(module_name, None)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            with _temporary_sys_path([directory.parent], [directory]):
                spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(module_name, None)
            raise
    return module


def _legacy_plugin_class(module: Any, bundle: _SdkBundle) -> type | None:
    """找出旧版 ``BasePlugin`` 子类（与官方 ``_try_load_legacy_plugin`` 一致）。"""
    marked = getattr(module, "_legacy_plugin_class", None)
    if isinstance(marked, type):
        return marked
    base_plugin: Any = None
    if bundle.compat_module is not None:
        base_plugin = getattr(bundle.compat_module, "BasePlugin", None)
    if not isinstance(base_plugin, type):
        return None
    for attribute in dir(module):
        candidate = getattr(module, attribute, None)
        if isinstance(candidate, type) and issubclass(candidate, base_plugin) and candidate is not base_plugin:
            return candidate
    return None


def _validate_sdk_plugin_contract(instance: Any, plugin_id: str, bundle: _SdkBundle) -> None:
    """复刻官方 Runner 的插件契约校验（``PluginLoader._validate_sdk_plugin_contract``）。

    新版 ``MaiBotPlugin`` 子类必须覆盖三个生命周期方法（基类里是
    ``NotImplementedError`` 占位），并且 ``get_config_reload_subscriptions()``
    必须可调用；否则按官方行为在加载期直接报错，而不是留到运行时静默失败。

    Raises:
        PluginLoadError: 插件违反 SDK 契约。
    """
    plugin_class = bundle.plugin_class
    if plugin_class is None or not isinstance(instance, plugin_class):
        return
    for name in ("on_load", "on_unload", "on_config_update"):
        base_implementation = getattr(plugin_class, name, None)
        if base_implementation is None:
            continue
        if getattr(type(instance), name, None) is base_implementation:
            raise PluginLoadError(plugin_id, f"插件必须实现 {name}()（SDK 基类里是 NotImplementedError 占位）")
    getter = getattr(instance, "get_config_reload_subscriptions", None)
    if callable(getter):
        try:
            getter()
        except Exception as exc:  # noqa: BLE001 — 契约校验失败要显式报出来
            raise PluginLoadError(
                plugin_id,
                f"get_config_reload_subscriptions() 不可用：{type(exc).__name__}: {exc}",
            ) from exc


def _instantiate_plugin(module: Any, plugin_id: str, bundle: _SdkBundle) -> tuple[Any, bool]:
    """实例化插件：优先 ``create_plugin()``，否则旧版兼容层。

    Returns:
        ``(实例, 是否为旧版适配器)``。

    Raises:
        PluginLoadError: 两种形态都没有找到。
    """
    factory = getattr(module, "create_plugin", None)
    if callable(factory):
        instance = factory()
        return instance, False
    legacy_class = _legacy_plugin_class(module, bundle)
    if legacy_class is None:
        raise PluginLoadError(plugin_id, "入口模块既没有 create_plugin()，也没有旧版 BasePlugin 子类")
    if not bundle.has_compat():
        raise BackendUnavailableError(BACKEND_NAME, "maibot_sdk 兼容层不可用，无法加载旧版插件")
    adapter_class = getattr(bundle.legacy_adapter_module, "LegacyPluginAdapter", None)
    if not isinstance(adapter_class, type):
        raise BackendUnavailableError(BACKEND_NAME, "maibot_sdk.compat.legacy_adapter 缺少 LegacyPluginAdapter")
    return adapter_class(legacy_class()), True


# --------------------------------------------------------------------------- #
# 兼容层
# --------------------------------------------------------------------------- #


class MaiBotBackend:
    """MaiBot 兼容层（``FrameworkBackend`` 的一个实现）。"""

    name = BACKEND_NAME
    display_name = DISPLAY_NAME
    requires: tuple[str, ...] = REQUIRES

    # ------------------------------------------------------------------ 探测

    def detect(self, path: Path) -> PluginManifest | None:
        """判断 ``path`` 是不是 MaiBot 插件目录。

        识别依据（与官方 ``PluginLoader._discover_single_candidate`` 一致）：
        **必须**同时存在清单 ``_manifest.json`` 与入口 ``plugin.py``。
        清单为 Manifest v2 时直接认可；清单为 v1 / 无版本号（旧版布局）或清单
        解析失败时，还需要入口文件里出现 MaiBot 生态痕迹才认领。
        路径不存在、不是目录、或缺少这两个文件时返回 ``None``；永不抛异常。
        """
        try:
            directory = Path(path)
            if not directory.is_dir():
                return None
            entry = directory / ENTRY_FILE
            if not entry.is_file():
                return None
            if not (directory / MANIFEST_FILE).is_file():
                return None
            manifest_data, manifest_error = _read_manifest(directory)
            manifest_version = manifest_data.get("manifest_version") if manifest_data else None
            is_sdk_v2 = manifest_version == 2
            if manifest_data is None or not is_sdk_v2:
                if not _looks_like_maibot_source(entry):
                    return None
            plugin_id = directory.name
            if not plugin_id:
                return None
            native_id = str(manifest_data.get("id") or "").strip() if manifest_data else ""
            if is_sdk_v2 and not native_id:
                manifest_error = manifest_error or "Manifest v2 缺少 id，已回落到目录名作为插件 id"
            name = str(manifest_data.get("name") or "").strip() if manifest_data else ""
            version = str(manifest_data.get("version") or "").strip() if manifest_data else ""
            description = str(manifest_data.get("description") or "").strip() if manifest_data else ""
            extra: dict[str, Any] = {
                "layout": "sdk_v2" if is_sdk_v2 else "legacy",
                "manifest": manifest_data or {},
                "manifest_version": manifest_version,
                "manifest_error": manifest_error,
                "native_id": native_id or plugin_id,
                "plugin_type": str(
                    (manifest_data or {}).get("plugin_type") or (manifest_data or {}).get("type") or ""
                ).strip(),
                "author": _jsonable((manifest_data or {}).get("author") or {}),
                "capabilities": [str(item) for item in (manifest_data or {}).get("capabilities") or [] if str(item).strip()],
                "llm_providers": [item for item in (manifest_data or {}).get("llm_providers") or [] if isinstance(item, Mapping)],
                "dependencies": [item for item in (manifest_data or {}).get("dependencies") or [] if isinstance(item, Mapping)],
                "config_file": CONFIG_FILE if (directory / CONFIG_FILE).is_file() else "",
            }
            return PluginManifest(
                path=directory.resolve(),
                plugin_id=plugin_id,
                framework=BACKEND_NAME,
                name=name or plugin_id,
                version=version,
                description=description,
                entry=ENTRY_FILE,
                extra=extra,
            )
        except Exception as exc:  # noqa: BLE001 — 探测失败按“不是本框架”处理
            logger.warning("maibot.detect(%s) 失败：%r", path, exc)
            return None

    # ------------------------------------------------------------------ 加载

    def load(self, manifest: PluginManifest, host: HostServices) -> LoadedPlugin:
        """导入并实例化 MaiBot 插件（同步）。

        Raises:
            PluginLoadError: 入口导入/实例化/组件解析失败。
            BackendUnavailableError: ``maibot_sdk`` 不可用。
        """
        plugin_id = manifest.plugin_id
        directory = Path(manifest.path)
        entry = directory / (manifest.entry or ENTRY_FILE)
        if not entry.is_file():
            raise PluginLoadError(plugin_id, f"入口文件不存在：{entry}")
        if manifest.framework and manifest.framework != BACKEND_NAME:
            raise PluginLoadError(plugin_id, f"清单声明的框架是 {manifest.framework}，不是 {BACKEND_NAME}")

        bundle = load_sdk_bundle()
        ensure_legacy_import_hook(bundle)

        try:
            module = _import_plugin_module(directory, entry, plugin_id)
        except PluginLoadError:
            raise
        except Exception as exc:  # noqa: BLE001 — 导入失败必须转成结构化错误
            raise PluginLoadError(plugin_id, f"导入入口模块失败：{type(exc).__name__}: {exc}") from exc

        try:
            instance, legacy = _instantiate_plugin(module, plugin_id, bundle)
            _validate_sdk_plugin_contract(instance, plugin_id, bundle)
        except (PluginLoadError, BackendUnavailableError):
            _drop_plugin_module(module)
            raise
        except Exception as exc:  # noqa: BLE001
            _drop_plugin_module(module)
            raise PluginLoadError(plugin_id, f"实例化插件失败：{type(exc).__name__}: {exc}") from exc

        data_dir = _safe_dir(host, "data_dir", plugin_id)
        cache_dir = _safe_dir(host, "cache_dir", plugin_id)
        runtime = _PluginRuntime(
            manifest=manifest,
            host=host,
            bundle=bundle,
            instance=instance,
            module=module,
            legacy=legacy,
            directory=directory,
            module_name=str(getattr(module, "__name__", "") or ""),
            native_id=str(manifest.extra.get("native_id") or plugin_id),
            data_dir=data_dir,
            cache_dir=cache_dir,
        )
        runtime.native = runtime._native_component_state()
        runtime.native.update({"enabled": True, "started": False, "disabled_components": [], "gateway_state": {}})

        loaded = LoadedPlugin(manifest=manifest, instance=instance, native=runtime.native)
        try:
            runtime.collect()
        except PluginLoadError:
            _drop_plugin_module(module)
            raise
        except Exception as exc:  # noqa: BLE001
            _drop_plugin_module(module)
            raise PluginLoadError(plugin_id, f"解析组件声明失败：{type(exc).__name__}: {exc}") from exc

        # 配置：先推插件目录里的 config.toml（同步），start() 时再推 N.E.K.O. 的配置。
        toml_config = runtime.read_plugin_toml()
        if toml_config:
            runtime.apply_config(toml_config, origin=CONFIG_FILE)
        else:
            runtime._apply_default_config()

        runtime.build_context()
        loaded.commands = runtime.command_specs
        loaded.tools = runtime.tool_specs
        loaded.events = _with_bridge_event_aliases(runtime.event_specs)
        loaded.apis = runtime.api_specs
        loaded.handle_message = runtime.handle_message
        loaded.call_tool = runtime.call_tool
        loaded.call_api = runtime.call_api
        loaded.dispatch_event = runtime.dispatch_event
        runtime.refresh_native()

        register_runtime(runtime, loaded)
        host.log(
            "info",
            f"maibot 插件 {plugin_id}（{runtime.native_id}，{'旧版兼容层' if legacy else 'sdk ' + bundle.version}）已加载："
            f"命令 {len(loaded.commands)} / 工具 {len(loaded.tools)} / 事件 {len(loaded.events)} / API {len(loaded.apis)}",
            source=runtime.source,
        )
        return loaded

    # ------------------------------------------------------------------ 生命周期

    async def start(self, plugin: LoadedPlugin) -> None:
        """执行插件启动生命周期（``on_load``），容忍半启动。"""
        runtime = runtime_for(plugin)
        if runtime is None:
            self._log_missing_runtime(plugin)
            return
        await runtime.push_host_config()
        runtime.config_pushed = True
        try:
            await runtime.call_lifecycle("on_load")
        except Exception as exc:  # noqa: BLE001 — 启动失败只记录，桥接层继续跑
            runtime.start_error = f"{type(exc).__name__}: {exc}"
            runtime.host.log("error", f"maibot 插件 {runtime.plugin_id} on_load() 失败：{runtime.start_error}", source=runtime.source)
        else:
            runtime.start_error = ""
        runtime.started = True
        runtime.refresh_native()

    async def stop(self, plugin: LoadedPlugin) -> None:
        """执行插件停止生命周期（``on_unload``）；半启动的插件也不许抛错。"""
        runtime = runtime_for(plugin)
        if runtime is None:
            self._log_missing_runtime(plugin)
            return
        try:
            await runtime.call_lifecycle("on_unload")
        except Exception as exc:  # noqa: BLE001 — 卸载失败只记录
            runtime.host.log("error", f"maibot 插件 {runtime.plugin_id} on_unload() 失败：{type(exc).__name__}: {exc}", source=runtime.source)
        finally:
            runtime.started = False
            runtime.refresh_native()
            unregister_runtime(runtime, plugin)
            _drop_plugin_module(runtime.module)

    # ------------------------------------------------------------------ 附加能力

    def set_enabled(self, plugin: LoadedPlugin, enabled: bool) -> bool:
        """启用/停用插件（桥接层可直接调用；停用后不再匹配命令/事件）。"""
        runtime = runtime_for(plugin)
        if runtime is None:
            return False
        return runtime.set_enabled(enabled)

    def set_component_enabled(self, plugin: LoadedPlugin, name: str, enabled: bool) -> bool:
        """启用/停用单个组件。"""
        runtime = runtime_for(plugin)
        if runtime is None:
            return False
        return runtime.set_component_enabled(name, enabled) is not None

    # ------------------------------------------------------------------ 内部

    @staticmethod
    def _log_missing_runtime(plugin: LoadedPlugin) -> None:
        logger.warning("maibot 插件 %s 没有对应的运行时（可能已被卸载）", plugin.plugin_id)


def _with_bridge_event_aliases(specs: Sequence[EventSpec]) -> list[EventSpec]:
    """给 ``on_message`` 补一条桥接层入站事件别名。

    桥接层分发的是 ``message.received``，MaiBot 的等价事件是 ``on_message``；
    两条声明都会保留，这样插件既能在 MaiBot 语境下被理解，也能真的收到入站消息。
    """
    result = list(specs)
    existing = {spec.event_type for spec in specs}
    for spec in specs:
        if spec.event_type != "on_message":
            continue
        alias = "message.received"
        if alias in existing:
            continue
        result.append(
            EventSpec(
                event_type=alias,
                description=(spec.description or "on_message") + "（桥接层入站消息别名）",
                blocking=spec.blocking,
                priority=spec.priority,
            )
        )
        existing.add(alias)
    return result


def _safe_dir(host: HostServices, attribute: str, plugin_id: str) -> Path:
    """安全地取 ``host.data_dir`` / ``host.cache_dir``（失败时回落到系统临时目录）。"""
    getter = getattr(host, attribute, None)
    if callable(getter):
        try:
            path = getter(plugin_id)
        except Exception as exc:  # noqa: BLE001 — 目录不可用也要让插件能加载
            logger.warning("host.%s(%s) 失败：%r", attribute, plugin_id, exc)
        else:
            if isinstance(path, Path):
                return path
            if path:
                return Path(str(path))
    fallback = Path(tempfile.gettempdir()) / "bot_bridge" / ("data" if attribute == "data_dir" else "cache") / plugin_id
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _drop_plugin_module(module: Any) -> None:
    """把插件模块从 ``sys.modules`` 里摘掉（幂等）。"""
    name = str(getattr(module, "__name__", "") or "")
    if name and sys.modules.get(name) is module:
        sys.modules.pop(name, None)


#: 兼容层单例（``bridge/backends/__init__.py::load_backends`` 读取本对象）。
BACKEND = MaiBotBackend()


__all__: list[str] = [
    "BACKEND",
    "BACKEND_NAME",
    "DISPLAY_NAME",
    "MaiBotBackend",
    "MaiBotCallError",
    "REQUIRES",
    "ensure_legacy_import_hook",
    "find_registered_api",
    "list_registered_apis",
    "list_registered_tools",
    "load_sdk_bundle",
    "runtime_for",
]
