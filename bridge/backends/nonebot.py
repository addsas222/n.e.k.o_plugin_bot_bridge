"""NoneBot2 兼容层（``FrameworkBackend`` 实现）。

职责：

1. **识别**：扫描候选目录 / 文件里的 Python 源码，判断它是不是 NoneBot2 插件
   （NoneBot 没有清单文件，唯一可靠的信号就是 ``import nonebot``）。
2. **加载**：把 ``bot_bridge/shims`` 放进 ``sys.path``（幂等），用
   ``importlib.util.spec_from_file_location`` 以私有模块名导入插件，并在导入期间
   收集它注册的 Matcher / Hook / ``__plugin_meta__``。
3. **映射**：把 Matcher 映射成 :class:`CommandSpec`（有文本触发的）/ :class:`ToolSpec`
   （可被 N.E.K.O. LLM 调用）/ :class:`EventSpec`（notice / request / meta_event），
   把 ``__plugin_meta__.extra["apis"]`` 里声明的模块级函数映射成 :class:`ApiSpec`。
   注意 NoneBot2 本身没有「LLM 工具」这个概念：每个事件响应器都被暴露成一个工具，
   调用时必须满足它自己的规则（规则不通过则如实返回 ``matched=False``）。
4. **桥接**：插件在事件分发中发出的文本进入 ``CommandResult.replies``（由 N.E.K.O.
   决定怎么显示）；插件在分发之外发言（后台任务）则转交 ``HostServices.send_chat``。

设计约束：``detect`` 必须便宜、无副作用、绝不抛异常；``load`` 之外的模块级状态
（``sys.path``、shim 导入）都必须幂等，允许多个插件并存。
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import importlib.util
import inspect
import re
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any

from ..contracts import (
    ApiSpec,
    CommandContext,
    CommandResult,
    CommandSpec,
    EventSpec,
    LoadedPlugin,
    PluginManifest,
    ToolSpec,
)
from ..errors import BackendUnavailableError, BridgeError, PluginLoadError, UnsupportedCapabilityError

__all__ = ["BACKEND", "NoneBotBackend"]

#: 兼容 shim 的根目录：``<plugin>/shims``（里面是 ``nonebot/`` 包）。
SHIM_ROOT = Path(__file__).resolve().parents[2] / "shims"
#: 导入插件时使用的私有模块名前缀（避免与 N.E.K.O. 自己的模块同名）。
MODULE_PREFIX = "neko_bridge_nonebot_"
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

#: fallback：源码无法解析成 AST 时用的粗匹配（只认顶格的导入语句）。
_IMPORT_RE = re.compile(r"(?m)^(?:from|import)\s+nonebot(?:\.|\s|$)")
_SHIM_LOCK = threading.RLock()
_SHIM_INSTALLED = False
_NON_ALNUM = re.compile(r"[^A-Za-z0-9_]+")

#: OneBot 的四种事件大类（桥接层接受的事件类型；v11/v12 同名）。
_EVENT_TYPES = ("message", "notice", "request", "meta_event")

#: 支持的适配器版本 -> shim 子模块名。桥接层按插件静态扫描结果二选一。
_ADAPTER_VERSIONS = ("v11", "v12")
_DEFAULT_ADAPTER = "v11"
#: 扫描插件源码时最多读多少字节（防止超大仓库拖慢加载）。
_ADAPTER_SCAN_LIMIT = 512 * 1024


def _scan_adapter_version(path: Path) -> str:
    """静态判断插件想用哪个 OneBot 适配器（必须在 import 之前决定）。

    为什么要**先**判断：不少插件在模块顶层就 `bot = nonebot.get_bot()`，那时
    Bot 的类型已经确定；等到 import 完再换就来不及了（插件手里握着旧 Bot）。

    判据按可信度排序：
      1. 源码里出现 ``adapters.onebot.v12`` 且**没有** ``adapters.onebot.v11`` → v12；
      2. 反过来 → v11；
      3. 两个都出现 / 都没出现 → v11（兼容面最大，也是历史默认值）。
    """
    target = Path(path)
    files: list[Path] = [target] if target.is_file() else sorted(target.rglob("*.py"))
    v11 = v12 = 0
    budget = _ADAPTER_SCAN_LIMIT
    for file in files:
        if "__pycache__" in file.parts or budget <= 0:
            continue
        try:
            raw = file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        budget -= len(raw)
        v11 += raw.count("adapters.onebot.v11")
        v12 += raw.count("adapters.onebot.v12")
    if v12 and not v11:
        return "v12"
    return _DEFAULT_ADAPTER


def _adapter_module(version: str) -> ModuleType:
    """按版本号取 shim 里的适配器模块。"""
    version = version if version in _ADAPTER_VERSIONS else _DEFAULT_ADAPTER
    return importlib.import_module(f"nonebot.adapters.onebot.{version}")


#: ``[bridge].nonebot_adapter`` 认的写法（大小写不敏感，允许带 ``~`` / ``onebot.`` 前缀）。
_ADAPTER_ALIASES = {
    "v11": "v11",
    "11": "v11",
    "onebot.v11": "v11",
    "~onebot.v11": "v11",
    "v12": "v12",
    "12": "v12",
    "onebot.v12": "v12",
    "~onebot.v12": "v12",
}


def _resolve_adapter_setting(value: Any) -> tuple[str | None, str]:
    """解析 ``[bridge].nonebot_adapter``。

    Returns:
        ``(version, status)``：
          * ``version`` 为 ``"v11"`` / ``"v12"`` 表示**强制**所有 NoneBot 插件用它；
          * ``None`` 表示不强制（``status`` 为 ``"auto"`` 或 ``"invalid"``）。
    """
    text = str(value or "").strip().lower()
    if not text or text in {"auto", "scan", "detect"}:
        return None, "auto"
    version = _ADAPTER_ALIASES.get(text)
    if version is None:
        return None, "invalid"
    return version, "config"


class NoneBotBackend:
    """NoneBot2 兼容层。"""

    name = "nonebot"
    display_name = "NoneBot2"
    framework = "nonebot"
    #: 兼容 shim 只用标准库，因此没有第三方依赖要求。
    requires: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._modules: dict[str, ModuleType] = {}
        self._hosts: dict[str, Any] = {}
        self._bots: dict[str, Any] = {}
        self._tools: dict[str, dict[str, type[Any]]] = {}
        self._apis: dict[str, dict[str, Any]] = {}
        # matcher 标签 -> CommandSpec.id，让 handle_message 报出的命令名与入口/工具名一致。
        self._spec_ids: dict[str, dict[str, str]] = {}
        self._enabled: dict[str, bool] = {}
        #: 插件键 -> OneBot 适配器版本（"v11" / "v12"），事件合成按它分流。
        self._adapters: dict[str, str] = {}
        #: 插件键 -> 版本来源（"scan" 源码扫描 / "config" 配置覆盖）。
        self._adapter_sources: dict[str, str] = {}
        self._shim: ModuleType | None = None

    # ------------------------------------------------------------------ 识别

    def detect(self, path: Path) -> PluginManifest | None:
        """判断 ``path`` 是不是 NoneBot2 插件；不是则返回 ``None``（绝不抛异常）。"""
        try:
            return self._detect(Path(path))
        except Exception:  # noqa: BLE001 — detect 的契约就是「便宜 + 永不抛」
            return None

    def _detect(self, path: Path) -> PluginManifest | None:
        if path.is_file():
            if path.suffix != PYTHON_SUFFIX or path.name.startswith("_"):
                return None
            if not _imports_nonebot(path):
                return None
            return self._manifest(path, path, "file")
        if not path.is_dir():
            return None
        entry = _pick_entry(path)
        if entry is None:
            return None
        if not _any_file_imports_nonebot(path, entry):
            return None
        return self._manifest(path, entry, "package")

    def _manifest(self, path: Path, entry: Path, kind: str) -> PluginManifest:
        plugin_id = _sanitize_identifier(path.stem if kind == "file" else path.name) or "nonebot_plugin"
        digest = hashlib.sha1(str(entry).encode("utf-8")).hexdigest()[:8]
        return PluginManifest(
            path=path,
            plugin_id=plugin_id,
            framework=self.name,
            entry=str(entry),
            extra={
                "kind": kind,
                "module_name": f"{MODULE_PREFIX}{plugin_id}_{digest}",
                "entry_file": entry.name,
            },
        )

    # ------------------------------------------------------------------ 加载

    def load(self, manifest: PluginManifest, host: Any) -> LoadedPlugin:
        """导入插件模块并整理出 N.E.K.O. 侧的能力描述。"""
        plugin_id = manifest.plugin_id
        self._ensure_shim(host)
        runtime = importlib.import_module("nonebot._runtime")
        shim_message = importlib.import_module("nonebot.message")

        # 适配器版本必须在 import 之前定：插件模块顶层常会 get_bot() 拿走 Bot。
        # 优先用 [bridge].nonebot_adapter 的显式覆盖，否则按插件源码扫描。
        settings = dict(getattr(host, "settings", None) or {})
        override, override_status = _resolve_adapter_setting(settings.get("nonebot_adapter"))
        if override_status == "invalid":
            host.log(
                "warning",
                f"nonebot_adapter={settings.get('nonebot_adapter')!r} 无法识别（只认 auto / v11 / v12），"
                "已回退到按插件源码扫描",
                source="nonebot",
            )
        if override is not None:
            adapter_version = override
            adapter_source = "config"
            host.log(
                "info",
                f"nonebot_adapter={override}（配置覆盖）：nonebot 插件 {plugin_id} 强制使用 OneBot {override}",
                source="nonebot",
            )
        else:
            adapter_version = _scan_adapter_version(Path(manifest.path))
            adapter_source = "scan"
        plugin_adapter = _adapter_module(adapter_version)

        bot = self._prepare_runtime(runtime, plugin_adapter, host, plugin_id)
        module_name = str(manifest.extra.get("module_name") or f"{MODULE_PREFIX}{plugin_id}")

        with runtime.collector(
            plugin_id=plugin_id, module_name=module_name, path=str(manifest.path)
        ) as record:
            module = self._import_module(manifest, module_name, host)

        runtime.attach_plugin_module(plugin_id, module)
        metadata = _read_metadata(module)
        _apply_metadata(manifest, metadata)

        with self._lock:
            self._modules[plugin_id] = module
            self._hosts[plugin_id] = host
            self._bots[plugin_id] = bot
            self._adapters[plugin_id] = adapter_version
            self._adapter_sources[plugin_id] = adapter_source
            self._enabled.setdefault(plugin_id, True)

        loaded = LoadedPlugin(
            manifest=manifest,
            instance=module,
            loaded_at=time.time(),
        )
        self._fill_specs(loaded, manifest, module, record, runtime, plugin_id, metadata)
        loaded.native = self._native(manifest, record, metadata, loaded)
        loaded.handle_message = self._make_message_handler(runtime, shim_message, plugin_id)
        loaded.call_tool = self._make_tool_handler(runtime, shim_message, plugin_id)
        loaded.call_api = self._make_api_handler(plugin_id, host)
        loaded.dispatch_event = self._make_event_handler(runtime, shim_message, plugin_adapter, plugin_id)
        return loaded

    def _ensure_shim(self, host: Any) -> ModuleType:
        """把 shim 目录放进 ``sys.path`` 并导入 ``nonebot``（幂等 + 线程安全）。"""
        global _SHIM_INSTALLED
        with _SHIM_LOCK:
            if not SHIM_ROOT.is_dir():
                raise BackendUnavailableError(self.name, f"shim directory not found: {SHIM_ROOT}")
            root = str(SHIM_ROOT)
            if root not in sys.path:
                sys.path.insert(0, root)
            shim = sys.modules.get("nonebot")
            if shim is None:
                try:
                    shim = importlib.import_module("nonebot")
                except Exception as exc:  # noqa: BLE001 — 导入失败要让宿主看到原因
                    raise BackendUnavailableError(self.name, f"cannot import the nonebot shim: {exc!r}") from exc
            if not getattr(shim, "__neko_bridge_shim__", False):
                raise BackendUnavailableError(
                    self.name,
                    "a foreign 'nonebot' module is already imported in this interpreter; "
                    "the bridge refuses to mix two implementations",
                )
            _SHIM_INSTALLED = True
            self._shim = shim
            return shim

    def _prepare_runtime(self, runtime: Any, plugin_adapter: ModuleType, host: Any, plugin_id: str) -> Any:
        """绑定驱动 / 日志 / 回复回落 / 依赖解析，并合成这个插件专属的 Bot。

        ``plugin_adapter`` 是该插件要用的 OneBot 适配器模块（v11 或 v12）——
        Bot 的类型必须与后续合成的事件一致，否则插件的 ``isinstance`` 判断会落空。
        """
        settings = dict(getattr(host, "settings", None) or {})
        shim_driver = importlib.import_module("nonebot.drivers")
        driver = shim_driver.Driver(
            {
                "command_start": list(settings.get("nonebot_command_start") or ["/"]),
                "command_sep": list(settings.get("nonebot_command_sep") or ["."]),
                "superusers": list(settings.get("nonebot_superusers") or []),
                "nickname": list(settings.get("nonebot_nickname") or []),
                "log_level": str(settings.get("nonebot_log_level") or "INFO"),
                "session_expire_timeout": float(settings.get("nonebot_session_timeout") or 120),
                "self_id": str(settings.get("nonebot_self_id") or "neko"),
            }
        )
        adapter = plugin_adapter.Adapter(driver)
        driver.register_adapter(adapter.get_name(), adapter)
        bot = plugin_adapter.Bot(adapter, str(settings.get("nonebot_self_id") or "neko"))
        bot.neko_plugin_id = plugin_id
        adapter.bot_connect(bot)

        runtime.bind_runtime(
            driver=driver,
            reply_fallback=self._make_reply_fallback(host, plugin_id),
            log_sink=self._make_log_sink(host),
            resolver=self._make_plugin_resolver(host),
        )
        runtime.set_session_bot(bot)
        return bot

    def _make_reply_fallback(self, host: Any, plugin_id: str) -> Any:
        async def _fallback(text: str, source: str) -> bool:
            try:
                return bool(
                    await host.send_chat(
                        text,
                        source=source or plugin_id or "nonebot",
                        priority=5,
                        metadata={"nonebot_out_of_run": True},
                    )
                )
            except BridgeError:
                raise
            except Exception as exc:  # noqa: BLE001 — 投递失败要如实记录，不能静默
                host.log("error", f"nonebot 后台发言投递失败：{type(exc).__name__}: {exc}", source=source or plugin_id)
                return False

        return _fallback

    def _make_log_sink(self, host: Any) -> Any:
        def _sink(level: str, message: str, source: str) -> None:
            host.log(level, message, source=f"nonebot:{source}" if source else "nonebot")

        return _sink

    def _make_plugin_resolver(self, host: Any) -> Any:
        """``nonebot.require`` 的解析器：先找本桥已加载的插件，再问宿主。"""

        def _resolve(name: str) -> ModuleType | None:
            with self._lock:
                module = self._modules.get(name)
            if module is not None:
                return module
            candidate = getattr(host, "resolve_plugin_module", None)
            if callable(candidate):
                result = candidate(name)
                if isinstance(result, ModuleType):
                    return result
            return None

        return _resolve

    def _import_module(self, manifest: PluginManifest, module_name: str, host: Any) -> ModuleType:
        path = Path(manifest.path)
        entry = Path(manifest.entry) if manifest.entry else path
        if path.is_file():
            entry = path
        if module_name in sys.modules:
            sys.modules.pop(module_name, None)
            host.log("debug", f"重新导入 NoneBot 插件模块 {module_name}", source=manifest.plugin_id)
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

    # -------------------------------------------------------------- 规格映射

    def _fill_specs(
        self,
        loaded: LoadedPlugin,
        manifest: PluginManifest,
        module: ModuleType,
        record: Any,
        runtime: Any,
        plugin_id: str,
        metadata: Any,
    ) -> None:
        tools: dict[str, type[Any]] = {}
        seen_ids: set[str] = set()
        spec_ids: dict[str, str] = {}
        for index, matcher_cls in enumerate(getattr(record, "matchers", []) or []):
            info = runtime.describe_matcher(matcher_cls)
            if info["temp"]:
                continue
            spec_id = _unique_identifier(_spec_identifier(info, index), seen_ids)
            label = str(info.get("label") or "")
            if label:
                # 命令响应器对外报命令名（与入口/工具名一致）；其余保持原生标签
                # （``regex:…`` / ``keyword:…``），比清洗后的 id 更好认。
                reported = spec_id if str(info["kind"]) == "command" else label
                spec_ids.setdefault(label, reported)
            kind = str(info["kind"])
            event_type = str(info["type"] or "")
            patterns = tuple(str(item) for item in info["patterns"])
            description = str(info["doc"] or "")
            if event_type in ("", "message"):
                if patterns:
                    loaded.commands.append(
                        CommandSpec(
                            id=spec_id,
                            name=patterns[0],
                            description=description,
                            patterns=patterns,
                            usage=_usage_from(info),
                            priority=int(info["priority"]),
                            hidden=int(info["handler_count"]) == 0,
                        )
                    )
                tool_name = _tool_name(plugin_id, spec_id)
                tools[tool_name] = matcher_cls
                loaded.tools.append(
                    ToolSpec(
                        name=tool_name,
                        description=_tool_description(description, kind, patterns),
                        parameters={
                            "type": "object",
                            "properties": {
                                "text": {
                                    "type": "string",
                                    "description": "发给这个事件响应器的消息文本（必须满足它自己的触发规则）",
                                },
                                "group_id": {
                                    "type": "string",
                                    "description": "可选的群 ID；不填则按私聊消息事件执行",
                                },
                            },
                            "required": [],
                        },
                        origin=f"{self.name}:{plugin_id}",
                    )
                )
                continue
            loaded.events.append(
                EventSpec(
                    event_type=event_type,
                    description=" ".join(part for part in (spec_id, description) if part),
                    blocking=bool(info["block"]),
                    priority=int(info["priority"]),
                )
            )

        apis: dict[str, Any] = {}
        for name in sorted(module_api_names(metadata)):
            target = getattr(module, name, None)
            if not callable(target):
                continue
            apis[name] = target
            doc = (getattr(target, "__doc__", "") or "").strip()
            loaded.apis.append(
                ApiSpec(
                    name=name,
                    description=doc.splitlines()[0] if doc else "",
                    version="",
                )
            )
        with self._lock:
            self._tools[plugin_id] = tools
            self._apis[plugin_id] = apis
            self._spec_ids[plugin_id] = spec_ids

    def _native(
        self, manifest: PluginManifest, record: Any, metadata: Any, loaded: LoadedPlugin
    ) -> dict[str, Any]:
        matchers = [
            item for item in (getattr(record, "matchers", []) or []) if not getattr(item, "temp", False)
        ]
        by_type: dict[str, int] = {}
        by_trigger: dict[str, int] = {}
        for matcher_cls in matchers:
            type_key = str(getattr(matcher_cls, "type", "") or "event")
            by_type[type_key] = by_type.get(type_key, 0) + 1
            trigger = getattr(matcher_cls, "_trigger", None)
            kind_key = str(getattr(trigger, "kind", "") or "event")
            by_trigger[kind_key] = by_trigger.get(kind_key, 0) + 1
        return {
            "framework": self.name,
            "shim": str(SHIM_ROOT),
            "shim_version": str(getattr(self._shim, "__version__", "")),
            "plugin_id": manifest.plugin_id,
            "adapter": self._adapter_version(manifest.plugin_id),
            "adapter_source": self._adapter_source(manifest.plugin_id),
            "module": str(manifest.extra.get("module_name") or ""),
            "entry": str(manifest.entry or manifest.path),
            "kind": str(manifest.extra.get("kind") or ""),
            "metadata": _metadata_summary(metadata),
            "matchers": {"total": len(matchers), "by_type": by_type, "by_trigger": by_trigger},
            "specs": {
                "commands": [item.id for item in loaded.commands],
                "tools": [item.name for item in loaded.tools],
                "events": [item.event_type for item in loaded.events],
                "apis": [item.name for item in loaded.apis],
            },
            "hooks": record.hook_count() if record is not None else {},
            "enabled": self.is_plugin_enabled(manifest.plugin_id),
            "started": False,
        }

    # ------------------------------------------------------------ 回调绑定

    def _make_message_handler(self, runtime: Any, shim_message: ModuleType, plugin_id: str) -> Any:
        bot = self._bots.get(plugin_id)
        builder = _EventBuilder(self._adapter_version(plugin_id))

        async def handle_message(ctx: CommandContext) -> CommandResult:
            result = CommandResult(plugin_id=plugin_id, framework=self.name)
            if not self.is_plugin_enabled(plugin_id):
                result.data["disabled"] = True
                return result
            event = builder.build(ctx, bot, self._runtime_settings(runtime))
            runtime.set_session_bot(bot)  # get_bot() 在本插件分发期间要指向它自己的 Bot
            try:
                report = await shim_message.handle_event(bot, event, plugin_id=plugin_id)
            except Exception as exc:  # noqa: BLE001 — 分发异常要转成结构化错误
                result.error = f"{type(exc).__name__}: {exc}"
                result.data = {"matchers": [], "errors": [result.error]}
                return result
            result.handled = bool(report.matchers)
            # 报出的命令名与投影出去的入口/工具名保持一致：能翻译就翻译成 spec id，
            # 临时响应器（会话续跑等）不在表里，保持它自己的标签。
            known = self._spec_ids.get(plugin_id, {})
            matched_ids = [known.get(str(label), str(label)) for label in report.matchers]
            result.command = matched_ids[0] if matched_ids else ""
            result.replies = list(report.replies)
            result.data = {
                "matchers": matched_ids,
                "matcher_labels": [str(label) for label in report.matchers],
                "user_id": ctx.user_id,
                "group_id": ctx.group_id,
                "is_group": ctx.is_group,
                "errors": list(report.errors),
            }
            result.error = report.errors[0] if report.errors else ""
            return result

        return handle_message

    def _make_tool_handler(self, runtime: Any, shim_message: ModuleType, plugin_id: str) -> Any:
        bot = self._bots.get(plugin_id)
        builder = _EventBuilder(self._adapter_version(plugin_id))

        async def call_tool(name: str, args: dict[str, Any]) -> Any:
            if not self.is_plugin_enabled(plugin_id):
                raise BridgeError(f"nonebot plugin '{plugin_id}' is disabled")
            with self._lock:
                matcher_cls = self._tools.get(plugin_id, {}).get(name)
            if matcher_cls is None:
                raise UnsupportedCapabilityError(
                    f"nonebot.tool.{name}", "this plugin does not expose such an event response"
                )
            text = str(args.get("text") or args.get("message") or "")
            event = builder.build(_context_from_args(args, text), bot, self._runtime_settings(runtime))
            report = await shim_message.run_matcher(matcher_cls, bot, event, plugin_id)
            return {
                "success": bool(report.matchers) and not report.errors,
                "tool": name,
                "plugin_id": plugin_id,
                "framework": self.name,
                "matched": bool(report.matchers),
                "replies": list(report.replies),
                "error": report.errors[0] if report.errors else "",
            }

        return call_tool

    def _make_api_handler(self, plugin_id: str, host: Any) -> Any:
        async def call_api(name: str, args: dict[str, Any]) -> Any:
            if not self.is_plugin_enabled(plugin_id):
                raise BridgeError(f"nonebot plugin '{plugin_id}' is disabled")
            with self._lock:
                target = self._apis.get(plugin_id, {}).get(name)
            if target is None:
                raise UnsupportedCapabilityError(
                    f"nonebot.api.{name}",
                    "the plugin did not declare this module-level function in __plugin_meta__.extra['apis']",
                )
            try:
                result = await _call_awaitable(target, args)
            except Exception as exc:  # noqa: BLE001 — 如实抛出，由桥接层转成结构化错误
                host.log("error", f"nonebot api '{name}' 调用失败：{type(exc).__name__}: {exc}", source=plugin_id)
                raise BridgeError(f"nonebot api '{name}' failed: {type(exc).__name__}: {exc}") from exc
            return {
                "success": True,
                "api": name,
                "plugin_id": plugin_id,
                "framework": self.name,
                "result": _jsonable(result),
            }

        return call_api

    def _make_event_handler(
        self, runtime: Any, shim_message: ModuleType, plugin_adapter: ModuleType, plugin_id: str
    ) -> Any:
        bot = self._bots.get(plugin_id)

        async def dispatch_event(event_type: str, payload: dict[str, Any]) -> Any:
            if not self.is_plugin_enabled(plugin_id):
                raise BridgeError(f"nonebot plugin '{plugin_id}' is disabled")
            body = dict(payload)
            raw_type = str(event_type or "").split("@", 1)[0].strip().lower()
            normalized = raw_type.split(".")[0] if raw_type else ""
            if not normalized:
                normalized = str(body.get("post_type") or "").split(".", 1)[0]
            if normalized not in _EVENT_TYPES and not body.get("post_type"):
                raise UnsupportedCapabilityError(
                    f"nonebot.event.{event_type}",
                    "only message / notice / request / meta_event payloads can be dispatched",
                )
            try:
                event = plugin_adapter.build_event(body, normalized if normalized in _EVENT_TYPES else "")
            except ValueError as exc:
                raise UnsupportedCapabilityError(f"nonebot.event.{event_type}", str(exc)) from exc
            runtime.set_session_bot(bot)  # get_bot() 在本插件分发期间要指向它自己的 Bot
            report = await shim_message.handle_event(bot, event, plugin_id=plugin_id)
            return {
                "success": not report.errors,
                "handled": bool(report.matchers),
                "plugin_id": plugin_id,
                "framework": self.name,
                "matchers": list(report.matchers),
                "replies": list(report.replies),
                "error": report.errors[0] if report.errors else "",
            }

        return dispatch_event

    def _adapter_version(self, plugin_id: str) -> str:
        """这个插件用哪个 OneBot 适配器（默认 v11）。"""
        with self._lock:
            return self._adapters.get(plugin_id, _DEFAULT_ADAPTER)

    def _adapter_source(self, plugin_id: str) -> str:
        """版本是谁定的：``scan``（源码扫描）/ ``config``（``[bridge].nonebot_adapter``）。"""
        with self._lock:
            return self._adapter_sources.get(plugin_id, "scan")

    def _runtime_settings(self, runtime: Any) -> dict[str, Any]:
        driver = runtime.driver_or_none()
        config = getattr(driver, "config", None)
        if config is None:
            return {}
        return {
            "self_id": str(getattr(config, "self_id", "") or ""),
            "nickname": sorted(getattr(config, "nickname", None) or []),
        }

    # ------------------------------------------------------------ 生命周期

    async def start(self, plugin: LoadedPlugin) -> None:
        """执行插件注册的启动钩子（``Driver.on_startup`` / ``on_bot_connect``）。

        容忍半初始化：任一钩子失败只记录到 ``native``，不影响 N.E.K.O. 启动。
        """
        await self._run_lifecycle(plugin, "start")
        plugin.started = True
        self._patch_native(plugin, {"started": True})

    async def stop(self, plugin: LoadedPlugin) -> None:
        """执行插件注册的停止钩子（``on_bot_disconnect`` / ``on_shutdown``）。"""
        await self._run_lifecycle(plugin, "stop")
        plugin.started = False
        self._patch_native(plugin, {"started": False})

    async def _run_lifecycle(self, plugin: LoadedPlugin, phase: str) -> None:
        plugin_id = plugin.plugin_id
        runtime = importlib.import_module("nonebot._runtime")
        host = self._hosts.get(plugin_id)
        bot = self._bots.get(plugin_id)
        errors: list[str] = []
        if phase == "start":
            plan = (("startup", ()), ("bot_connect", (bot,)))
        else:
            plan = (("bot_disconnect", (bot,)), ("shutdown", ()))
        for kind, args in plan:
            hooks = list(runtime.plugin_hooks(plugin_id, kind))
            if kind in ("shutdown", "bot_disconnect"):
                hooks.reverse()
            for hook in hooks:
                try:
                    if args:
                        await _call_bot_hook(hook, args[0])
                    else:
                        await _call_awaitable(hook, {})
                except Exception as exc:  # noqa: BLE001 — 钩子失败只记录，不阻断其它钩子 / 插件
                    detail = f"{kind} hook {getattr(hook, '__name__', hook)!r}: {type(exc).__name__}: {exc}"
                    errors.append(detail)
                    if host is not None:
                        host.log("error", f"nonebot {detail}", source=plugin_id)
        if errors:
            self._patch_native(plugin, {"hook_errors": errors})

    def _patch_native(self, plugin: LoadedPlugin, patch: dict[str, Any]) -> None:
        native = plugin.native if isinstance(plugin.native, dict) else {}
        errors = patch.pop("hook_errors", None)
        native.update(patch)
        bucket = native.setdefault("hook_errors", [])
        if isinstance(errors, list) and errors and isinstance(bucket, list):
            bucket.extend(errors)
        plugin.native = native

    # ------------------------------------------------------------ 启用开关

    def is_plugin_enabled(self, plugin_id: str) -> bool:
        """该外来插件当前是否启用。"""
        with self._lock:
            return self._enabled.get(plugin_id, True)

    def set_plugin_enabled(self, plugin_id: str, enabled: bool) -> None:
        """启用 / 停用一个外来插件（不卸载模块，只挡住命令 / 工具 / 事件分发）。"""
        with self._lock:
            self._enabled[plugin_id] = bool(enabled)


# --------------------------------------------------------------------------- #
# 源码探测
# --------------------------------------------------------------------------- #


def _imports_nonebot(path: Path) -> bool:
    """读取一个 Python 文件，判断它是否 ``import nonebot``。"""
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return _source_imports_nonebot(source)


def _source_imports_nonebot(source: str) -> bool:
    """源码是否在**模块级**导入了 nonebot。

    只看模块级（含 ``if`` / ``try`` / ``with`` 这些模块级分支，但不进入函数与类体）：
    插件身份由模块级导入声明，函数体里的延迟导入（例如桥接层自己的
    ``from nonebot.adapters.onebot.v11 import ...``）不算插件标识。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return bool(_IMPORT_RE.search(source))
    for node in _iter_module_level_imports(tree.body):
        if isinstance(node, ast.Import):
            if any(alias.name == "nonebot" or alias.name.startswith("nonebot.") for alias in node.names):
                return True
        else:
            module = node.module or ""
            if module == "nonebot" or module.startswith("nonebot."):
                return True
    return False


