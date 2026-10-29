"""消息组件导出（N.E.K.O. shim 子集）。

用法与上游一致：``import astrbot.api.message_components as Comp``。
"""

from __future__ import annotations

from ..core.message.components import *  # noqa: F403
from ..core.message.components import __all__ as _components_all

__all__ = list(_components_all)
