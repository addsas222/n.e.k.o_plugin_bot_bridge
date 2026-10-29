"""HTML / 文本转图片渲染。

上游由 ``astrbot.core.html_renderer`` 提供渲染服务（Playwright + 模板）；N.E.K.O.
侧没有等价实现，因此这里的入口一律抛
:class:`astrbot.UnsupportedCapabilityError`，绝不返回假图片。
"""

from __future__ import annotations

from typing import Any

from .._runtime import UnsupportedCapabilityError


async def render_t2i(
    text: str,
    return_url: bool = True,
    template_name: str | None = None,
) -> str:
    """把文本渲染为图片 URL（未映射）。"""
    raise UnsupportedCapabilityError(
        "render.t2i",
        f"N.E.K.O. 没有文本转图片渲染服务（template={template_name!r}, return_url={return_url}）",
    )


async def render_custom_template(
    tmpl: str,
    data: dict[str, Any],
    return_url: bool = True,
    options: dict[str, Any] | None = None,
) -> str:
    """渲染自定义 HTML 模板（未映射）。"""
    raise UnsupportedCapabilityError(
        "render.custom_template",
        f"N.E.K.O. 没有 HTML 模板渲染服务（tmpl={tmpl!r}, options={options!r}）",
    )


__all__ = ["render_custom_template", "render_t2i"]