def _iter_module_level_imports(nodes: Any) -> Any:
    """遍历模块级导入语句（不进入函数 / 类体）。"""
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
        elif isinstance(node, (ast.If, ast.Try, ast.With, ast.AsyncWith)):
            for field in ("body", "orelse", "finalbody"):
                yield from _iter_module_level_imports(getattr(node, field, None) or [])
            for handler in getattr(node, "handlers", None) or []:
                yield from _iter_module_level_imports(handler.body)


def _pick_entry(path: Path) -> Path | None:
    """挑出插件包的入口文件。

    NoneBot2 从目录加载插件时只认 ``__init__.py``（``PluginManager`` 用文件夹名作为插件名，
    并要求它是一个包）；目录里散落的 ``*.py`` 是「单文件插件」，由调用方按文件路径探测。
    因此这里不接受「目录里唯一的 .py」这种布局，避免把框架分类目录误判成插件。
    """
    init_file = path / "__init__.py"
    return init_file if init_file.is_file() else None


def _any_file_imports_nonebot(path: Path, entry: Path) -> bool:
    """入口文件 + 最多 ``MAX_SCAN_DEPTH`` 层子目录里是否有 ``import nonebot``。"""
    if _imports_nonebot(entry):
        return True
    scanned = 0
    for item in _iter_plugin_python_files(path, entry):
        scanned += 1
        if scanned > MAX_SCAN_FILES:
            break
        if _imports_nonebot(item):
            return True
    return False


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


