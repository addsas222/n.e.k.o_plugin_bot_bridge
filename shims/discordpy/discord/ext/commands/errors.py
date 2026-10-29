"""``discord.ext.commands.errors``：命令错误家族。

本 shim 把命令错误和网关错误一起定义在 ``discord.errors``（原因写在那个模块的
docstring 里：避免 ``discord.errors`` ↔ ``discord.ext`` 的循环导入，并让
``discord.errors.CommandError`` 与 ``commands.CommandError`` 是同一个类）。
本模块只是上游路径的转出，方便 ``from discord.ext.commands.errors import ...`` 照常工作。
"""

from __future__ import annotations

from ...errors import *  # noqa: F401,F403 — 上游路径转出
from ...errors import __all__ as _error_names

__all__ = list(_error_names)
