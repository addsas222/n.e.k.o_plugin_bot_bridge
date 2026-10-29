"""缺少必需元数据字段、但入口文件存在的目录（detect 应当返回 None）。"""

from __future__ import annotations

from astrbot.api.star import Context, Star


class BadMetadataAstrBot(Star):
    """不会被加载的插件（metadata 非法）。"""

    def __init__(self, context: Context) -> None:
        super().__init__(context)
