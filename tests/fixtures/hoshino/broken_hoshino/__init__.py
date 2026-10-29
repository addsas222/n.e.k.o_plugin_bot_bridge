"""坏插件：模块导入期就抛异常 —— 兼容层必须给出结构化的 ``PluginLoadError``，而不是崩掉。"""

from __future__ import annotations

from hoshino import Service

sv = Service("broken_hoshino", help_="加载必然失败的插件")

raise RuntimeError("intentional import-time failure (bot_bridge hoshino fixture)")
