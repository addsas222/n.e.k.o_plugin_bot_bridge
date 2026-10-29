"""Hoshino 兼容 shim 的桥接运行时（不是上游 API）。

``aiocqhttp._runtime`` 负责"宿主能力面 + 输出收集 + 插件上下文"；本模块在它之上加
Hoshino 自己的东西：

* **插件记录**（:class:`PluginRecord`）：导入期间注册的 Service / 命令 / 触发器 /
  ``on_notice`` / ``on_request`` / 生命周期钩子 / 定时任务都挂在记录上，桥接层据此填
  ``LoadedPlugin`` 的 specs；
* **分发报告**（:class:`RunReport`）：一次 ``handle_message`` 的产出（命中哪些 handler、
  回复文本、结构化消息段、错误、被 only_to_me / 权限挡掉的候选项）；
* **配置快照**：``hoshino.config`` 的只读来源（桥接 ``[bridge]`` 段的 ``hoshino_*`` 键 +
  ``hoshino_config`` 覆盖），以及 Hoshino 侧默认值（``SUPERUSERS`` / ``COMMAND_START`` …）。

所有状态都在锁内维护，且**幂等可重入**：重复 load 同一个插件会先清掉它上一次注册的
Service / 触发器 / 钩子，多插件并存时互不影响。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from aiocqhttp._runtime import (
    RUNTIME as OUTPUT,
    ShimHostBridge,
    UnsupportedCapabilityError,
    emit_log,
    emit_message,
    emit_reply,
)
from aiocqhttp.message import normalize_text

__all__ = [
    "DEFAULT_SETTINGS",
    "CommandRegistration",
    "JobRegistration",
    "PluginRecord",
    "RunReport",
    "ServiceRecord",
    "SessionRegistration",
    "TriggerRegistration",
    "UnsupportedCapabilityError",
    "all_plugins",
    "attach_plugin_module",
    "begin_run",
    "bind_runtime",
    "bundles",
    "capture_replies",
    "collector",
    "config_snapshot",
    "current_plugin",
    "current_plugin_id",
    "drop_plugin",
    "emit_log",
    "emit_message",
    "emit_reply",
    "plugin_commands",
    "plugin_hooks",
    "plugin_jobs",
    "plugin_preprocessors",
    "plugin_record",
    "plugin_sessions",
    "plugin_services",
    "plugin_triggers",
    "record_hook",
    "record_preprocessor",
    "register_command",
    "register_job",
    "register_service",
    "register_session",
    "register_trigger",
    "service_registry",
    "settings",
]

#: 桥接配置里 ``hoshino_*`` 键 → 快照键（``hoshino_config`` 整个 dict 可以直接覆盖任意键）。
SETTINGS_TO_SNAPSHOT: dict[str, str] = {
    "hoshino_superusers": "SUPERUSERS",
    "hoshino_nickname": "NICKNAME",
    "hoshino_command_start": "COMMAND_START",
    "hoshino_res_dir": "RES_DIR",
    "hoshino_res_url": "RES_URL",
    "hoshino_res_protocol": "RES_PROTOCOL",
    "hoshino_black_list": "BLACK_LIST",
    "hoshino_white_list": "WHITE_LIST",
    "hoshino_modules_on": "MODULES_ON",
    "hoshino_debug": "DEBUG",
}

#: 运行期默认值（``hoshino.config`` 与 ``priv`` 都从这里取）。
DEFAULT_SETTINGS: dict[str, Any] = {
    "self_id": "neko",
    "command_start": ("/",),
    "command_sep": ".",
    "superusers": (),
    "nickname": (),
    "allow_private": True,
    "debug": True,
    "config": {},
}

#: 生命周期钩子种类。
HOOK_KINDS: tuple[str, ...] = ("startup", "shutdown")


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #


@dataclass
class ServiceRecord:
    """一个 ``Service`` 的注册信息。"""

    name: str
    service: Any = None
    plugin_id: str = ""
    help: str = ""
    use_priv: int = 1
    manage_priv: int = 21
    enable_on_default: bool = True
    visible: bool = True
    bundle: str = "通用"


@dataclass
class CommandRegistration:
    """``Service.on_command`` 注册的一条命令。"""

    name: str
    aliases: tuple[str, ...] = ()
    func: Any = None
    service: Any = None
    plugin_id: str = ""
    only_to_me: bool = False
    deny_tip: str = ""

    @property
    def patterns(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    @property
    def label(self) -> str:
        return self.name

    @property
    def description(self) -> str:
        return _first_line(getattr(self.func, "__doc__", "") or "")


@dataclass
class TriggerRegistration:
    """一个触发器（prefix / suffix / keyword / rex / fullmatch）注册的处理器。"""

    kind: str
    value: Any = ""
    func: Any = None
    service: Any = None
    plugin_id: str = ""
    only_to_me: bool = False
    normalize_text: bool = False

    @property
    def label(self) -> str:
        if isinstance(self.value, str):
            return self.value
        return str(getattr(self.value, "pattern", self.value))

    @property
    def description(self) -> str:
        return _first_line(getattr(self.func, "__doc__", "") or "")


@dataclass
class SessionRegistration:
    """``on_notice`` / ``on_request`` 注册的处理器。"""

    kind: str
    events: tuple[str, ...] = ()
    func: Any = None
    service: Any = None
    plugin_id: str = ""
    session_style: bool = True

    @property
    def label(self) -> str:
        return ",".join(str(item) for item in self.events) or self.kind

    @property
    def description(self) -> str:
        return _first_line(getattr(self.func, "__doc__", "") or "")


@dataclass
class JobRegistration:
    """``scheduled_job`` 登记的一条定时任务（登记但**不执行**，见兼容层 docstring）。"""

    func: Any = None
    trigger: str = ""
    plugin_id: str = ""
    service_name: str = ""
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return str(getattr(self.func, "__name__", "") or "job")

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "trigger": self.trigger,
            "service": self.service_name,
            "args": [str(item) for item in self.args],
            "kwargs": {str(key): str(value) for key, value in self.kwargs.items()},
        }


@dataclass
class PluginRecord:
    """一个外来 Hoshino 插件的运行期记录。"""

    plugin_id: str
    module_name: str = ""
    path: str = ""
    module: ModuleType | None = None
    services: list[ServiceRecord] = field(default_factory=list)
    commands: list[CommandRegistration] = field(default_factory=list)
    triggers: list[TriggerRegistration] = field(default_factory=list)
    sessions: list[SessionRegistration] = field(default_factory=list)
    jobs: list[JobRegistration] = field(default_factory=list)
    preprocessors: list[Callable[..., Any]] = field(default_factory=list)
    hooks: dict[str, list[Callable[..., Any]]] = field(default_factory=dict)

    def hook_count(self) -> dict[str, int]:
        return {kind: len(self.hooks.get(kind, ())) for kind in HOOK_KINDS}

    def summary(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "module": self.module_name,
            "path": self.path,
            "services": [item.name for item in self.services],
            "commands": [item.name for item in self.commands],
            "triggers": [f"{item.kind}:{item.label}" for item in self.triggers],
            "sessions": [f"{item.kind}:{item.label}" for item in self.sessions],
            "jobs": [item.name for item in self.jobs],
            "hooks": self.hook_count(),
        }


@dataclass
class RunReport:
    """一次分发的产出（兼容层据此填 ``CommandResult``）。"""

    plugin_id: str = ""
    replies: list[str] = field(default_factory=list)
    extras: list[dict[str, Any]] = field(default_factory=list)
    handlers: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    finished: bool = False
    matched_text: str = ""

    @property
    def handled(self) -> bool:
        """是否有 handler 真正跑过。"""
        return bool(self.handlers)

    def to_payload(self) -> dict[str, Any]:
        return {
            "handled": self.handled,
            "plugin_id": self.plugin_id,
            "handlers": list(self.handlers),
            "replies": list(self.replies),
            "extras": [dict(item) for item in self.extras],
            "errors": list(self.errors),
            "skipped": list(self.skipped),
            "finished": self.finished,
        }


# --------------------------------------------------------------------------- #
# 全局状态
# --------------------------------------------------------------------------- #

_lock = threading.RLock()
_plugins: dict[str, PluginRecord] = {}
_services: dict[str, Any] = {}
_bundles: dict[str, list[Any]] = {}
_settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
_extra_config: dict[str, Any] = {}


def bind_runtime(
    *,
    host: ShimHostBridge | None = None,
    reply_fallback: Callable[[str, str, Mapping[str, Any]], Any] | None = None,
    settings: Mapping[str, Any] | None = None,
) -> None:
    """绑定（或刷新）桥接运行时。幂等：只覆盖显式传入的部分。"""
    if host is not None:
        OUTPUT.install_host(host)
    if reply_fallback is not None:
        OUTPUT.set_reply_fallback(reply_fallback)
    if settings is not None:
        update_settings(settings)


def update_settings(patch: Mapping[str, Any]) -> dict[str, Any]:
    """合并运行期设置（宿主配置 → Hoshino 语义）。"""
    with _lock:
        for key, value in patch.items():
            if key in DEFAULT_SETTINGS or key.startswith("hoshino_"):
                _settings[str(key)] = value
        config = patch.get("hoshino_config")
        if isinstance(config, Mapping):
            _extra_config.update({str(key): value for key, value in config.items()})
    return settings()


def settings() -> dict[str, Any]:
    with _lock:
        return dict(_settings)


def config_snapshot() -> dict[str, Any]:
    """``hoshino.config`` 的只读快照（桥接配置 → 上游键名）。"""
    with _lock:
        values = dict(_settings)
        extra = dict(_extra_config)
    snapshot: dict[str, Any] = {
        "DEBUG": bool(values.get("debug", True)),
        "SUPERUSERS": _as_tuple(values.get("superusers")),
        "NICKNAME": _as_tuple(values.get("nickname")),
        "COMMAND_START": _as_tuple(values.get("command_start")) or ("/",),
        "COMMAND_SEP": str(values.get("command_sep") or "."),
        "BLACK_LIST": (),
        "WHITE_LIST": (),
        "MODULES_ON": (),
        "RES_DIR": "",
        "RES_URL": "",
        "RES_PROTOCOL": "file",
    }
    for key, target in SETTINGS_TO_SNAPSHOT.items():
        if key in values and values[key] not in (None, ""):
            snapshot[target] = values[key]
    snapshot["BLACK_LIST"] = _as_tuple(snapshot.get("BLACK_LIST"))
    snapshot["WHITE_LIST"] = _as_tuple(snapshot.get("WHITE_LIST"))
    snapshot["MODULES_ON"] = _as_tuple(snapshot.get("MODULES_ON"))
    snapshot.update(extra)
    if not snapshot.get("RES_DIR"):
        snapshot["RES_DIR"] = str(values.get("res_dir") or "")
    return snapshot


def service_registry() -> dict[str, Any]:
    """已加载的 Service（``Service.get_loaded_services()``）。"""
    with _lock:
        return dict(_services)


def bundles() -> dict[str, list[Any]]:
    """Service 的分组（``Service.get_bundles()``）。"""
    with _lock:
        return {name: list(items) for name, items in _bundles.items()}


def register_service(record: ServiceRecord) -> None:
    """登记一个 Service（``Service.__init__`` 调用）。"""
    plugin_record_ = _record_of(record.plugin_id)
    if plugin_record_ is not None:
        plugin_record_.services.append(record)
    with _lock:
        previous = _services.get(record.name)
        if previous is not None and getattr(previous, "_plugin_id", "") != record.plugin_id:
            # 上游会 assert 名字唯一；桥接里多插件并存，改成"先到先得 + 记一条日志"。
            emit_log(
                "warning",
                f"Service 名 {record.name!r} 已被另一个插件注册，本次注册只在 {record.plugin_id} 内生效",
                source=record.plugin_id,
            )
        else:
            _services[record.name] = record.service
            _bundles.setdefault(record.bundle, [])
            if record.service not in _bundles[record.bundle]:
                _bundles[record.bundle].append(record.service)


def unregister_plugin_services(plugin_id: str) -> None:
    """清掉某个插件注册的 Service（重复 load / 卸载时用）。"""
    with _lock:
        for name, service in list(_services.items()):
            if getattr(service, "_plugin_id", "") == plugin_id:
                _services.pop(name, None)
                for items in _bundles.values():
                    if service in items:
                        items.remove(service)


# --------------------------------------------------------------------------- #
# 插件上下文
# --------------------------------------------------------------------------- #


@contextmanager
def collector(plugin_id: str, module_name: str = "", path: str = "") -> Iterator[PluginRecord]:
    """进入某个插件的导入上下文；导入期间注册的东西都记到它名下。

    同一个 ``plugin_id`` 重复进入时**复用并清空**旧记录（幂等）—— 否则重复 load 会让
    命令 / 触发器规格翻倍。
    """
    with _lock:
        record = _plugins.get(plugin_id)
        if record is None:
            record = PluginRecord(plugin_id=plugin_id)
            _plugins[plugin_id] = record
        else:
            record.services = []
            record.commands = []
            record.triggers = []
            record.sessions = []
            record.jobs = []
            record.preprocessors = []
            record.hooks = {}
        record.module_name = module_name or record.module_name
        record.path = path or record.path
        unregister_plugin_services(plugin_id)
    clear_plugin_triggers(plugin_id)
    with OUTPUT.scope(plugin_id):
        yield record


def current_plugin() -> PluginRecord | None:
    """当前正在导入 / 运行的插件记录（上下文变量）。"""
    plugin_id = OUTPUT.current_plugin_id()
    if not plugin_id:
        return None
    with _lock:
        return _plugins.get(plugin_id)


def current_plugin_id() -> str:
    return OUTPUT.current_plugin_id()


def plugin_record(plugin_id: str) -> PluginRecord | None:
    with _lock:
        return _plugins.get(plugin_id)


def all_plugins() -> dict[str, PluginRecord]:
    with _lock:
        return dict(_plugins)


def attach_plugin_module(plugin_id: str, module: ModuleType) -> None:
    """登记插件模块对象（``hoshino.util.get_module`` 之类用得到）。"""
    with _lock:
        record = _plugins.get(plugin_id)
        if record is None:
            record = PluginRecord(plugin_id=plugin_id)
            _plugins[plugin_id] = record
        record.module = module
        record.module_name = record.module_name or module.__name__


def drop_plugin(plugin_id: str) -> None:
    """卸载记录（重新加载前调用，保证幂等）。"""
    with _lock:
        _plugins.pop(plugin_id, None)
    unregister_plugin_services(plugin_id)
    clear_plugin_triggers(plugin_id)
    OUTPUT.forget_plugin(plugin_id)


def clear_plugin_triggers(plugin_id: str) -> int:
    """把某个插件注册的触发器从全局链上摘掉（懒导入 trigger 避免循环依赖）。"""
    import hoshino.trigger as triggers

    removed = 0
    for item in triggers.chain:
        removed += item.clear_plugin(plugin_id)
    return removed


# --------------------------------------------------------------------------- #
# 注册入口（Service 的各方法调用）
# --------------------------------------------------------------------------- #


def register_command(registration: CommandRegistration) -> None:
    record = _record_of(registration.plugin_id)
    if record is None:
        return
    with _lock:
        record.commands.append(registration)


def register_trigger(registration: TriggerRegistration) -> None:
    record = _record_of(registration.plugin_id)
    if record is None:
        return
    with _lock:
        record.triggers.append(registration)


def register_session(registration: SessionRegistration) -> None:
    record = _record_of(registration.plugin_id)
    if record is None:
        return
    with _lock:
        record.sessions.append(registration)


def register_job(registration: JobRegistration) -> None:
    record = _record_of(registration.plugin_id)
    if record is None:
        return
    with _lock:
        record.jobs.append(registration)
    emit_log(
        "warning",
        f"hoshino: 定时任务 {registration.name}（{registration.trigger}）只登记不执行："
        "N.E.K.O. 不承载 apscheduler 式的后台调度",
        source=registration.plugin_id,
    )


def record_hook(plugin_id: str, kind: str, func: Callable[..., Any]) -> None:
    if kind not in HOOK_KINDS:
        raise ValueError(f"unknown hoshino hook kind: {kind}")
    record = _record_of(plugin_id)
    bucket = record.hooks.setdefault(kind, []) if record is not None else []
    if func not in bucket:
        bucket.append(func)


def record_preprocessor(plugin_id: str, func: Callable[..., Any]) -> None:
    record = _record_of(plugin_id)
    if record is None:
        emit_log("warning", "hoshino: message_preprocessor 注册在插件导入上下文之外，已忽略")
        return
    with _lock:
        if func not in record.preprocessors:
            record.preprocessors.append(func)


def _record_of(plugin_id: str) -> PluginRecord | None:
    record = current_plugin() if not plugin_id else None
    if record is None:
        with _lock:
            record = _plugins.get(plugin_id)
    return record


# --------------------------------------------------------------------------- #
# 读取入口（兼容层的规格填充 / 生命周期）
# --------------------------------------------------------------------------- #


def plugin_services(plugin_id: str) -> list[ServiceRecord]:
    with _lock:
        record = _plugins.get(plugin_id)
        return list(record.services) if record is not None else []


def plugin_commands(plugin_id: str) -> list[CommandRegistration]:
    with _lock:
        record = _plugins.get(plugin_id)
        return list(record.commands) if record is not None else []


def plugin_triggers(plugin_id: str) -> list[TriggerRegistration]:
    with _lock:
        record = _plugins.get(plugin_id)
        return list(record.triggers) if record is not None else []


def plugin_sessions(plugin_id: str, kind: str = "") -> list[SessionRegistration]:
    with _lock:
        record = _plugins.get(plugin_id)
        items = list(record.sessions) if record is not None else []
    return [item for item in items if not kind or item.kind == kind]


def plugin_jobs(plugin_id: str) -> list[JobRegistration]:
    with _lock:
        record = _plugins.get(plugin_id)
        return list(record.jobs) if record is not None else []


def plugin_hooks(plugin_id: str, kind: str) -> list[Callable[..., Any]]:
    with _lock:
        record = _plugins.get(plugin_id)
        return list(record.hooks.get(kind, ())) if record is not None else []


def plugin_preprocessors(plugin_id: str) -> list[Callable[..., Any]]:
    with _lock:
        record = _plugins.get(plugin_id)
        return list(record.preprocessors) if record is not None else []


# --------------------------------------------------------------------------- #
# 分发报告
# --------------------------------------------------------------------------- #


def begin_run(plugin_id: str = "") -> RunReport:
    """开始一次分发，返回结果收集器。"""
    return RunReport(plugin_id=plugin_id or current_plugin_id())


@contextmanager
def capture_replies(report: RunReport) -> Iterator[RunReport]:
    """把分发期间插件发出的文本 / 消息段收进 ``report``。"""
    with OUTPUT.capture(report.replies, report.extras):
        yield report


# --------------------------------------------------------------------------- #
# 杂项
# --------------------------------------------------------------------------- #


def normalize(string: Any) -> str:
    """上游 ``util.normalize_str`` 的同一套规范化（NFKC + 小写）。"""
    return normalize_text(str(string))


def _as_tuple(value: Any) -> tuple[Any, ...]:
    if value is None or value == "":
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(value)
    return (value,)


def _first_line(text: str) -> str:
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
