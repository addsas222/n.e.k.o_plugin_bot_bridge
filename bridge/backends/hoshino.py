"""HoshinoBot / Arcadia（``hoshino``）兼容层：把 Hoshino 插件加载进 N.E.K.O.。

HoshinoBot（Ice9Coffee/HoshinoBot）是 **nonebot v1 时代**的 QQ 群机器人框架，插件形态有两种
（上游用 ``nonebot.load_plugins('hoshino/modules')`` 递归加载）：

* **模块目录**：``hoshino/modules/<name>/``，里面 ``__init__.py`` 与/或若干 ``*.py``
  （例如 ``dice/dice.py``，或 ``priconne/{__init__.py,chara.py,…}``）；
* **单文件模块**：``hoshino/modules/<name>.py``。

本兼容层把这两种形态都当成"一个插件"：目录里所有顶层 ``*.py``（跳过 ``_`` 私有文件）都在
同一个导入上下文里按序导入，因此它们注册的 Service / 触发器都归属同一个 ``LoadedPlugin``。

插件里的 ``import hoshino.*`` / ``import aiocqhttp`` 由 ``bot_bridge/shims`` 下两个只依赖
标准库的 shim 包满足：``shims/hoshino/**``（Service / typing / util / trigger / config / priv /
log / R）与 ``shims/hoshino_support/aiocqhttp/**``（CQHttp / Event / Message / CQHttpError）。
两个 shim 共用一套运行时（``aiocqhttp._runtime``）：宿主能力面、回复收集、日志转发、
插件上下文都在那里，``hoshino._runtime`` 在其上再加 Service / 触发器 / 生命周期记录。

能力映射（真实实现）：

=========================================  ======================================================
Hoshino API                                 桥到 N.E.K.O.
=========================================  ======================================================
``Service.on_command`` / ``on_prefix`` / ``on_suffix`` / ``on_keyword`` / ``on_rex`` /
``on_fullmatch`` / ``on_message``           ``CommandSpec``（可被 ``bridge_run_command`` 触发）
上面的每一条（消息型处理器）                  ``ToolSpec`` + ``call_tool``（LLM 可调用）
``Service.on_notice`` / ``on_request``      ``EventSpec`` + ``dispatch_event``
``bot.send`` / ``bot.finish`` / ``session.send`` / ``session.finish`` / ``sv.broadcast``
                                            ``CommandResult.replies``（分发期间）或
                                            ``HostServices.send_chat``（分发之外）
``bot.send_group_msg`` / ``send_private_msg`` / ``send_msg``
                                            同上（投递到当前对话，并在结构化记录里标注目标）
``hoshino.logger`` / ``sv.logger`` / ``hoshino.log``
                                            ``HostServices.log``（source 为插件 id）
``hoshino.util.load_config`` / ``save_config``
                                            ``<host.data_dir(plugin_id)>/config/<模块>.json``
                                            （只读回落到上游位置：``<__file__ 目录>/config.json``）
``hoshino.config``                          ``[bridge]`` 段的 ``hoshino_*`` 键 + ``hoshino_config``
                                            只读快照（模块级配置对象显式报能力缺失）
``Service.scheduler`` / ``sv.scheduled_job``
                                            只登记不执行（``native["jobs"]``；登记时写 warning）
``Service.on_startup`` / ``on_shutdown``    ``start()`` / ``stop()``（shim 扩展；上游没有这两个）
=========================================  ======================================================

没有 N.E.K.O. 等价实现的能力**一律**抛 ``aiocqhttp.UnsupportedCapabilityError``
（``code="unsupported_capability"``，带 ``to_payload()``），并登记到
``LoadedPlugin.native["unsupported_calls"]``；绝不假装成功。主要清单：

* OneBot 动作：群成员 / 群列表 / 用户资料查询、撤回、禁言、加群审批、改名片、点赞、重启…
* ``hoshino.util.delete_msg`` / ``silence`` / 群成员查询（只有"当前消息发送者"可用）；
* ``Service.get_enable_groups``（拿不到群列表）、``Service.broadcast`` 只到当前对话；
* ``hoshino.R.url``（需要静态资源服务）、``RES_PROTOCOL='http'``；
* ``CommandSession.pause`` / ``switch`` / 多轮 ``get``（N.E.K.O. 的分发是一次性的）；
* ``nlp`` 自然语言会话（``on_natural_language``）、pygtrie/zhconv 带来的繁简互通。

有意的差异（不是能力缺失）：

* **私聊默认放行**：上游 ``priv.check_priv`` 对非群聊直接返回 ``False``（群聊机器人），
  N.E.K.O. 的对话基本都是私聊，照抄会让所有命令失效 —— 默认按 ``priv.PRIVATE`` 处理私聊
  发送者（``[bridge].hoshino_allow_private = false`` 可恢复上游行为）。
* **``nonebot`` v1 直连导入显式不支持**：插件若在模块级 ``import nonebot`` /
  ``from nonebot import ...`` / ``from nonebot.typing import ...``，``load()`` 会抛带解释的
  ``PluginLoadError``（shim 里的 ``nonebot`` 名字属于 NoneBot2 兼容层，两套语义混用会静默
  出错；也不做临时替换 ``sys.modules['nonebot']`` —— 桥接是并发加载的，换全局模块身份不安全）。
  写法改成 ``from hoshino.typing import ...`` 即可。
* ``Service`` 名重复不 assert（多插件并存时先到先得 + 一条 warning 日志），同一插件重复 load
  会先清掉它上一次的注册，保证幂等。
* 定时任务、繁简互通、子命令（``cmd.sub``）与 argparse 参数解析没有实现（见上）。
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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
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
from ..errors import BackendUnavailableError, BridgeError, PluginLoadError, UnsupportedCapabilityError

__all__ = ["BACKEND", "HoshinoBackend"]

#: 兼容 shim 的根目录（``hoshino`` 在这里）。
SHIM_ROOT = Path(__file__).resolve().parents[2] / "shims"
#: ``aiocqhttp`` shim 的根目录（与 ``hoshino`` 分开放，避免同名模块被导成两份）。
SUPPORT_ROOT = SHIM_ROOT / "hoshino_support"
#: 导入插件时使用的私有模块名前缀。
MODULE_PREFIX = "_neko_bot_bridge_hoshino_"
PYTHON_SUFFIX = ".py"
#: 扫描一个插件目录时最多读取的 Python 文件数（detect 要保持便宜）。
MAX_SCAN_FILES = 256
#: Hoshino / nonebot v1 风格的注册函数名（弱判据；两个及以上才认）。
_WEAK_MARKERS = frozenset(
    {
        "on_command",
        "on_fullmatch",
        "on_keyword",
        "on_message",
        "on_notice",
        "on_prefix",
        "on_request",
        "on_rex",
        "on_suffix",
        "scheduled_job",
        "message_preprocessor",
    }
)
#: 强判据：这些名字/属性只可能出现在 Hoshino（或它的 nonebot v1）插件里。
_STRONG_CALLS = frozenset({"Service"})
_STRONG_ATTRS = frozenset({"hoshino", "aiocqhttp"})
#: 源码无法解析成 AST 时用的粗匹配。
_IMPORT_RE = re.compile(r"(?m)^(?:from|import)\s+(?:hoshino|aiocqhttp)(?:\.|\s|$)")
_NON_ALNUM = re.compile(r"[^A-Za-z0-9_-]+")

#: OneBot v11 的事件大类。
_EVENT_TYPES = ("message", "notice", "request", "meta_event")

_SHIM_LOCK = threading.RLock()
_shim_cache: "_Shim | None" = None


# --------------------------------------------------------------------------- #
# shim 加载
# --------------------------------------------------------------------------- #


@dataclass
class _Shim:
    """已导入的 shim 对象集合（缓存，避免反复 getattr）。"""

    hoshino: ModuleType
    runtime: Any
    msgdispatch: Any
    service: Any
    trigger: Any
    util: Any
    config: Any
    bot_cls: type
    command_session_cls: type
    notice_session_cls: type
    request_session_cls: type
    event_cls: type
    message_cls: type
    build_event: Any
    output: Any
    unsupported_error: type


def _is_our_shim(module: object) -> bool:
    """判断 ``sys.modules['hoshino']`` / ``['aiocqhttp']`` 是不是本项目的 shim。"""
    return bool(getattr(module, "__neko_bridge_shim__", False) or getattr(module, "_NEKO_BRIDGE_SHIM", False))


def _ensure_shim() -> _Shim:
    """把两个 shim 根放进 ``sys.path`` 并导入兼容 shim（幂等、线程安全）。"""
    global _shim_cache
    with _SHIM_LOCK:
        if _shim_cache is not None:
            return _shim_cache
        for package, root in (("hoshino", SHIM_ROOT), ("aiocqhttp", SUPPORT_ROOT)):
            init_file = root / package / "__init__.py"
            if not init_file.is_file():
                raise BackendUnavailableError(
                    "hoshino",
                    f"compatibility shim not found at {init_file}",
                )
            existing = sys.modules.get(package)
            if existing is not None and not _is_our_shim(existing):
                raise BackendUnavailableError(
                    "hoshino",
                    f"another '{package}' package is already imported in this process "
                    f"({getattr(existing, '__file__', '?')}); the compatibility shim cannot take over",
                )
            root_str = str(root)
            if root_str not in sys.path:
                sys.path.insert(0, root_str)
        hoshino = importlib.import_module("hoshino")
        if not _is_our_shim(hoshino):
            raise BackendUnavailableError(
                "hoshino",
                f"imported 'hoshino' is not the bot_bridge shim: {getattr(hoshino, '__file__', '?')}",
            )
        aiocqhttp = importlib.import_module("aiocqhttp")
        if not _is_our_shim(aiocqhttp):
            raise BackendUnavailableError(
                "hoshino",
                f"imported 'aiocqhttp' is not the bot_bridge shim: {getattr(aiocqhttp, '__file__', '?')}",
            )
        importlib.import_module("hoshino.priv")  # 副作用:安装 shim 模块,供外来插件 import
        _shim_cache = _Shim(
            hoshino=hoshino,
            runtime=importlib.import_module("hoshino._runtime"),
            msgdispatch=importlib.import_module("hoshino.msgdispatch"),
            service=importlib.import_module("hoshino.service"),
            trigger=importlib.import_module("hoshino.trigger"),
            util=importlib.import_module("hoshino.util"),
            config=importlib.import_module("hoshino.config"),
            bot_cls=importlib.import_module("hoshino.bot").HoshinoBot,
            command_session_cls=importlib.import_module("hoshino.session").CommandSession,
            notice_session_cls=importlib.import_module("hoshino.session").NoticeSession,
            request_session_cls=importlib.import_module("hoshino.session").RequestSession,
            event_cls=aiocqhttp.Event,
            message_cls=aiocqhttp.Message,
            build_event=aiocqhttp.build_event,
            output=importlib.import_module("aiocqhttp._runtime"),
            unsupported_error=aiocqhttp.UnsupportedCapabilityError,
        )
        return _shim_cache


# --------------------------------------------------------------------------- #
# 宿主能力面适配（shim → HostServices）
# --------------------------------------------------------------------------- #


class _HostBridge:
    """把 N.E.K.O. 的 ``HostServices`` 适配成 shim 期望的接口。"""

    def __init__(self, host: Any) -> None:
        self._host = host

    async def send_text(
        self,
        plugin_id: str,
        text: str,
        *,
        source: str = "",
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        """把一段文本送进 N.E.K.O. 对话（分发之外的发言走这里）。"""
        payload = {"framework": "hoshino", "plugin_id": plugin_id, **dict(metadata or {})}
        return bool(
            await self._host.send_chat(
                text,
                source=source or f"hoshino:{plugin_id}" or "hoshino",
                priority=5,
                metadata=payload,
            ),
        )

    def log(self, level: str, message: str, *, plugin_id: str = "", source: str = "") -> None:
        origin = source or plugin_id or "hoshino"
        self._host.log(level, message, source=f"hoshino:{origin}" if origin != "hoshino" else "hoshino")

    def data_dir(self, plugin_id: str) -> Path:
        return Path(self._host.data_dir(plugin_id))

    async def load_plugin_config(self, plugin_id: str) -> Mapping[str, Any]:
        return dict(await self._host.plugin_config(plugin_id))

    async def save_plugin_config(self, plugin_id: str, data: Mapping[str, Any]) -> bool:
        return bool(await self._host.save_plugin_config(plugin_id, dict(data)))


# --------------------------------------------------------------------------- #
# 兼容层
# --------------------------------------------------------------------------- #


class HoshinoBackend:
    """HoshinoBot / Arcadia 兼容层。"""

    name = "hoshino"
    display_name = "HoshinoBot (Arcadia)"
    framework = "hoshino"
    #: 兼容 shim 只用标准库，因此没有第三方依赖要求。
    requires: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._modules: dict[str, ModuleType] = {}
        self._module_names: dict[str, list[str]] = {}
        self._bots: dict[str, Any] = {}
        self._hosts: dict[str, Any] = {}
        self._targets: dict[str, dict[str, tuple[str, Any]]] = {}
        self._spec_ids: dict[str, dict[str, str]] = {}
        self._enabled: dict[str, bool] = {}
        self._shim: _Shim | None = None

    # ------------------------------------------------------------------ 识别

    def detect(self, path: Path) -> PluginManifest | None:
        """判断 ``path`` 是不是 Hoshino 插件；不是则返回 ``None``（绝不抛异常）。"""
        try:
            return self._detect(Path(path))
        except Exception:  # noqa: BLE001 — detect 的契约就是「便宜 + 永不抛」
            return None

    def _detect(self, path: Path) -> PluginManifest | None:
        if path.is_file():
            if path.suffix != PYTHON_SUFFIX or path.name.startswith("_"):
                return None
            source = _read_source(path)
            if source is None or not _source_is_hoshino(source):
                return None
            return self._manifest(path, path, "file", source)
        if not path.is_dir():
            return None
        entry, sources = _pick_dir_entry(path)
        if entry is None:
            return None
        return self._manifest(path, entry, "package", sources.get(entry, ""))

    def _manifest(self, path: Path, entry: Path, kind: str, entry_source: str) -> PluginManifest:
        plugin_id = _sanitize_identifier(path.stem if kind == "file" else path.name) or "hoshino_plugin"
        digest = hashlib.sha1(str(entry).encode("utf-8")).hexdigest()[:8]
        services = _scan_services(entry_source)
        first = services[0] if services else {}
        manifest = PluginManifest(
            path=path,
            plugin_id=plugin_id,
            framework=self.name,
            entry=str(entry),
            name=str(first.get("name") or plugin_id),
            description=_first_line(str(first.get("help_") or first.get("help") or "")),
            extra={
                "kind": kind,
                "module_name": f"{MODULE_PREFIX}{plugin_id}_{digest}",
                "entry_file": entry.name,
                "services": services,
                "imports_nonebot_v1": _module_level_nonebot_imports(entry_source),
                "imports_nonebot_v2": _module_level_nonebot_v2_imports(entry_source),
                "features": _features_of(entry_source),
            },
        )
        return manifest

    # ------------------------------------------------------------------ 加载

    def load(self, manifest: PluginManifest, host: Any) -> LoadedPlugin:
        """导入插件模块并整理出 N.E.K.O. 侧的能力描述。"""
        shim = self._ensure_shim()
        plugin_id = manifest.plugin_id
        module_name = str(manifest.extra.get("module_name") or f"{MODULE_PREFIX}{plugin_id}")
        self._reject_v1_nonebot(manifest)

        settings = self._settings(host)
        shim.runtime.bind_runtime(host=_HostBridge(host), settings=settings)
        bot = shim.bot_cls(
            plugin_id=plugin_id,
            self_id=str(settings.get("self_id") or "neko"),
            api_clients={str(settings.get("self_id") or "neko"): None},
            _internal=True,
        )
        shim.output.RUNTIME.bind_bot(plugin_id, bot)

        with shim.runtime.collector(plugin_id, module_name=module_name, path=str(manifest.path)) as record:
            module, import_errors = self._import_plugin(manifest, module_name, host)
        shim.runtime.attach_plugin_module(plugin_id, module)

        with self._lock:
            self._modules[plugin_id] = module
            self._module_names[plugin_id] = import_errors["modules"]
            self._hosts[plugin_id] = host
            self._bots[plugin_id] = bot
            self._enabled.setdefault(plugin_id, True)

        _apply_manifest_metadata(manifest, record, shim)
        loaded = LoadedPlugin(manifest=manifest, instance=module, loaded_at=time.time())
        self._fill_specs(loaded, manifest, record, shim, plugin_id)
        loaded.native = self._native(manifest, record, shim, loaded, import_errors)
        loaded.handle_message = self._make_message_handler(shim, plugin_id)
        loaded.call_tool = self._make_tool_handler(shim, plugin_id)
        loaded.dispatch_event = self._make_event_handler(shim, plugin_id)
        # Hoshino 没有"插件对外 API"的概念：兼容层不伪造一个，apis 留空、call_api 不绑定。
        return loaded

    def _ensure_shim(self) -> _Shim:
        """导入（并缓存）shim；宿主能力面在 ``bind_runtime`` 里按插件注入。"""
        shim = _ensure_shim()
        self._shim = shim
        return shim

    def _reject_v1_nonebot(self, manifest: PluginManifest) -> None:
        """插件直接 import nonebot v1 时给出结构化、可操作的错误。"""
        hits: list[str] = []
        v2 = False
        for source_file in _plugin_python_files(manifest):
            source = _read_source(source_file)
            if source is None:
                continue
            if _module_level_nonebot_v2_imports(source):
                v2 = True
                hits.append(source_file.name)
            elif _module_level_nonebot_imports(source):
                hits.append(source_file.name)
        if not hits:
            return
        if v2:
            raise PluginLoadError(
                manifest.plugin_id,
                f"{', '.join(sorted(set(hits)))} 在模块级 import 了 nonebot.adapters（NoneBot2 语义）；"
                "这看起来是 NoneBot2 插件 —— 请把插件装到 NoneBot2 兼容层（framework=nonebot）下",
            )
        raise PluginLoadError(
            manifest.plugin_id,
            f"{', '.join(sorted(set(hits)))} 在模块级 import 了 nonebot（v1 API，Hoshino 的底座）；"
            "本兼容层只提供 hoshino.* / aiocqhttp.* 两个 shim（『nonebot』这个名字归 NoneBot2 兼容层，"
            "两套语义混用会静默出错，因此不做临时替换）；"
            "把导入改成 `from hoshino.typing import ...` / `from hoshino import ...` 即可加载",
        )

    def _import_plugin(self, manifest: PluginManifest, module_name: str, host: Any) -> tuple[ModuleType, dict[str, Any]]:
        """导入插件的入口模块（目录插件还会把顶层兄弟模块一起导入，与上游一致）。"""
        path = Path(manifest.path)
        entry = Path(manifest.entry) if manifest.entry else path
        if path.is_file():
            entry = path
        imported: list[str] = []
        errors: list[str] = []
        self._forget_modules(module_name)
        if path.is_dir() and entry.name == "__init__.py":
            # 有 __init__.py：按包导入，兄弟模块作为它的子模块（相对导入也因此可用）。
            module = self._exec_module(module_name, entry, submodule_search=[str(path)], plugin_id=manifest.plugin_id)
            imported.append(module.__name__)
            for sibling in _top_level_modules(path, skip={entry}):
                name = f"{module_name}.{sibling.stem}"
                try:
                    self._exec_module(name, sibling, plugin_id=manifest.plugin_id)
                except BridgeError as exc:
                    errors.append(str(exc))
                    host.log(
                        "error",
                        f"hoshino 插件 {manifest.plugin_id} 的 {sibling.name} 导入失败：{exc}",
                        source=manifest.plugin_id,
                    )
                    continue
                imported.append(name)
            return module, {"modules": imported, "errors": errors}
        if path.is_dir():
            # 没有 __init__.py：上游把目录里的每个 *.py 都当独立模块加载（例如 dice/dice.py），
            # 这里逐个导入；入口文件用主模块名，其余加文件后缀（都与上游一样没有包关系）。
            module = self._exec_module(module_name, entry, plugin_id=manifest.plugin_id)
            imported.append(module.__name__)
            for sibling in _top_level_modules(path, skip={entry}):
                name = f"{module_name}_{_sanitize_identifier(sibling.stem)}"
                try:
                    self._exec_module(name, sibling, plugin_id=manifest.plugin_id)
                except BridgeError as exc:
                    errors.append(str(exc))
                    host.log(
                        "error",
                        f"hoshino 插件 {manifest.plugin_id} 的 {sibling.name} 导入失败：{exc}",
                        source=manifest.plugin_id,
                    )
                    continue
                imported.append(name)
            return module, {"modules": imported, "errors": errors}
        module = self._exec_module(module_name, entry, plugin_id=manifest.plugin_id)
        imported.append(module.__name__)
        return module, {"modules": imported, "errors": errors}

    def _exec_module(
        self,
        module_name: str,
        file: Path,
        *,
        submodule_search: list[str] | None = None,
        plugin_id: str = "",
    ) -> ModuleType:
        sys.modules.pop(module_name, None)
        try:
            if submodule_search is not None:
                spec = importlib.util.spec_from_file_location(module_name, file, submodule_search_locations=submodule_search)
            else:
                spec = importlib.util.spec_from_file_location(module_name, file)
            if spec is None or spec.loader is None:
                raise PluginLoadError(plugin_id, f"cannot build an import spec for {file}")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        except PluginLoadError:
            sys.modules.pop(module_name, None)
            raise
        except BaseException as exc:  # noqa: BLE001 — 导入期异常要转成结构化错误
            sys.modules.pop(module_name, None)
            raise PluginLoadError(plugin_id or module_name, f"{type(exc).__name__}: {exc}") from exc
        return module

    def _forget_modules(self, module_name: str) -> None:
        """清掉上一次导入留下的模块（重复 load 幂等）。

        一个插件的所有模块共享同一个前缀（``<module_name>`` 及其 ``.`` / ``_`` 派生名），
        因此按前缀清理即可。
        """
        for name in list(sys.modules):
            if name == module_name or name.startswith(f"{module_name}.") or name.startswith(f"{module_name}_"):
                sys.modules.pop(name, None)

    # -------------------------------------------------------------- 设置 / Bot

    def _settings(self, host: Any) -> dict[str, Any]:
        """把桥接配置翻译成 shim 的运行期设置。"""
        raw = dict(getattr(host, "settings", None) or {})
        settings: dict[str, Any] = {
            "self_id": str(raw.get("hoshino_self_id") or raw.get("self_id") or "neko"),
            "allow_private": bool(raw.get("hoshino_allow_private", True)),
            "debug": bool(raw.get("hoshino_debug", True)),
        }
        for key in (
            "hoshino_command_start",
            "hoshino_command_sep",
            "hoshino_superusers",
            "hoshino_nickname",
            "hoshino_black_list",
            "hoshino_white_list",
            "hoshino_modules_on",
            "hoshino_res_dir",
            "hoshino_res_url",
            "hoshino_res_protocol",
            "hoshino_config",
        ):
            if key in raw:
                settings[key] = raw[key]
        if "hoshino_superusers" not in settings and raw.get("superusers"):
            settings["hoshino_superusers"] = raw["superusers"]
        return settings

    # -------------------------------------------------------------- 规格映射

    def _fill_specs(
        self,
        loaded: LoadedPlugin,
        manifest: PluginManifest,
        record: Any,
        shim: _Shim,
        plugin_id: str,
    ) -> None:
        seen: set[str] = set()
        spec_ids: dict[str, str] = {}
        targets: dict[str, tuple[str, Any]] = {}
        services = {item.name: item for item in shim.runtime.plugin_services(plugin_id)}
        default_help = _first_line(next((item.help for item in services.values() if item.help), ""))

        def add_tool(spec_id: str, description: str, usage: str) -> None:
            """每个消息型处理器都暴露成一个 LLM 工具（Hoshino 没有工具概念，这是显式映射）。"""
            loaded.tools.append(
                ToolSpec(
                    name=_tool_name(plugin_id, spec_id),
                    description=_tool_description(description, usage),
                    parameters={
                        "type": "object",
                        "properties": {
                            "text": {
                                "type": "string",
                                "description": "发给这个处理器的完整消息文本（必须满足它自己的触发条件）",
                            },
                            "group_id": {"type": "string", "description": "可选的群 ID；不填则按私聊消息执行"},
                            "user_id": {"type": "string", "description": "可选的发送者 ID"},
                        },
                        "required": [],
                    },
                    origin=f"{self.name}:{plugin_id}",
                ),
            )

        for index, registration in enumerate(shim.runtime.plugin_commands(plugin_id)):
            spec_id = _spec_identifier("command", registration.name, index, seen)
            spec_ids[f"command:{registration.name}"] = spec_id
            service = services.get(str(getattr(registration.service, "name", "")))
            description = registration.description or (service.help if service else "") or default_help
            usage = _usage_for_command(registration)
            loaded.commands.append(
                CommandSpec(
                    id=spec_id,
                    name=registration.name,
                    description=description,
                    patterns=registration.patterns,
                    usage=usage,
                    aliases=tuple(registration.aliases),
                    priority=_priority_of(service),
                    parameters=_command_parameters(registration),
                ),
            )
            targets[_tool_name(plugin_id, spec_id)] = ("command", registration)
            add_tool(spec_id, f"{description}（Hoshino 命令）".strip("（）"), usage)

        for index, registration in enumerate(shim.runtime.plugin_triggers(plugin_id)):
            label = str(registration.label)
            spec_id = _spec_identifier(registration.kind, label, index, seen)
            spec_ids[f"{registration.kind}:{label}"] = spec_id
            service = services.get(str(getattr(registration.service, "name", "")))
            description = registration.description or (service.help if service else "") or default_help
            usage = f"{registration.kind}：{label}"
            loaded.commands.append(
                CommandSpec(
                    id=spec_id,
                    name=label,
                    description=description,
                    patterns=(label,) if label else (),
                    usage=usage,
                    priority=_priority_of(service),
                ),
            )
            targets[_tool_name(plugin_id, spec_id)] = ("trigger", registration)
            add_tool(spec_id, f"{description}（Hoshino {registration.kind} 触发器）".strip("（）"), usage)

        for index, registration in enumerate(shim.runtime.plugin_sessions(plugin_id, "message")):
            label = str(registration.label)
            spec_id = _spec_identifier("message", label, index, seen)
            spec_ids[f"message:{label}"] = spec_id
            service = services.get(str(getattr(registration.service, "name", "")))
            description = registration.description or default_help
            usage = f"消息监听：{label}"
            loaded.commands.append(
                CommandSpec(
                    id=spec_id,
                    name=f"on_message({label})",
                    description=description,
                    # 消息监听没有确定触发词：给空 patterns，让它成为"什么都收"的兜底项
                    # （路由层按 specificity 排序，精确命令优先）。
                    patterns=(),
                    usage=usage,
                    priority=_priority_of(service),
                ),
            )
            targets[_tool_name(plugin_id, spec_id)] = ("session", registration)
            add_tool(spec_id, f"{description}（Hoshino on_message 监听）".strip("（）"), usage)

        for registration in shim.runtime.plugin_sessions(plugin_id, "notice"):
            loaded.events.append(
                EventSpec(
                    event_type=_event_name("notice", registration.events),
                    description=f"{registration.label}：{registration.description}".strip("："),
                    priority=_priority_of(services.get(str(getattr(registration.service, "name", "")))),
                ),
            )
        for registration in shim.runtime.plugin_sessions(plugin_id, "request"):
            loaded.events.append(
                EventSpec(
                    event_type=_event_name("request", registration.events),
                    description=f"{registration.label}：{registration.description}".strip("："),
                    priority=_priority_of(services.get(str(getattr(registration.service, "name", "")))),
                ),
            )

        with self._lock:
            self._targets[plugin_id] = targets
            self._spec_ids[plugin_id] = spec_ids

    def _native(
        self,
        manifest: PluginManifest,
        record: Any,
        shim: _Shim,
        loaded: LoadedPlugin,
        import_errors: dict[str, Any],
    ) -> dict[str, Any]:
        unsupported = shim.output.RUNTIME.register_unsupported_sink(manifest.plugin_id)
        return {
            "framework": self.name,
            "shim": str(SHIM_ROOT),
            "support_shim": str(SUPPORT_ROOT),
            "shim_version": str(getattr(shim.hoshino, "__version__", "")),
            "plugin_id": manifest.plugin_id,
            "module": str(manifest.extra.get("module_name") or ""),
            "modules": list(import_errors.get("modules") or []),
            "entry": str(manifest.entry or manifest.path),
            "kind": str(manifest.extra.get("kind") or ""),
            "services": [
                {
                    "name": item.name,
                    "help": item.help,
                    "use_priv": item.use_priv,
                    "manage_priv": item.manage_priv,
                    "enable_on_default": item.enable_on_default,
                    "visible": item.visible,
                    "bundle": item.bundle,
                }
                for item in shim.runtime.plugin_services(manifest.plugin_id)
            ],
            "detected_services": list(manifest.extra.get("services") or []),
            "jobs": [item.summary() for item in shim.runtime.plugin_jobs(manifest.plugin_id)],
            "hooks": record.hook_count(),
            "specs": {
                "commands": [item.id for item in loaded.commands],
                "tools": [item.name for item in loaded.tools],
                "events": [item.event_type for item in loaded.events],
            },
            "import_errors": list(import_errors.get("errors") or []),
            "unsupported_calls": unsupported,
            "config": shim.runtime.config_snapshot(),
            "settings": shim.runtime.settings(),
            "enabled": self.is_plugin_enabled(manifest.plugin_id),
            "started": False,
        }

    # ------------------------------------------------------------ 回调绑定

    def _make_message_handler(self, shim: _Shim, plugin_id: str) -> Any:
        bot = self._bots.get(plugin_id)
        builder = _EventBuilder(shim)

        async def handle_message(ctx: CommandContext) -> CommandResult:
            result = CommandResult(plugin_id=plugin_id, framework=self.name)
            if not self.is_plugin_enabled(plugin_id):
                result.data["disabled"] = True
                return result
            event = builder.build(ctx, bot, self._runtime_settings(shim))
            try:
                report = await shim.msgdispatch.handle_message(bot, event, plugin_id)
            except BaseException as exc:  # noqa: BLE001 — 分发异常要转成结构化错误
                result.error = f"{type(exc).__name__}: {exc}"
                result.data = {"handlers": [], "errors": [result.error]}
                return result
            known = self._spec_ids.get(plugin_id, {})
            matched_ids = [known.get(str(key), str(key)) for key in report.handlers]
            result.handled = report.handled
            result.command = matched_ids[0] if matched_ids else ""
            result.replies = list(report.replies)
            result.error = report.errors[0] if report.errors else ""
            result.data = {
                "handlers": matched_ids,
                "handler_keys": list(report.handlers),
                "skipped": list(report.skipped),
                "finished": report.finished,
                "matched_text": report.matched_text,
                "user_id": ctx.user_id,
                "group_id": ctx.group_id,
                "is_group": ctx.is_group,
                "output": list(report.extras),
                "errors": list(report.errors),
            }
            return result

        return handle_message

    def _make_tool_handler(self, shim: _Shim, plugin_id: str) -> Any:
        bot = self._bots.get(plugin_id)
        builder = _EventBuilder(shim)

        async def call_tool(name: str, args: dict[str, Any]) -> Any:
            if not self.is_plugin_enabled(plugin_id):
                raise BridgeError(f"hoshino plugin '{plugin_id}' is disabled")
            with self._lock:
                target = self._targets.get(plugin_id, {}).get(name)
            if target is None:
                raise UnsupportedCapabilityError(
                    f"hoshino.tool.{name}",
                    "this plugin does not expose such a handler",
                )
            kind, registration = target
            text = str(args.get("text") or args.get("message") or "")
            event = builder.build(_context_from_args(args, text), bot, self._runtime_settings(shim))
            if kind == "command":
                report = await shim.msgdispatch.run_command(bot, event, registration, plugin_id)
            elif kind == "trigger":
                report = await shim.msgdispatch.run_trigger(bot, event, registration, plugin_id)
            else:
                report = await shim.msgdispatch.run_session(bot, event, registration, plugin_id)
            return {
                "success": bool(report.handlers) and not report.errors,
                "tool": name,
                "plugin_id": plugin_id,
                "framework": self.name,
                "matched": bool(report.handlers),
                "handlers": list(report.handlers),
                "skipped": list(report.skipped),
                "replies": list(report.replies),
                "output": list(report.extras),
                "error": report.errors[0] if report.errors else "",
            }

        return call_tool

    def _make_event_handler(self, shim: _Shim, plugin_id: str) -> Any:
        bot = self._bots.get(plugin_id)

        async def dispatch_event(event_type: str, payload: dict[str, Any]) -> Any:
            if not self.is_plugin_enabled(plugin_id):
                raise BridgeError(f"hoshino plugin '{plugin_id}' is disabled")
            body = dict(payload)
            raw_type = str(event_type or "").split("@", 1)[0].strip().lower()
            normalized = raw_type.split(".")[0] if raw_type else ""
            if normalized not in _EVENT_TYPES:
                normalized = str(body.get("post_type") or "").split(".", 1)[0]
            if normalized not in _EVENT_TYPES:
                raise UnsupportedCapabilityError(
                    f"hoshino.event.{event_type}",
                    "only message / notice / request / meta_event payloads can be dispatched",
                )
            try:
                event = shim.build_event(body)
            except ValueError as exc:
                raise UnsupportedCapabilityError(f"hoshino.event.{event_type}", str(exc)) from exc
            report = await shim.msgdispatch.handle_event(bot, event, plugin_id)
            return {
                "success": not report.errors,
                "handled": report.handled,
                "plugin_id": plugin_id,
                "framework": self.name,
                "handlers": list(report.handlers),
                "replies": list(report.replies),
                "skipped": list(report.skipped),
                "error": report.errors[0] if report.errors else "",
            }

        return dispatch_event

    def _runtime_settings(self, shim: _Shim) -> dict[str, Any]:
        values = shim.runtime.settings()
        return {
            "self_id": str(values.get("self_id") or "neko"),
            "command_start": tuple(values.get("command_start") or ("/",)),
            "nickname": tuple(values.get("nickname") or ()),
        }

    # ------------------------------------------------------------ 生命周期

    async def start(self, plugin: LoadedPlugin) -> None:
        """执行插件的启动钩子（``@sv.on_startup`` / ``sv.on_startup``）。"""
        errors = await self._run_hooks(plugin, "startup")
        plugin.started = True
        self._patch_native(plugin, {"started": True, "hook_errors": errors})

    async def stop(self, plugin: LoadedPlugin) -> None:
        """执行停止钩子（逆序）；容忍半启动的插件。"""
        errors = await self._run_hooks(plugin, "shutdown", reverse=True)
        plugin.started = False
        self._patch_native(plugin, {"started": False, "hook_errors": errors})

    async def _run_hooks(self, plugin: LoadedPlugin, kind: str, *, reverse: bool = False) -> list[str]:
        shim = self._shim or _ensure_shim()
        plugin_id = plugin.plugin_id
        host = self._hosts.get(plugin_id)
        bot = self._bots.get(plugin_id)
        hooks = list(shim.runtime.plugin_hooks(plugin_id, kind))
        if reverse:
            hooks.reverse()
        errors: list[str] = []
        for hook in hooks:
            try:
                await _call_hook(hook, bot)
            except Exception as exc:  # noqa: BLE001 — 钩子失败只记录，不阻断其它钩子 / 插件
                detail = f"{kind} hook {getattr(hook, '__name__', hook)!r}: {type(exc).__name__}: {exc}"
                errors.append(detail)
                if host is not None:
                    host.log("error", f"hoshino {detail}", source=plugin_id)
        return errors

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
# 事件构造
# --------------------------------------------------------------------------- #


class _EventBuilder:
    """把 :class:`CommandContext` 变成 OneBot v11 事件（等价于真实适配器的上报解析）。"""

    def __init__(self, shim: _Shim) -> None:
        self._shim = shim

    def build(self, ctx: CommandContext, bot: Any, settings: Mapping[str, Any]) -> Any:
        message = self._shim.message_cls(ctx.text or "")
        self_id = str(getattr(bot, "self_id", "") or settings.get("self_id") or "neko")
        raw = dict(ctx.raw or {})
        body: dict[str, Any] = {
            "post_type": "message",
            "message_type": "group" if ctx.is_group else "private",
            "sub_type": raw.get("sub_type") or ("normal" if ctx.is_group else "friend"),
            "self_id": raw.get("self_id") or self_id,
            "user_id": ctx.user_id or raw.get("user_id") or 0,
            "group_id": ctx.group_id or raw.get("group_id") or 0,
            "message_id": raw.get("message_id") or 0,
            "message": message,
            "raw_message": raw.get("raw_message", ctx.text or ""),
            "font": raw.get("font") or 0,
            "sender": {
                "user_id": ctx.user_id,
                "nickname": ctx.user_name,
                "card": raw.get("card") or "",
                "role": raw.get("role") or ("member" if ctx.is_group else ""),
                "sex": raw.get("sex") or "unknown",
                "age": raw.get("age") or 0,
                "area": raw.get("area") or "",
            },
            "anonymous": raw.get("anonymous"),
            "to_me": self._to_me(ctx, raw, message, self_id, settings),
            "time": raw.get("time") or 0,
        }
        return self._shim.build_event(body)

    def _to_me(
        self,
        ctx: CommandContext,
        raw: Mapping[str, Any],
        message: Any,
        self_id: str,
        settings: Mapping[str, Any],
    ) -> bool:
        """这条消息是不是冲着机器人来的。

        优先级：``raw`` 里显式的 ``to_me`` / ``is_at_bot`` → 私聊（总是唤醒）→ 群聊里
        ``[CQ:at,qq=bot]`` 或消息里出现机器人昵称。
        """
        for key in ("to_me", "is_at_bot", "at_bot"):
            if key in raw:
                return bool(raw[key])
        if not ctx.is_group:
            return True
        for segment in message:
            if segment.type == "at" and str(segment.data.get("qq") or "") == self_id:
                return True
        nicknames = [str(item) for item in (settings.get("nickname") or ()) if str(item)]
        text = message.extract_plain_text()
        return any(item in text for item in nicknames)


def _context_from_args(args: dict[str, Any], text: str) -> CommandContext:
    extra = {
        str(key): value
        for key, value in args.items()
        if key not in {"text", "message", "user_id", "user_name", "group_id", "channel"}
    }
    return CommandContext(
        text=text,
        user_id=str(args.get("user_id") or "hoshino-tool"),
        user_name=str(args.get("user_name") or ""),
        group_id=str(args.get("group_id") or ""),
        channel=str(args.get("channel") or "neko"),
        source="bot_bridge",
        is_group=bool(args.get("group_id")),
        raw={"tool_call": True, **extra},
    )


# --------------------------------------------------------------------------- #
# 源码探测
# --------------------------------------------------------------------------- #


def _read_source(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _source_is_hoshino(source: str) -> bool:
    """源码是不是 Hoshino 插件。

    判据（Hoshino 插件没有清单文件，只能看源码）：

    * **强判据**：模块级 ``import hoshino`` / ``import aiocqhttp``；源码里出现 ``hoshino.`` /
      ``aiocqhttp.`` 属性前缀或对这两个包的任何导入；``Service("...")`` 调用；
    * **弱判据**：出现两个及以上 Hoshino / nonebot v1 风格的注册函数名
      （``on_command`` / ``on_prefix`` / ``on_message`` / ``scheduled_job`` …），
      且源码里确实提到 ``hoshino`` / ``aiocqhttp``，并且**没有** ``nonebot.adapters``
      导入（那是 NoneBot2 的形状，交给 NoneBot2 兼容层 —— 两个框架都有 ``on_command``）。
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return bool(_IMPORT_RE.search(source))
    if _module_level_imports(tree.body, ("hoshino", "aiocqhttp")):
        return True
    if _imports_anywhere(tree, ("hoshino", "aiocqhttp")):
        return True
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _STRONG_CALLS:
                return True
            if isinstance(func, ast.Attribute):
                if func.attr in _STRONG_CALLS:
                    return True
                if func.attr in _WEAK_MARKERS:
                    names.add(func.attr)
            elif isinstance(func, ast.Name) and func.id in _WEAK_MARKERS:
                names.add(func.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id in _STRONG_ATTRS:
                return True
    if len(names) >= 2 and ("hoshino" in source or "aiocqhttp" in source):
        return not _module_level_nonebot_v2_imports(source)
    return False


def _imports_anywhere(tree: ast.AST, packages: tuple[str, ...]) -> bool:
    """整棵树（含函数体里的延迟导入）是否导入了给定包。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == package or alias.name.startswith(f"{package}.") for alias in node.names for package in packages
            ):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if any(module == package or module.startswith(f"{package}.") for package in packages):
                return True
    return False


def _features_of(source: str) -> list[str]:
    """源码里命中的 Hoshino 特征标记（写进 manifest.extra，便于排查识别结果）。"""
    features: set[str] = set()
    if _module_level_imports(_parse_or_empty(source), ("hoshino",)):
        features.add("import hoshino")
    if _module_level_imports(_parse_or_empty(source), ("aiocqhttp",)):
        features.add("import aiocqhttp")
    if _module_level_nonebot_imports(source):
        features.add("import nonebot(v1)")
    if _module_level_nonebot_v2_imports(source):
        features.add("import nonebot.adapters")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return sorted(features)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _STRONG_CALLS or name in _WEAK_MARKERS:
                features.add(f"{name}(")
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in _STRONG_ATTRS:
            features.add(f"{node.value.id}.{node.attr}")
    return sorted(features)


def _parse_or_empty(source: str) -> list[Any]:
    try:
        return list(ast.parse(source).body)
    except SyntaxError:
        return []


def _module_level_imports(nodes: Sequence[Any], packages: tuple[str, ...]) -> bool:
    """模块级（含 ``if`` / ``try`` / ``with`` 分支，不进入函数体）是否导入了给定包。"""
    for node in _iter_module_level_imports(nodes):
        if isinstance(node, ast.Import):
            if any(
                alias.name == package or alias.name.startswith(f"{package}.") for alias in node.names for package in packages
            ):
                return True
        else:
            module = node.module or ""
            if any(module == package or module.startswith(f"{package}.") for package in packages):
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


def _module_level_nonebot_imports(source: str) -> bool:
    """模块级 ``import nonebot``（v1 直连；nonebot.adapters 是 v2 形状，单独判断）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return bool(re.search(r"(?m)^(?:from|import)\s+nonebot(?:\.|\s|$)", source))
    return _module_level_imports(tree.body, ("nonebot",))


def _module_level_nonebot_v2_imports(source: str) -> bool:
    """模块级 ``from nonebot.adapters…``（NoneBot2 的适配器导入）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "nonebot.adapters" in source
    for node in _iter_module_level_imports(tree.body):
        module = node.module or "" if isinstance(node, ast.ImportFrom) else ""
        if module.startswith("nonebot.adapters"):
            return True
        if isinstance(node, ast.Import):
            if any(alias.name.startswith("nonebot.adapters") for alias in node.names):
                return True
    return False


def _scan_services(source: str) -> list[dict[str, Any]]:
    """从源码里提取 ``Service("<名>", ...)`` 声明（名字 + 可读参数）。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    found: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "Service" or not node.args:
            continue
        first = node.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue
        entry: dict[str, Any] = {"name": first.value, "line": int(getattr(node, "lineno", 0) or 0)}
        for keyword in node.keywords:
            if keyword.arg is None:
                continue
            entry[keyword.arg] = _literal_or_expr(keyword.value)
        found.append(entry)
    found.sort(key=lambda item: item.get("line", 0))
    return found


def _literal_or_expr(node: ast.AST) -> Any:
    """能取字面量就取字面量，否则退回源码表达式文本（例如 ``priv.SUPERUSER``）。"""
    try:
        return ast.literal_eval(node)
    except Exception:  # noqa: BLE001 — 非字面量（属性访问 / 调用）走 unparse
        try:
            return ast.unparse(node)
        except Exception:  # noqa: BLE001 — 极端情况下连 unparse 都失败
            return ""


def _pick_dir_entry(path: Path) -> tuple[Path | None, dict[Path, str]]:
    """挑出目录插件的入口文件，并返回"顶层 .py → 源码"的映射。

    只在**目录顶层**找 Python 文件：Hoshino 的 ``hoshino/modules/<name>/`` 里是
    ``__init__.py`` 与/或若干 ``*.py``（上游按文件逐一加载）；分类目录
    （``data/plugins/hoshino/``）下面只有子目录，因此不会误判成插件。
    """
    sources: dict[Path, str] = {}
    for item in sorted(path.iterdir()):
        if item.is_dir() or item.suffix != PYTHON_SUFFIX:
            continue
        if item.name.startswith("_") and item.name != "__init__.py":
            continue
        source = _read_source(item)
        if source is None:
            continue
        sources[item] = source
    if not sources:
        return None, {}
    matched = [item for item, source in sources.items() if _source_is_hoshino(source)]
    if not matched:
        return None, {}
    init_file = path / "__init__.py"
    named = path / f"{path.name}{PYTHON_SUFFIX}"
    for candidate in (init_file, named):
        if candidate in sources:
            return candidate, sources
    return sorted(matched)[0], sources


def _top_level_modules(path: Path, *, skip: Iterable[Path] = ()) -> list[Path]:
    """目录顶层的兄弟模块（跳过 ``__init__.py``、``_`` 私有文件与 ``skip``）。"""
    skipped = set(skip)
    result: list[Path] = []
    for item in sorted(path.iterdir()):
        if item.is_dir() or item.suffix != PYTHON_SUFFIX:
            continue
        if item in skipped or item.name == "__init__.py":
            continue
        if item.name.startswith("_"):
            continue
        result.append(item)
        if len(result) > MAX_SCAN_FILES:
            break
    return result


def _plugin_python_files(manifest: PluginManifest) -> list[Path]:
    """插件的入口文件 + 目录顶层的兄弟模块（用于加载前的源码检查）。"""
    path = Path(manifest.path)
    if path.is_file():
        return [path]
    entry = Path(manifest.entry) if manifest.entry else path / "__init__.py"
    files = [entry] if entry.is_file() else []
    if path.is_dir():
        files.extend(_top_level_modules(path, skip=[entry]))
    return files


# --------------------------------------------------------------------------- #
# 清单 / 规格辅助
# --------------------------------------------------------------------------- #


def _spec_identifier(kind: str, label: str, index: int, seen: set[str]) -> str:
    """给一个处理器生成 ASCII 安全且唯一的入口 id。

    宿主只接受 ``[A-Za-z0-9_-]``（见 ``bridge/router.sanitize_entry_id``），而 Hoshino 的
    命令名 / 触发词大量是中文，直接清洗会互相撞车（``图`` 与 ``查图`` 都会变成 ``prefix``）。
    因此：能清洗出 ASCII 就用它，否则退回稳定摘要；人类可读的标签仍放在
    ``CommandSpec.name`` / ``patterns`` / ``usage`` 里。
    """
    cleaned = _sanitize_identifier(f"{kind}_{label}")
    if not cleaned or cleaned == kind:
        digest = hashlib.sha1(str(label or index).encode("utf-8")).hexdigest()[:8]
        cleaned = f"{kind}_{digest}" if str(label or "").strip() else f"{kind}_{index}"
    return _unique_identifier(cleaned, seen)


def _sanitize_identifier(raw: str) -> str:
    return _NON_ALNUM.sub("_", str(raw or "").strip()).strip("_")


def _unique_identifier(candidate: str, seen: set[str]) -> str:
    identifier = candidate or "handler"
    suffix = 2
    while identifier in seen:
        identifier = f"{candidate}_{suffix}"
        suffix += 1
    seen.add(identifier)
    return identifier[:64]


def _tool_name(plugin_id: str, spec_id: str) -> str:
    cleaned = _sanitize_identifier(f"{plugin_id}_{spec_id}")
    return cleaned[:64] or "hoshino_tool"


def _usage_for_command(registration: Any) -> str:
    names = " / ".join(registration.patterns)
    return f"{names} [参数]" if names else ""


def _tool_description(description: str, usage: str) -> str:
    parts = [item for item in (description, usage) if item]
    parts.append("Hoshino 消息处理器（调用时会按它自己的触发条件匹配）")
    return "；".join(parts)[:512]


def _command_parameters(registration: Any) -> dict[str, Any]:
    aliases = ", ".join(str(item) for item in registration.aliases)
    description = "发给这条 Hoshino 命令的完整消息文本（含命令名）"
    if aliases:
        description = f"{description}；别名：{aliases}"
    return {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": description},
            "group_id": {"type": "string", "description": "可选的群 ID；不填则按私聊消息执行"},
        },
        "required": [],
    }


