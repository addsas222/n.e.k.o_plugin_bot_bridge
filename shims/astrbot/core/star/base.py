"""插件基类 ``Star``（N.E.K.O. shim 子集）。

继承关系与上游一致（``CommandParserMixin`` + ``PluginKVStoreMixin``），
``__init_subclass__`` 会把子类登记进 ``star_map`` / ``star_registry``，因此插件类
只要写 ``class MyPlugin(Star)`` 就能被桥接层发现。

``text_to_image`` / ``html_render`` 依赖上游的渲染服务，N.E.K.O. 侧没有等价实现，
调用会抛 :class:`astrbot.UnsupportedCapabilityError`。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .. import html_renderer
from ..utils.command_parser import CommandParserMixin
from ..utils.plugin_kv_store import PluginKVStoreMixin
from .star import StarMetadata, star_map, star_registry

if TYPE_CHECKING:
    from .context import Context

logger = logging.getLogger("astrbot")


class Star(CommandParserMixin, PluginKVStoreMixin):
    """所有插件（Star）的父类。"""

    author: str
    name: str
    plugin_id: str
    context: Context
    logger: logging.Logger
    """该插件专属日志器（与全局 ``astrbot`` 日志隔离）。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        del config  # 上游只为兼容旧签名而接收它
        self.context = context
        metadata = star_map.get(self.__class__.__module__)
        plugin_name = (metadata.name if metadata else None) or getattr(self, "name", None)
        try:
            self.logger = (
                logging.getLogger(f"astrbot.plugin.{plugin_name}")
                if plugin_name
                else logging.getLogger("astrbot")
            )
        except AttributeError as exc:
            # 插件把 logger 定义成只读 property 时，保留它自己的实现（与上游一致，
            # 但这里记一条日志而不是静默吞掉）。
            logger.debug(
                "plugin %s defines a read-only logger property: %r",
                plugin_name or self.__class__.__name__,
                exc,
            )

    def _get_context_config(self) -> Any:
        """取 AstrBot 配置对象（存在 ``get_config`` 时优先）。"""
        get_config = getattr(self.context, "get_config", None)
        if callable(get_config):
            try:
                return get_config()
            except Exception as exc:
                logger.debug(f"get_config() failed: {exc}")
                return None
        return getattr(self.context, "_config", None)

    async def text_to_image(self, text: str, return_url: bool = True) -> str:
        """把文本转换为图片（未映射）。"""
        config_obj = self._get_context_config()
        template_name = None
        if hasattr(config_obj, "get"):
            try:
                template_name = config_obj.get("t2i_active_template")
            except Exception as exc:
                logger.debug(f"read t2i_active_template failed: {exc}")
                template_name = None
        return await html_renderer.render_t2i(
            text,
            return_url=return_url,
            template_name=template_name,
        )

    async def html_render(
        self,
        tmpl: str,
        data: dict[str, Any],
        return_url: bool = True,
        options: dict[str, Any] | None = None,
    ) -> str:
        """渲染 HTML 模板（未映射）。"""
        return await html_renderer.render_custom_template(
            tmpl,
            data,
            return_url=return_url,
            options=options,
        )

    async def initialize(self) -> None:
        """插件被激活时调用。"""

    async def terminate(self) -> None:
        """插件被禁用 / 重载时调用。"""

    def __del__(self) -> None:
        """[Deprecated] 插件被禁用 / 重载时会调用这个方法。"""

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if not star_map.get(cls.__module__):
            metadata = StarMetadata(
                star_cls_type=cls,
                module_path=cls.__module__,
            )
            star_map[cls.__module__] = metadata
            star_registry.append(metadata)
        else:
            star_map[cls.__module__].star_cls_type = cls
            star_map[cls.__module__].module_path = cls.__module__


__all__ = ["Star"]