# --------------------------------------------------------------------------- #
# 清单 / 规格辅助
# --------------------------------------------------------------------------- #


def _sanitize_identifier(raw: str) -> str:
    return _NON_ALNUM.sub("_", str(raw or "").strip()).strip("_")


def _read_metadata(module: ModuleType) -> Any:
    return getattr(module, "__plugin_meta__", None)


def module_api_names(metadata: Any) -> set[str]:
    """``__plugin_meta__.extra["apis"]`` 声明的模块级函数名。"""
    if metadata is None:
        return set()
    extra = getattr(metadata, "extra", None)
    if not isinstance(extra, dict):
        return set()
    names = extra.get("apis") or ()
    if isinstance(names, str):
        names = [names]
    return {str(name).strip() for name in names if str(name).strip()}


def _metadata_summary(metadata: Any) -> dict[str, Any] | None:
    if metadata is None:
        return None
    return {
        "name": str(getattr(metadata, "name", "") or ""),
        "description": str(getattr(metadata, "description", "") or ""),
        "usage": str(getattr(metadata, "usage", "") or ""),
        "type": getattr(metadata, "type", None),
        "homepage": getattr(metadata, "homepage", None),
        "supported_adapters": sorted(getattr(metadata, "supported_adapters", None) or []),
        "extra_keys": sorted(str(key) for key in (getattr(metadata, "extra", None) or {})),
    }


