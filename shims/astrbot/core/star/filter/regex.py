"""正则过滤器（N.E.K.O. shim 子集，与上游同语义）。"""

from __future__ import annotations

import re

from ...config import AstrBotConfig
from ...platform.astr_message_event import AstrMessageEvent
from . import HandlerFilter


class RegexFilter(HandlerFilter):
    """正则表达式过滤器（不受唤醒前缀约束）。"""

    def __init__(self, regex: str | re.Pattern[str]) -> None:
        self.regex = re.compile(regex)
        self.regex_str = self.regex.pattern

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        del cfg
        return bool(self.regex.search(event.get_message_str().strip()))


__all__ = ["RegexFilter"]
