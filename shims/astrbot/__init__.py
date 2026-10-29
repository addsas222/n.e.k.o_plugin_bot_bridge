"""AstrBot 插件 API 的兼容 shim（N.E.K.O. bot_bridge 专用）。

本包只复刻 AstrBot（上游 4.28.1）**插件面向**的 API 子集，且**只依赖标准库**，
以便在 N.E.K.O. 冻结的 Python 3.11 解释器里直接可用。它与上游的差异（哪些能力
被映射到 N.E.K.O.、哪些会抛 :class:`UnsupportedCapabilityError`）记在
``bridge/backends/astrbot.py`` 的模块 docstring 里。

第三方 AstrBot 插件通过 ``import astrbot.*`` 使用本包；桥接层负责把
``bot_bridge/shims`` 放进 ``sys.path`` 并注入宿主能力面。
"""

from __future__ import annotations

import logging

from ._runtime import RUNTIME, ShimHostBridge, UnsupportedCapabilityError

__version__ = "4.28.1"
"""模拟的上游 AstrBot 版本号（本 shim 复刻的 API 子集对应这个版本）。"""

_NEKO_BRIDGE_SHIM = True
"""桥接层用来确认 ``sys.modules['astrbot']`` 确实是本 shim 而不是真的 AstrBot。"""

_NEKO_SHIM_VERSION = "0.1.0"
"""shim 自身版本。"""

_NEKO_SHIM_UPSTREAM = "https://github.com/AstrBotDevs/AstrBot"

logger = logging.getLogger("astrbot")
"""与上游 `astrbot/__init__.py` 一致的模块级 logger。

插件应当使用 ``astrbot.api.logger``（会按调用模块路由到宿主日志）。
"""

__all__ = [
    "RUNTIME",
    "ShimHostBridge",
    "UnsupportedCapabilityError",
    "__version__",
    "logger",
]
