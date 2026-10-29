"""外来机器人插件的发现、加载与生命周期管理。

与具体框架无关：所有框架差异都藏在 :class:`FrameworkBackend` 后面。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .backends import BACKEND_LABELS, describe_backend_error, load_backends
from .contracts import (
    FrameworkBackend,
    HostServices,
    LoadedPlugin,
    PluginManifest,
    framework_dirs_from_settings,
)
from .errors import PluginLoadError
from .plugin_admin import PluginAdminMixin
from .security import scan_plugin_dir

_SKIP_DIR_NAMES = frozenset({"__pycache__", "node_modules", "data", "cache", "config", "store"})
_MAX_SCAN_DEPTH = 2


def plugin_key(manifest: PluginManifest) -> str:
    """``框架:插件`` 形式的稳定键，避免不同框架的插件 id 撞车。"""
    return f"{manifest.framework}:{manifest.plugin_id}"


class BridgeRegistry(PluginAdminMixin):
    """管理外来插件的注册表。"""

    def __init__(self, host: HostServices) -> None:
        self._host = host
        self._plugins: dict[str, LoadedPlugin] = {}
        self._backends: list[FrameworkBackend] = []
        self._backend_errors: dict[str, str] = {}
        self._plugin_errors: dict[str, str] = {}
        self._manifests: dict[str, PluginManifest] = {}
        # ``插件键 -> 安全扫描报告``（``security.py`` 的 ``SecurityReport.as_dict()``）。
        # 加载被 enforce 拒绝时这里也留着报告，面板/模型要能查到「为什么被拒」。
        self._security: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ 视图

    @property
    def plugins(self) -> Mapping[str, LoadedPlugin]:
        return dict(self._plugins)

    @property
    def backends(self) -> Sequence[FrameworkBackend]:
        return tuple(self._backends)

    @property
    def plugin_errors(self) -> dict[str, str]:
        """``插件键 -> 失败原因``（发现/加载/停止阶段的所有错误）。"""
        return dict(self._plugin_errors)

    def get(self, key: str) -> LoadedPlugin | None:
        return self._plugins.get(key) or self._plugins.get(_match_suffix(self._plugins, key))

    def settings(self) -> Mapping[str, Any]:
        return self._host.settings

    def status(self) -> dict[str, Any]:
        per_framework: dict[str, int] = {}
        for plugin in self._plugins.values():
            per_framework[plugin.framework] = per_framework.get(plugin.framework, 0) + 1
        enabled_scan, scan_mode = self._security_settings()
        loaded_rows = []
        for plugin in self._plugins.values():
            row = plugin.summary()
            # 每个插件带上自己的扫描摘要；报告全文在 security_reports。
            row["security"] = self._security.get(f"{plugin.framework}:{plugin.plugin_id}")
            loaded_rows.append(row)
        return {
            "plugins_root": str(getattr(self._host, "plugins_root", "")),
            # 各框架单独配置的插件源目录（[bridge.framework_dirs]，空表 = 全部走公共根目录）
            "framework_dirs": {
                name: str(path) for name, path in sorted(framework_dirs_from_settings(self.settings()).items())
            },
            "security_scan": {"enabled": enabled_scan, "mode": scan_mode},
            "security_reports": dict(self._security),
            "backends": [
                {
                    "name": backend.name,
                    "label": getattr(backend, "display_name", BACKEND_LABELS.get(backend.name, backend.name)),
                    "plugins": per_framework.get(backend.name, 0),
                    "requires": list(getattr(backend, "requires", ()) or ()),
                }
                for backend in self._backends
            ],
            "backend_errors": dict(self._backend_errors),
            "plugin_count": len(self._plugins),
            "plugins": loaded_rows,
            # 停用的插件不在 plugins 里；调用方（面板/模型）要能看到它们仍然存在
            "disabled_plugins": self.disabled_plugins(),
            "stopped_plugins": self.stopped_plugins(),
            "errors": dict(self._plugin_errors),
        }

    # ------------------------------------------------------------ 启动 / 停止

    async def start(self) -> None:
        """加载并启动所有可发现的外来插件。"""
        self._security.clear()  # 上一轮的扫描报告整体作废（reload 语义）
        self._backends, self._backend_errors = load_backends(list(self._settings_frameworks()))
        if not self._backends:
            self._host.log(
                "warning",
                f"没有任何兼容层可用：{self._backend_errors or '未启用任何框架'}",
                source="registry",
            )
            return
        manifests = self.discover()
        if not manifests:
            self._host.log(
                "info",
                f"插件目录 {getattr(self._host, 'plugins_root', '?')} 里没有发现机器人插件",
                source="registry",
            )
            return
        disabled = {str(item) for item in (self.settings().get("disabled_plugins") or [])}
        for manifest in manifests:
            key = plugin_key(manifest)
            # 被停用的插件也要留清单：面板要能显示它、启停要能在两个方向上来回切。
            self._manifests[key] = manifest
            if manifest.plugin_id in disabled or Path(manifest.path).name in disabled:
                self._host.log("info", f"插件 {manifest.plugin_id} 在 disabled_plugins 中，跳过加载", source="registry")
                continue
            try:
                plugin = await self._load_one(manifest)
            except Exception as exc:  # noqa: BLE001 — 单个插件失败不能拖垮整座桥
                self._plugin_errors[key] = describe_backend_error(exc)
                self._host.log("error", f"加载 {key} 失败：{self._plugin_errors[key]}", source="registry")
                if not bool(self.settings().get("continue_on_error", True)):
                    raise
                continue
            self._plugins[key] = plugin
        self._host.log(
            "info",
            f"机器人插件加载完成：{len(self._plugins)} 个（失败 {len(self._plugin_errors)} 个）",
            source="registry",
        )

    def _settings_frameworks(self) -> Iterable[str]:
        enabled = self.settings().get("enabled_frameworks") or []
        return [str(item) for item in enabled] if enabled else []

    async def _load_one(self, manifest: PluginManifest) -> LoadedPlugin:
        backend = self._backend_for(manifest.framework)
        key = plugin_key(manifest)
        # 加载第三方代码之前的唯一闸门：enforce 模式下高风险命中会在这里抛
        # PluginLoadError，由 start() 的单插件失败路径接住（continue_on_error 生效）。
        await self._security_check(manifest, key)
        plugin = await asyncio.to_thread(backend.load, manifest, self._host)
        if not isinstance(plugin, LoadedPlugin):
            raise PluginLoadError(key, "backend.load() did not return LoadedPlugin")
        plugin.loaded_at = time.time()
        await backend.start(plugin)
        plugin.started = True
        self._host.log(
            "info",
            f"已加载 {key}：命令 {len(plugin.commands)} / 工具 {len(plugin.tools)} / "
            f"事件 {len(plugin.events)} / API {len(plugin.apis)}",
            source="registry",
        )
        return plugin

    def _backend_for(self, framework: str) -> FrameworkBackend:
        for backend in self._backends:
            if backend.name == framework:
                return backend
        raise PluginLoadError(framework, f"backend '{framework}' is not loaded")

    # ------------------------------------------------------------ 安全校验

    def _security_settings(self) -> tuple[bool, str]:
        """``[bridge.security_scan]`` 的 ``(enabled, mode)``；非法 mode 回落 ``report``。"""
        raw = self.settings().get("security_scan")
        section = raw if isinstance(raw, Mapping) else {}
        enabled = bool(section.get("enabled", True))
        mode = str(section.get("mode") or "report")
        return enabled, (mode if mode in ("report", "enforce") else "report")

    async def _security_check(self, manifest: PluginManifest, key: str) -> None:
        """加载前静态扫描；``report`` 只亮灯，``enforce`` 对高风险命中抛 :class:`PluginLoadError`。"""
        enabled, mode = self._security_settings()
        if not enabled:
            self._security.pop(key, None)
            return
        report = await asyncio.to_thread(scan_plugin_dir, Path(manifest.path))
        self._security[key] = report.as_dict()
        if report.dangerous:
            sample = "；".join(f"{item.reason}（{item.file}:{item.line}）" for item in report.dangerous[:3])
            if mode == "enforce":
                raise PluginLoadError(
                    key,
                    f"安全扫描发现 {len(report.dangerous)} 处高风险模式，"
                    f"已按 security_scan.mode=enforce 拒绝加载：{sample}",
                )
            self._host.log(
                "warning",
                f"安全扫描：{key} 有 {len(report.dangerous)} 处高风险模式（report 模式，仍将加载）：{sample}",
                source="security",
            )
        if report.warnings:
            self._host.log(
                "info",
                f"安全扫描：{key} 有 {len(report.warnings)} 处提示（详见面板/状态里的 security 字段）",
                source="security",
            )
        if report.truncated:
            self._host.log(
                "warning", f"安全扫描：{key} 源文件超过 {2000} 个，扫描被截断（结果不完整）", source="security"
            )

    async def stop(self) -> None:
        """停止并卸载全部插件（逆序，尽量不影响仍在跑的插件）。"""
        for key, plugin in reversed(list(self._plugins.items())):
            backend = self._backend_for(plugin.framework)
            try:
                await backend.stop(plugin)
            except Exception as exc:  # noqa: BLE001 — 停止失败要记录，但继续停其余的
                error = describe_backend_error(exc)
                self._plugin_errors[key] = f"stop failed: {error}"
                self._host.log("warning", f"停止 {key} 失败：{error}", source="registry")
            finally:
                plugin.started = False
        self._plugins.clear()

    async def reload(self) -> dict[str, Any]:
        """重启整座桥（重新扫描目录）。"""
        await self.stop()
        self._plugin_errors.clear()
        self._manifests.clear()
        await self.start()
        return self.status()

    # ------------------------------------------------------------------ 发现

    def discover(self) -> list[PluginManifest]:
        """扫描插件根目录与各框架配置的插件源目录，返回按 ``框架:插件`` 排序的清单。

        扫描顺序（先到先得，``plugin_key`` 去重）：

        1. ``[bridge.framework_dirs]`` 里每个框架**单独配置**的插件源目录
           —— 显式配置压过公共根目录里的同名插件；
        2. 公共根目录 ``plugins_root``（扁平与 ``<根>/<框架>/<插件>`` 布局都认）。
        """
        limit = int(self.settings().get("max_plugins_per_framework", 32) or 32)
        found: dict[str, PluginManifest] = {}
        counts: dict[str, int] = {}

        def _admit(manifest: PluginManifest | None, candidate: Path) -> None:
            if manifest is None:
                return
            counts[manifest.framework] = counts.get(manifest.framework, 0) + 1
            if counts[manifest.framework] > limit:
                self._host.log(
                    "warning",
                    f"{manifest.framework} 插件数量超过上限 {limit}，跳过 {candidate}",
                    source="registry",
                )
                return
            found.setdefault(plugin_key(manifest), manifest)  # 先扫到的源优先

        by_name = {backend.name: backend for backend in self._backends}
        # 1) 每个框架自己的插件源目录。目录不存在只告警跳过，**不代建**：
        #    那多半是用户手滑写错的路径，悄悄 mkdir 会把错误路径坐实。
        for framework, source_dir in sorted(framework_dirs_from_settings(self.settings()).items()):
            backend = by_name.get(framework)
            if backend is None:
                self._host.log(
                    "warning",
                    f"framework_dirs.{framework} 指向 {source_dir}，但该框架未启用或不存在，已忽略",
                    source="registry",
                )
                continue
            if not source_dir.is_dir():
                self._host.log(
                    "warning",
                    f"framework_dirs.{framework} 指向的目录不存在，已跳过：{source_dir}",
                    source="registry",
                )
                continue
            try:
                candidates = self._candidates(source_dir)
            except OSError as exc:
                self._host.log(
                    "warning",
                    f"扫描 framework_dirs.{framework}（{source_dir}）失败：{type(exc).__name__}: {exc}",
                    source="registry",
                )
                continue
            for candidate in candidates:
                _admit(self._detect(candidate, (backend,)), candidate)

        # 2) 公共根目录（默认仍会创建，保持既有行为）。
        root = Path(getattr(self._host, "plugins_root", "") or ".")
        try:
            root.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # noqa: BLE001 — 目录都建不出来就只能报错返回
            self._host.log("error", f"创建插件目录 {root} 失败：{type(exc).__name__}: {exc}", source="registry")
            return [found[key] for key in sorted(found)]
        try:
            candidates = self._candidates(root)
        except OSError as exc:
            self._host.log("error", f"扫描插件目录 {root} 失败：{type(exc).__name__}: {exc}", source="registry")
            return [found[key] for key in sorted(found)]
        for candidate in candidates:
            _admit(self._detect(candidate), candidate)
        return [found[key] for key in sorted(found)]

    def _candidates(self, root: Path) -> list[Path]:
        """深度 1-2 的候选路径：既支持 ``root/<plugin>/``，也支持 ``root/<framework>/<plugin>/``。"""
        result: list[Path] = []
        for child in sorted(root.iterdir()):
            if _skip(child):
                continue
            result.append(child)
            if child.is_dir():
                for nested in sorted(child.iterdir()):
                    if not _skip(nested):
                        result.append(nested)
        return result

    def _detect(self, path: Path, backends: Sequence[FrameworkBackend] | None = None) -> PluginManifest | None:
        for backend in backends if backends is not None else self._backends:
            try:
                manifest = backend.detect(path)
            except Exception as exc:  # noqa: BLE001 — 探测失败按“不是本框架”处理并记录
                self._host.log(
                    "debug",
                    f"{backend.name}.detect({path.name}) 抛错：{describe_backend_error(exc)}",
                    source="registry",
                )
                continue
            if manifest is None:
                continue
            if not manifest.framework:
                manifest.framework = backend.name
            return manifest
        return None


def _skip(path: Path) -> bool:
    name = path.name
    if name.startswith(".") or name.startswith("__"):
        return True
    if path.is_dir() and name in _SKIP_DIR_NAMES:
        return True
    return False


def _match_suffix(plugins: Mapping[str, LoadedPlugin], key: str) -> str:
    """允许用插件原生 id 或 ``框架:插件`` 之外的写法定位插件（唯一命中才生效）。"""
    if not key:
        return ""
    matches = [stored for stored in plugins if stored.endswith(f":{key}") or stored == key]
    return matches[0] if len(matches) == 1 else ""


__all__ = ["BridgeRegistry", "plugin_key"]