def _apply_metadata(manifest: PluginManifest, metadata: Any) -> None:
    if metadata is None:
        return
    manifest.name = str(getattr(metadata, "name", "") or "")
    manifest.description = str(getattr(metadata, "description", "") or "")
    manifest.extra["usage"] = str(getattr(metadata, "usage", "") or "")
    summary = _metadata_summary(metadata)
    if summary is not None:
        manifest.extra["metadata"] = summary


def _spec_identifier(info: dict[str, Any], index: int) -> str:
    """命令/响应器的稳定入口标识（入口 id、工具名都基于它）。

    * 命令响应器 → 命令名（``hello`` / ``sfping``）；
    * 其它响应器 → ``<kind>_<首个模式>``（``keyword_sfecho``）；
    * 都没有 → ``<kind>_<index>``。

    一律做字符清洗：``register_dynamic_entry`` 的 id 与对话期工具名都要求
    ``[A-Za-z0-9_-]`` 一类的安全字符。人类可读的标签（``regex:…``）走
    ``CommandResult.command``（见 ``describe_matcher`` 的 ``label``）。
    """
    kind = str(info.get("kind") or "matcher")
    patterns = [str(item) for item in info.get("patterns") or ()]
    if kind == "command" and patterns:
        base = patterns[0]
    elif patterns:
        base = f"{kind}_{patterns[0]}"
    else:
        base = f"{kind or 'matcher'}_{index}"
    cleaned = _sanitize_identifier(base)
    return (cleaned or f"matcher_{index}")[:64]


