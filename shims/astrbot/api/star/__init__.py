"""插件基类与上下文的对外入口（N.E.K.O. shim 子集）。

``from astrbot.api.star import Context, Star, StarTools, register`` 与上游一致；
``astrbot.core.star.config``（废弃的配置读写辅助函数）没有提供。
"""

from __future__ import annotations

from ...core.star import Context, Star, StarMetadata, StarTools, star_map, star_registry
from ...core.star.register import register_star as register

__all__ = [
    "Context",
    "Star",
    "StarMetadata",
    "StarTools",
    "register",
    "star_map",
    "star_registry",
]
