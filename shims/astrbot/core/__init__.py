"""AstrBot 核心包（N.E.K.O. shim 子集）。

上游的 ``sp``（共享偏好存储）与 ``astrbot_config``（全局配置）没有映射到
N.E.K.O.：插件配置走 ``Context.get_config()`` / ``astrbot.core.config.AstrBotConfig``，
键值存储走 ``PluginKVStoreMixin``。它们的缺失会在 ``ImportError`` 时立即暴露，
而不是返回一个能导入但不可用的假对象。
"""

from __future__ import annotations

import logging

from . import html_renderer

logger = logging.getLogger("astrbot")

__all__ = ["html_renderer", "logger"]
