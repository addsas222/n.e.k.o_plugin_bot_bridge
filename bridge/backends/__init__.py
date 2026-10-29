"""各框架兼容层的注册表。

每个兼容层模块（``backends/maibot.py`` / ``backends/nonebot.py`` /
``backends/astrbot.py``）导出一个实现了 :class:`FrameworkBackend` 的 ``BACKEND``
对象。模块本身必须能在没有安装对应框架的情况下被 import —— 兼容层要自己把
"框架不在" 表达成 ``detect() -> None``，缺依赖则抛
:class:`BackendUnavailableError` 或 ``ImportError``，由这里记录成结构化状态。
"""

from __future__ import annotations

import importlib
import importlib.util
from collections.abc import Sequence
from pathlib import Path

from ..contracts import FrameworkBackend
from ..errors import BackendUnavailableError, BridgeError

#: 兼容层模块名 -> 模块路径（相对本包）。
BACKEND_MODULES: dict[str, str] = {
    "maibot": ".maibot",
    "nonebot": ".nonebot",
    "astrbot": ".astrbot",
    "hoshino": ".hoshino",
    # 模块文件名与框架名不一致时，显式映射优先于按文件名自动发现。
    "discord": ".discordpy",
}

#: 兼容层的展示名，用于状态输出（即使模块 import 失败也能报出来）。
BACKEND_LABELS: dict[str, str] = {
    "maibot": "MaiBot",
    "nonebot": "NoneBot2",
    "astrbot": "AstrBot",
    "hoshino": "HoshinoBot (Arcadia)",
    "discord": "discord.py cogs",
}


def _discover_backend_modules() -> dict[str, str]:
    """扫描本目录，把 ``<name>.py`` 自动登记成兼容层。

    加一个新框架只需要放一个 ``bridge/backends/<name>.py``（导出 ``BACKEND``），
    不用再回来改这张表；显式表里的条目优先（顺序也是）。
    """
    package_dir = Path(__file__).resolve().parent
    discovered = dict(BACKEND_MODULES)
    claimed = {str(path).lstrip(".") for path in discovered.values()}
    for module_path in sorted(package_dir.glob("*.py")):
        name = module_path.stem
        if name.startswith("_") or name in claimed:
            continue
        discovered.setdefault(name, f".{name}")
    return discovered


def _module_exists(module_path: str, package: str | None) -> bool:
    try:
        return importlib.util.find_spec(module_path, package=package) is not None
    except (ImportError, ValueError):
        return False


def load_backends(enabled: Sequence[str] | None = None) -> tuple[list[FrameworkBackend], dict[str, str]]:
    """导入启用的兼容层。

    Returns:
        ``(backends, errors)``：可用的兼容层列表，以及 ``框架名 -> 失败原因``。
    """
    table = _discover_backend_modules()
    wanted = [str(item) for item in (enabled or table.keys())]
    backends: list[FrameworkBackend] = []
    errors: dict[str, str] = {}
    for name in wanted:
        # 表里没有也可以直接按模块名找（放一个 backends/<name>.py 就能用）。
        module_path = table.get(name) or f".{name}"
        if not table.get(name) and not _module_exists(module_path, __package__):
            errors[name] = f"unknown framework backend: {name}"
            continue
        try:
            module = importlib.import_module(module_path, package=__package__)
        except Exception as exc:  # noqa: BLE001 — 兼容层坏掉不能让整个桥接起不来
            errors[name] = f"{type(exc).__name__}: {exc}"
            continue
        backend = getattr(module, "BACKEND", None)
        if backend is None:
            errors[name] = "backend module does not export BACKEND"
            continue
        missing_attributes = [
            attribute
            for attribute in ("name", "display_name", "detect", "load", "start", "stop")
            if not hasattr(backend, attribute)
        ]
        if missing_attributes:
            errors[name] = f"BACKEND is missing: {', '.join(missing_attributes)}"
            continue
        missing_requirements = _missing_requirements(backend)
        if missing_requirements:
            # 缺依赖时整层停用并把原因记下来 —— 否则用户只会看到「一个插件都没发现」。
            errors[name] = f"missing runtime dependency: {', '.join(missing_requirements)}"
            continue
        backends.append(backend)  # type: ignore[arg-type]
    return backends, errors


def _missing_requirements(backend: object) -> list[str]:
    """返回 ``FrameworkBackend.requires`` 里当前环境 import 不到的模块名。"""
    required = getattr(backend, "requires", ()) or ()
    missing: list[str] = []
    for raw in required:
        module_name = str(raw).strip()
        if not module_name:
            continue
        try:
            found = importlib.util.find_spec(module_name)
        except (ImportError, ValueError, ModuleNotFoundError):
            found = None
        if found is None:
            missing.append(module_name)
    return missing


def describe_backend_error(error: BaseException) -> str:
    if isinstance(error, BackendUnavailableError):
        return error.detail or str(error)
    if isinstance(error, BridgeError):
        return str(error)
    return f"{type(error).__name__}: {error}"


__all__: list[str] = [
    "BACKEND_LABELS",
    "BACKEND_MODULES",
    "describe_backend_error",
    "load_backends",
]
