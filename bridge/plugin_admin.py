"""BridgeRegistry 的启停 / 删除能力（由 ``registry.py`` 末尾导入，保持单文件内聚）。

拆出来的原因：启停/删除是**对磁盘有副作用**的操作（unload / rmtree），必须能单独测
（路径越界、半启动、文件型插件、状态目录清理），而主类文件已经很长。

设计要点
--------

* **定位**：接受 ``框架:插件`` 键、插件原生 id，或唯一后缀；歧义时报错而不是猜。
* **启停**：只动这一个插件（不重启整座桥）；被停掉的插件记进 ``[bridge].disabled_plugins``，
  下次启动/重扫不会再拉起来。返回值里带上新的禁用列表，由插件层负责持久化
  （注册表不碰宿主配置，便于测试）。
* **删除**：只允许删 ``plugins_root`` 里的东西；先停再删；文件型插件删文件、目录型插件
  删目录；可选连带清理该插件的数据/缓存目录（默认不清理，避免误删别人数据）。
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .contracts import LoadedPlugin, PluginManifest, framework_dirs_from_settings
from .errors import BridgeError, PluginNotLoadedError

__all__ = ["AmbiguousPluginError", "DeleteRefusedError", "PluginAdminMixin"]


class AmbiguousPluginError(BridgeError):
    """插件名不唯一（例如两个框架里都有同名的插件）。"""

    code = "plugin_ambiguous"

    def __init__(self, name: str, candidates: list[str]) -> None:
        super().__init__(f"插件名 {name!r} 不唯一：{'、'.join(candidates)}（请写全 框架:插件）")
        self.name = name
        self.candidates = list(candidates)


class DeleteRefusedError(BridgeError):
    """删除被拒绝（越界 / 根目录 / 文件系统错误）。"""

    code = "delete_refused"


class PluginAdminMixin:
    """给 :class:`BridgeRegistry` 用的启停 / 删除实现。"""

    # ------------------------------------------------------------------ 定位

    def find_key(self, identifier: str) -> str:
        """把用户给的插件名解析成注册表键；找不到或歧义都给结构化错误。"""
        needle = str(identifier or "").strip()
        if not needle:
            raise PluginNotLoadedError("(空)")
        plugins: Mapping[str, LoadedPlugin] = self._plugins  # type: ignore[attr-defined]
        manifests: Mapping[str, PluginManifest] = self._manifests  # type: ignore[attr-defined]
        if needle in plugins or needle in manifests:
            return needle
        exact = [key for key, plugin in plugins.items() if plugin.plugin_id == needle] + [
            key for key, manifest in manifests.items() if manifest.plugin_id == needle
        ]
        if len(set(exact)) == 1:
            return exact[0]
        suffix = sorted(
            {key for key in list(plugins) + list(manifests) if key.endswith(f":{needle}")}
        )
        if len(suffix) == 1:
            return suffix[0]
        if len(suffix) > 1:
            raise AmbiguousPluginError(needle, suffix)
        raise PluginNotLoadedError(needle)

    def disabled_plugins(self) -> list[str]:
        """当前被停用的插件 id（保持配置里的顺序，去重）。"""
        raw = self.settings().get("disabled_plugins") or []  # type: ignore[attr-defined]
        seen: list[str] = []
        for item in raw:
            text = str(item).strip()
            if text and text not in seen:
                seen.append(text)
        return seen

    def stopped_plugins(self) -> list[dict[str, Any]]:
        """磁盘上存在、但当前没在跑的插件（停用的 / 加载失败的）。

        面板要能对它们做「启动 / 删除」——所以必须带 ``key``（``框架:插件``）：
        只给原生 id 的话，跨框架同名插件会让调用方卡在「不唯一」上。
        """
        plugins: Mapping[str, LoadedPlugin] = self._plugins  # type: ignore[attr-defined]
        manifests: Mapping[str, PluginManifest] = self._manifests  # type: ignore[attr-defined]
        errors: Mapping[str, str] = self._plugin_errors  # type: ignore[attr-defined]
        disabled = set(self.disabled_plugins())
        rows: list[dict[str, Any]] = []
        for key in sorted(manifests):
            if key in plugins:
                continue
            manifest = manifests[key]
            rows.append(
                {
                    "key": key,
                    "plugin_id": manifest.plugin_id,
                    "framework": manifest.framework,
                    "name": manifest.display_name,
                    "path": str(manifest.path),
                    "enabled": False,
                    "disabled": manifest.plugin_id in disabled,
                    "error": str(errors.get(key, "") or ""),
                }
            )
        return rows

    def _manifest_for(self, key: str) -> PluginManifest:
        manifest = self._manifests.get(key)  # type: ignore[attr-defined]
        if manifest is not None:
            return manifest
        for candidate in self.discover():  # type: ignore[attr-defined]
            manifest_key = f"{candidate.framework}:{candidate.plugin_id}"
            self._manifests.setdefault(manifest_key, candidate)  # type: ignore[attr-defined]
            if manifest_key == key or candidate.plugin_id == key:
                return self._manifests[manifest_key] if manifest_key in self._manifests else candidate  # type: ignore[attr-defined]
        raise PluginNotLoadedError(key, "磁盘上也找不到这个插件")

    # ------------------------------------------------------------------ 启停

    async def set_enabled(self, identifier: str, enabled: bool) -> dict[str, Any]:
        """启动 / 停止**单个**插件，返回新状态（不负责写配置，调用方按 ``disabled`` 持久化）。"""
        key = self.find_key(identifier)
        manifest = self._manifest_for(key)
        plugin_id = manifest.plugin_id
        changed = False

        if enabled:
            if key in self._plugins:  # type: ignore[attr-defined]
                pass
            else:
                plugin = await self._load_one(manifest)  # type: ignore[attr-defined]
                self._plugins[key] = plugin  # type: ignore[attr-defined]
                changed = True
        else:
            plugin = self._plugins.pop(key, None)  # type: ignore[attr-defined]
            if plugin is not None:
                changed = True
                try:
                    backend = self._backend_for(plugin.framework)  # type: ignore[attr-defined]
                    await backend.stop(plugin)
                except Exception as exc:  # noqa: BLE001 — 停止失败也要把它从列表里摘掉
                    detail = f"{type(exc).__name__}: {exc}"
                    self._plugin_errors[key] = f"stop failed: {detail}"  # type: ignore[attr-defined]
                    self._host.log("warning", f"停止 {key} 失败：{detail}", source="registry")  # type: ignore[attr-defined]
                finally:
                    plugin.started = False

        disabled = [item for item in self.disabled_plugins() if item != plugin_id]
        if not enabled:
            disabled.append(plugin_id)
        self._apply_disabled(disabled)
        self._host.log(  # type: ignore[attr-defined]
            "info",
            f"{'启动' if enabled else '停止'}插件 {key}（{'有变化' if changed else '本来就是这个状态'}）",
            source="registry",
        )
        return {
            "plugin_id": plugin_id,
            "key": key,
            "framework": manifest.framework,
            "enabled": bool(enabled),
            "changed": changed,
            "loaded": key in self._plugins,  # type: ignore[attr-defined]
            "disabled_plugins": disabled,
        }

    def _apply_disabled(self, disabled: list[str]) -> None:
        """把新的禁用列表写回**内存里的**设置（磁盘那份由插件层用 config.update 落）。"""
        settings = self.settings()  # type: ignore[attr-defined]
        if isinstance(settings, dict):
            settings["disabled_plugins"] = list(disabled)
            return
        try:  # 只读映射（测试里的假宿主）就退化成私有覆盖
            override = getattr(self, "_settings_override", None)
            if override is None:
                override = dict(settings)
            override["disabled_plugins"] = list(disabled)
            self._settings_override = override
        except Exception:  # noqa: BLE001 — 设置不可写时不能影响启停本身
            pass

    # ------------------------------------------------------------------ 删除

    async def delete(self, identifier: str, *, purge_state: bool = False) -> dict[str, Any]:
        """删除插件（停 → 删文件 → 从清单里摘掉）；只允许删插件目录里的东西。"""
        key = self.find_key(identifier)
        manifest = self._manifest_for(key)
        plugin_id = manifest.plugin_id
        path = Path(manifest.path)
        root = Path(str(getattr(self._host, "plugins_root", "") or ".")).resolve(strict=False)  # type: ignore[attr-defined]
        target = path.resolve(strict=False)
        if target != root and root not in target.parents:
            # 外部插件源目录（[bridge.framework_dirs]）里的插件也落在这条拒绝上：
            # 那些目录多半是用户自己的真实机器人安装，删文件越权。提示里带上它属于哪个源。
            source_hint = ""
            for framework, source_dir in framework_dirs_from_settings(self.settings()).items():
                resolved_dir = source_dir.resolve(strict=False)
                if target == resolved_dir or resolved_dir in target.parents:
                    source_hint = f"（它来自 framework_dirs.{framework} = {source_dir}，那里不受删除保护也不受删除授权）"
                    break
            raise DeleteRefusedError(
                f"拒绝删除插件目录之外的路径：{target}（插件根目录 {root}）{source_hint}"
            )
        if target == root:
            raise DeleteRefusedError("拒绝删除插件根目录本身")

        status = await self.set_enabled(key, False)
        removed: list[str] = []
        detail = ""
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
                # 单文件插件：顺手清掉它自己的 .pyc（别整个 __pycache__ 一起删，可能还有别的插件）
                pycache = target.parent / "__pycache__"
                if pycache.is_dir():
                    for cached in pycache.glob(f"{target.stem}.*.pyc"):
                        cached.unlink(missing_ok=True)
            else:
                detail = "文件已经不在了（只做了摘除）"
            removed.append(str(target))
        except OSError as exc:
            raise DeleteRefusedError(f"删除失败：{type(exc).__name__}: {exc}") from exc

        purged: list[str] = []
        if purge_state:
            for label, directory in (
                ("data", self._host.data_dir(plugin_id)),  # type: ignore[attr-defined]
                ("cache", self._host.cache_dir(plugin_id)),  # type: ignore[attr-defined]
            ):
                try:
                    if Path(directory).exists():
                        await asyncio.to_thread(shutil.rmtree, Path(directory), True)
                        purged.append(f"{label}:{directory}")
                except Exception as exc:  # noqa: BLE001 — 状态清理失败不该让删除失败
                    self._host.log(  # type: ignore[attr-defined]
                        "warning",
                        f"清理 {label} 目录失败（{directory}）：{type(exc).__name__}: {exc}",
                        source="registry",
                    )

        self._plugins.pop(key, None)  # type: ignore[attr-defined]
        self._manifests.pop(key, None)  # type: ignore[attr-defined]
        self._plugin_errors.pop(key, None)  # type: ignore[attr-defined]
        disabled = [item for item in status["disabled_plugins"] if item != plugin_id]
        self._apply_disabled(disabled)
        self._host.log("info", f"已删除插件 {key}：{target}", source="registry")  # type: ignore[attr-defined]
        return {
            "plugin_id": plugin_id,
            "key": key,
            "framework": manifest.framework,
            "removed": removed,
            "purged": purged,
            "detail": detail,
            "disabled_plugins": disabled,
        }
