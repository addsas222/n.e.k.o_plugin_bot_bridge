"""OneBot v12 协议适配器（上游 ``onebot.v12.adapter`` 的极小子集）。

只提供插件会用到的东西：``Adapter.get_name()``（→ ``"OneBot V12"``）、
``Adapter.config`` 与已连接的 Bot 表。桥接层不需要网络协议端。
"""

from __future__ import annotations

from typing import Any

from nonebot.adapters import Adapter as BaseAdapter

from .bot import Bot

__all__ = ["Adapter"]


class Adapter(BaseAdapter):
    """OneBot V12 协议适配器。"""

    def __init__(self, driver: Any, **kwargs: Any) -> None:
        super().__init__(driver, **kwargs)

    @classmethod
    def get_name(cls) -> str:
        return "OneBot V12"

    def __repr__(self) -> str:
        return f"Adapter(name={self.get_name()!r}, bots={list(self.bots)})"