def _unique_identifier(candidate: str, seen: set[str]) -> str:
    identifier = candidate
    suffix = 2
    while identifier in seen:
        identifier = f"{candidate}_{suffix}"
        suffix += 1
    seen.add(identifier)
    return identifier


def _usage_from(info: dict[str, Any]) -> str:
    patterns = [str(item) for item in info.get("patterns") or ()]
    if not patterns:
        return ""
    if str(info.get("kind")) == "command":
        return " / ".join(f"/{item}" for item in patterns)
    return " | ".join(patterns)


def _tool_name(plugin_id: str, spec_id: str) -> str:
    cleaned = _sanitize_identifier(f"{plugin_id}_{spec_id}")
    return cleaned[:64] or "nonebot_tool"


def _tool_description(description: str, kind: str, patterns: tuple[str, ...]) -> str:
    parts: list[str] = []
    if description:
        parts.append(description)
    parts.append(f"NoneBot2 事件响应器（{kind}）")
    if patterns:
        parts.append("触发：" + "、".join(patterns))
    return "；".join(parts)[:512]


def _context_from_args(args: dict[str, Any], text: str) -> CommandContext:
    extra = {
        str(key): value
        for key, value in args.items()
        if key not in {"text", "message", "user_id", "user_name", "group_id", "channel"}
    }
    return CommandContext(
        text=text,
        user_id=str(args.get("user_id") or "nonebot-tool"),
        user_name=str(args.get("user_name") or ""),
        group_id=str(args.get("group_id") or ""),
        channel=str(args.get("channel") or "neko"),
        source="bot_bridge",
        is_group=bool(args.get("group_id")),
        raw={"tool_call": True, **extra},
    )


