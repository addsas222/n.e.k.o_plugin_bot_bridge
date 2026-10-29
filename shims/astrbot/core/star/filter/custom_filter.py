"""自定义过滤器（N.E.K.O. shim 子集，与上游同名同语义）。"""

from __future__ import annotations

from abc import ABCMeta, abstractmethod

from ...config import AstrBotConfig
from ...platform.astr_message_event import AstrMessageEvent
from . import HandlerFilter


class CustomFilterMeta(ABCMeta):
    """支持 ``FilterA & FilterB`` / ``FilterA | FilterB`` 的元类。"""

    def __and__(cls, other: type[CustomFilter]) -> CustomFilterAnd:
        if not issubclass(other, CustomFilter):
            raise TypeError("Operands must be subclasses of CustomFilter.")
        return CustomFilterAnd(cls(), other())

    def __or__(cls, other: type[CustomFilter]) -> CustomFilterOr:
        if not issubclass(other, CustomFilter):
            raise TypeError("Operands must be subclasses of CustomFilter.")
        return CustomFilterOr(cls(), other())


class CustomFilter(HandlerFilter, metaclass=CustomFilterMeta):
    """插件自定义的事件过滤器基类。"""

    def __init__(self, raise_error: bool = True, **kwargs: object) -> None:
        del kwargs
        self.raise_error = raise_error

    @abstractmethod
    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """一个用于重写的自定义 Filter。"""
        raise NotImplementedError

    def __or__(self, other: CustomFilter) -> CustomFilterOr:
        return CustomFilterOr(self, other)

    def __and__(self, other: CustomFilter) -> CustomFilterAnd:
        return CustomFilterAnd(self, other)


class CustomFilterOr(CustomFilter):
    """两个过滤器的逻辑或。"""

    def __init__(self, filter1: CustomFilter, filter2: CustomFilter) -> None:
        super().__init__()
        if not isinstance(filter1, (CustomFilter, CustomFilterAnd, CustomFilterOr)):
            raise ValueError("CustomFilter class can only operate with other CustomFilter.")
        self.filter1 = filter1
        self.filter2 = filter2

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return self.filter1.filter(event, cfg) or self.filter2.filter(event, cfg)


class CustomFilterAnd(CustomFilter):
    """两个过滤器的逻辑与。"""

    def __init__(self, filter1: CustomFilter, filter2: CustomFilter) -> None:
        super().__init__()
        if not isinstance(filter1, (CustomFilter, CustomFilterAnd, CustomFilterOr)):
            raise ValueError("CustomFilter class can only operate with other CustomFilter.")
        self.filter1 = filter1
        self.filter2 = filter2

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        return self.filter1.filter(event, cfg) and self.filter2.filter(event, cfg)


__all__ = ["CustomFilter", "CustomFilterAnd", "CustomFilterMeta", "CustomFilterOr"]
