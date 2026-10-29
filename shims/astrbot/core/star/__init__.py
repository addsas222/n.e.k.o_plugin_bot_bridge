"""AstrBot 插件（Star）子系统（N.E.K.O. shim 子集）。"""

from __future__ import annotations

from .base import Star
from .context import Context
from .star import StarMetadata, star_map, star_registry
from .star_handler import (
    EventType,
    StarHandlerMetadata,
    StarHandlerRegistry,
    star_handlers_registry,
)
from .star_tools import StarTools

__all__ = [
    "Context",
    "EventType",
    "Star",
    "StarHandlerMetadata",
    "StarHandlerRegistry",
    "StarMetadata",
    "StarTools",
    "star_handlers_registry",
    "star_map",
    "star_registry",
]
