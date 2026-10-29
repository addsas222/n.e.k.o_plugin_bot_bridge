"""AstrBot 平台元数据（N.E.K.O. shim 子集）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class PlatformMetadata:
    """平台适配器的元数据。"""

    name: str
    """平台的类型名，如 aiocqhttp / discord；桥接层使用 ``neko``。"""
    description: str
    """平台描述。"""
    id: str
    """平台实例的唯一标识符。"""
    default_config_tmpl: dict[str, Any] | None = None
    adapter_display_name: str | None = None
    logo_path: str | None = None
    support_streaming_message: bool = True
    support_proactive_message: bool = True
    module_path: str | None = None
    i18n_resources: dict[str, dict[str, str]] | None = None
    config_metadata: dict[str, Any] | None = None


__all__ = ["PlatformMetadata"]
