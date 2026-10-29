"""AstrBot Handler 过滤器基类（N.E.K.O. shim 子集）。"""

from __future__ import annotations

import abc

from ...config import AstrBotConfig
from ...platform.astr_message_event import AstrMessageEvent
from ...platform.message_type import MessageType


class HandlerFilter(abc.ABC):
    """一个 Handler 的事件过滤器。"""

    @abc.abstractmethod
    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """是否应当处理这个事件。"""
        raise NotImplementedError


__all__ = ["AstrBotConfig", "AstrMessageEvent", "HandlerFilter", "MessageType"]
