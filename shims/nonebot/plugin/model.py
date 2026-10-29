"""插件模型（上游 ``nonebot.plugin.model`` 的子集）。

偏差：上游 ``PluginMetadata`` / ``Plugin`` 是 pydantic 模型（会做类型校验、支持
``model_dump``）；shim 用 dataclass 实现，字段名与语义一致但不做校验。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

__all__ = ["Plugin", "PluginMetadata"]


@dataclass
class PluginMetadata:
    """插件元信息，由插件编写者提供（``__plugin_meta__``）。"""

    name: str
    """插件名称"""
    description: str
    """插件功能介绍"""
    usage: str
    """插件使用方法"""
    type: str | None = None
    """插件类型，用于商店分类"""
    homepage: str | None = None
    """插件主页"""
    config: Any = None
    """插件配置项（上游要求是 pydantic 模型类；shim 接受任意可调用配置类）"""
    supported_adapters: set[str] | None = None
    """插件支持的适配器模块路径；``None`` 表示支持所有适配器"""
    extra: dict[Any, Any] = field(default_factory=dict)
    """插件额外信息"""

    def get_supported_adapters(self) -> set[type[Any]] | None:
        """获取当前已安装的插件支持适配器类列表。"""
        if self.supported_adapters is None:
            return None
        adapters: set[type[Any]] = set()
        for adapter in self.supported_adapters:
            module_path, _, attribute = adapter.partition(":")
            if module_path.startswith("~"):
                module_path = f"nonebot.adapters{module_path[1:]}"
            try:
                module = __import__(module_path, fromlist=[""])
            except ImportError:
                continue
            obj = module
            for part in attribute.split("."):
                if not part:
                    continue
                obj = getattr(obj, part, None)
                if obj is None:
                    break
            if isinstance(obj, type):
                adapters.add(obj)
        return adapters

    def model_dump(self) -> dict[str, Any]:
        """与 pydantic v2 的同名方法对齐（``nonebot.compat.model_dump`` 会调用它）。"""
        return dataclasses.asdict(self)


@dataclass(eq=False)
class Plugin:
    """已加载插件的信息。"""

    name: str
    """插件名称，NoneBot 使用 文件/文件夹 名称作为插件名称"""
    module: ModuleType
    """插件模块对象"""
    module_name: str
    """点分割模块路径"""
    manager: Any = None
    """导入该插件的插件管理器（shim 中为桥接运行时的记录）"""
    matcher: set[type[Any]] = field(default_factory=set)
    """插件加载时定义的 ``Matcher``"""
    parent_plugin: "Plugin | None" = None
    """父插件"""
    sub_plugins: set["Plugin"] = field(default_factory=set)
    """子插件集合"""
    metadata: PluginMetadata | None = None
    """插件元信息"""

    @property
    def id_(self) -> str:
        """插件索引标识。"""
        return f"{self.parent_plugin.id_}:{self.name}" if self.parent_plugin else self.name
