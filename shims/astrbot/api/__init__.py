"""AstrBot 插件 API（N.E.K.O. shim 子集）。

与上游 ``astrbot/api/__init__.py`` 的差异：``FunctionTool`` / ``ToolSet`` /
``BaseFunctionToolExecutor`` / ``html_renderer`` / ``sp`` / ``agent`` 没有提供
（对应的能力在 N.E.K.O. 侧不存在，导入即 ``ImportError``，不会拿到假对象）。
"""

from __future__ import annotations

from . import event, logger, message_components, provider, star
from ..core.config import AstrBotConfig
from ..core.star.register import register_llm_tool as llm_tool

__all__ = [
    "AstrBotConfig",
    "event",
    "llm_tool",
    "logger",
    "message_components",
    "provider",
    "star",
]
