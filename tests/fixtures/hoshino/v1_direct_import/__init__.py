"""在模块级 ``import nonebot``（v1 语义）的 Hoshino 插件 —— 兼容层必须结构化拒绝。

Hoshino 跑在 nonebot **v1** 上，少数插件直接 ``from nonebot import ...``。桥接层里的
``nonebot`` 名字属于 NoneBot2 兼容层（语义不同），两套实现混用会静默出错，因此
``load()`` 会抛 ``PluginLoadError`` 并说明该怎么改（用 ``from hoshino.typing import ...``）。
"""

from __future__ import annotations

from hoshino import Service
from nonebot import get_bot  # noqa: F401 — 触发兼容层的显式拒绝

sv = Service("v1_direct_import", help_="直接 import nonebot v1 的插件（加载应被拒）")
