"""插件（Star）元数据与注册表（N.E.K.O. shim 子集）。

上游用 ``star_map``（key 为模块路径）与 ``star_registry``（列表）登记插件元数据；
``Star.__init_subclass__`` 在类定义时写入这里。桥接层据此找到插件主类。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .base import Star


@dataclass
class StarMetadata:
    """一个插件的元数据。"""

    name: str | None = None
    """插件名"""
    author: str | None = None
    """插件作者"""
    desc: str | None = None
    """插件简介"""
    short_desc: str | None = None
    """插件短简介"""
    version: str | None = None
    """插件版本"""
    repo: str | None = None
    """插件仓库地址"""
    display_name: str | None = None
    """插件展示名"""
    astrbot_version: str | None = None
    """声明的 AstrBot 版本范围"""
    support_platforms: list[str] = field(default_factory=list)
    """声明的支持平台"""
    pages: list[dict[str, Any]] = field(default_factory=list)
    """插件注册的 Pages"""
    i18n: dict[str, dict[str, str]] = field(default_factory=dict)
    """插件 i18n 资源"""

    star_cls_type: type[Star] | None = None
    """插件主类"""
    module_path: str | None = None
    """插件模块路径"""
    module: Any = None
    """已导入的模块对象"""
    star_cls: Star | None = None
    """插件实例"""
    config: Any = None
    """插件配置"""
    root_dir_name: str | None = None
    """插件目录名"""
    reserved: bool = False
    """是否为内置（保留）插件"""
    activated: bool = True
    """是否处于激活状态"""
    logo_path: str | None = None
    star_handler_full_names: list[str] = field(default_factory=list)
    """该插件注册的 Handler 全名"""

    @property
    def plugin_id(self) -> str:
        """原生插件 id：``author/name``（与上游一致，全小写）。"""
        p_name = (self.name or "unknown").lower().replace("/", "_")
        p_author = (self.author or "unknown").lower().replace("/", "_")
        return f"{p_author}/{p_name}"

    def __str__(self) -> str:
        return f"Plugin {self.name} ({self.version}) by {self.author}: {self.desc}"

    def __repr__(self) -> str:
        return self.__str__()


star_registry: list[StarMetadata] = []
"""按注册顺序保存所有插件元数据。"""

star_map: dict[str, StarMetadata] = {}
"""key 是模块路径（``__module__``）。"""

__all__ = ["StarMetadata", "star_map", "star_registry"]
