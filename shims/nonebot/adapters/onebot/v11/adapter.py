"""OneBot v11 协议适配器（上游 ``onebot.v11.adapter`` 的极小子集）。

桥接层不需要网络协议端，这里只提供插件会用到的东西：``Adapter.get_name()``
（→ ``"OneBot V11"``）、``Adapter.config``（全局配置）与已连接的 Bot 表。
"""

from __future__ import annotations

from typing import Any

from nonebot.adapters import Adapter as BaseAdapter

from .bot import Bot

__all__ = ["Adapter"]


class Adapter(BaseAdapter):
    """OneBot V11 协议适配器。"""

    def __init__(self, driver: Any, **kwargs: Any) -> None:
        super().__init__(driver, **kwargs)

    @classmethod
    def get_name(cls) -> str:
        return "OneBot V11"

    def __repr__(self) -> str:
        return f"Adapter(name={self.get_name()!r}, bots={list(self.bots)})"