def _priority_of(service: Any) -> int:
    """服务权限越高，越"专门"，路由排序时给它更高的确定性依据。"""
    if service is None:
        return 0
    return int(getattr(service, "use_priv", 0) or 0)


def _event_name(kind: str, events: tuple[str, ...]) -> str:
    """``on_notice('group_increase')`` → ``notice.group_increase``；空则只给大类。"""
    detail = next((str(item) for item in events if str(item)), "")
    return f"{kind}.{detail}" if detail else kind


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _apply_manifest_metadata(manifest: PluginManifest, record: Any, shim: _Shim) -> None:
    """用运行期拿到的 Service 信息补齐清单（detect 只能从源码里猜）。"""
    services = shim.runtime.plugin_services(manifest.plugin_id)
    if not services:
        return
    first = services[0]
    manifest.name = first.name or manifest.name
    if first.help:
        manifest.description = _first_line(first.help)
    manifest.extra["runtime_services"] = [
        {
            "name": item.name,
            "help": item.help,
            "use_priv": item.use_priv,
            "manage_priv": item.manage_priv,
            "enable_on_default": item.enable_on_default,
            "visible": item.visible,
            "bundle": item.bundle,
        }
        for item in services
    ]
    manifest.extra["hooks"] = record.hook_count()
    manifest.extra["jobs"] = [item.summary() for item in shim.runtime.plugin_jobs(manifest.plugin_id)]


async def _call_hook(hook: Any, bot: Any) -> Any:
    """调用启动 / 停止钩子（0 参就直接调，1 参及以上传 bot）。"""
    try:
        signature = inspect.signature(hook)
    except (TypeError, ValueError):
        result = hook(bot)
    else:
        positional = [
            parameter
            for parameter in signature.parameters.values()
            if parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        result = hook(bot) if positional else hook()
    if inspect.isawaitable(result):
        return await result
    return result


BACKEND = HoshinoBackend()
"""桥接注册表读取的模块级单例。"""