class _EventBuilder:
    """把 :class:`CommandContext` 变成 OneBot 事件 —— 按插件的适配器版本分流。

    v11 与 v12 的事件形状不同（v11 靠 ``post_type``+CQ 码、v12 靠
    ``type``+``detail_type``+段数组），而插件会用 ``isinstance`` 判断事件类型，
    所以「给哪个版本的插件就合成哪个版本的事件」这件事不能含糊：给错了，
    插件的每个 ``isinstance(event, GroupMessageEvent)`` 都是 False，命令全不命中。
    """

    def __init__(self, version: str = _DEFAULT_ADAPTER) -> None:
        self.version = version if version in _ADAPTER_VERSIONS else _DEFAULT_ADAPTER

    def build(self, ctx: CommandContext, bot: Any, settings: dict[str, Any]) -> Any:
        if self.version == "v12":
            return self._build_v12(ctx, bot, settings)
        return self._build_v11(ctx, bot, settings)

    def _build_v11(self, ctx: CommandContext, bot: Any, settings: dict[str, Any]) -> Any:
        from nonebot.adapters.onebot.v11 import (
            GroupMessageEvent,
            PrivateMessageEvent,
            check_at_me,
            check_nickname,
        )

        raw = dict(ctx.raw or {})
        explicit_to_me = "to_me" in raw
        common: dict[str, Any] = {
            "self_id": str(getattr(bot, "self_id", "") or settings.get("self_id") or "neko"),
            "user_id": ctx.user_id or raw.get("user_id") or 0,
            "message": ctx.text,
            "raw_message": raw.get("raw_message", ctx.text),
            "message_id": raw.get("message_id", 0),
            "font": raw.get("font", 0),
            "to_me": bool(raw.get("to_me", False)),
            "sender": {
                "user_id": ctx.user_id,
                "nickname": ctx.user_name,
                "role": raw.get("role") or "member",
                "card": raw.get("card"),
            },
        }
        if ctx.is_group:
            event = GroupMessageEvent(
                **common,
                group_id=ctx.group_id or raw.get("group_id") or 0,
                sub_type=raw.get("sub_type") or "normal",
            )
        else:
            event = PrivateMessageEvent(**common, sub_type=raw.get("sub_type") or "friend")
        if not explicit_to_me:
            check_at_me(event)
            check_nickname(event, settings.get("nickname") or ())
        return event

    def _build_v12(self, ctx: CommandContext, bot: Any, settings: dict[str, Any]) -> Any:
        from nonebot.adapters.onebot.v12 import GroupMessageEvent, Message, PrivateMessageEvent

        raw = dict(ctx.raw or {})
        self_id = str(getattr(bot, "self_id", "") or settings.get("self_id") or "neko")
        text = str(ctx.text or "")
        detail = "group" if ctx.is_group else "private"

        to_me = raw.get("to_me")
        if to_me is None:
            if ctx.is_group:
                # v12 的 to_me 由平台给出；桥接层没有 @ 段，只能按「@机器人 / 昵称开头」判。
                mention = bool(self_id and f"@{self_id}" in text)
                nickname = any(
                    str(name) and text.startswith(str(name)) for name in (settings.get("nickname") or ())
                )
                to_me = mention or nickname
            else:
                to_me = True  # 私聊本来就是对着机器人说的

        common: dict[str, Any] = {
            "type": "message",
            "time": time.time(),
            "self_id": self_id,
            "message_id": str(raw.get("message_id", "")),
            "message": Message(text),
            "user_id": str(ctx.user_id or raw.get("user_id") or ""),
            "detail_type": detail,
            "sub_type": str(raw.get("sub_type") or ""),
            "to_me": bool(to_me),
        }
        if detail == "group":
            return GroupMessageEvent(
                **common, group_id=str(ctx.group_id or raw.get("group_id") or "")
            )
        return PrivateMessageEvent(**common)


