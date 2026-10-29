"""不是 NoneBot 插件的普通模块（``detect`` 必须返回 ``None``）。"""

from __future__ import annotations


def helper() -> str:
    """一个跟 nonebot 无关的函数。"""
    return "plain"