async def _call_awaitable(target: Any, kwargs: dict[str, Any], args: tuple[Any, ...] = ()) -> Any:
    """调用同步 / 异步可调用对象，并按签名过滤 ``kwargs``。"""
    call_kwargs = _filter_kwargs(target, kwargs)
    result = target(*args, **call_kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _call_bot_hook(hook: Any, bot: Any) -> Any:
    """调用 ``on_bot_connect`` / ``on_bot_disconnect`` 钩子（优先按 ``bot=`` 关键字）。"""
    if _filter_kwargs(hook, {"bot": bot}):
        return await _call_awaitable(hook, {"bot": bot})
    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):
        return await _call_awaitable(hook, {}, (bot,))
    positional = [
        param
        for param in signature.parameters.values()
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    return await _call_awaitable(hook, {}, (bot,) if positional else ())


def _filter_kwargs(target: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    if not kwargs:
        return {}
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return dict(kwargs)
    parameters = signature.parameters
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return dict(kwargs)
    return {key: value for key, value in kwargs.items() if key in parameters}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    dumper = getattr(value, "model_dump", None) or getattr(value, "dict", None)
    if callable(dumper):
        try:
            return _jsonable(dumper())
        except Exception:  # noqa: BLE001 — 导出失败时退回 repr
            return repr(value)
    return repr(value)


BACKEND = NoneBotBackend()
"""桥接注册表读取的模块级单例。"""
