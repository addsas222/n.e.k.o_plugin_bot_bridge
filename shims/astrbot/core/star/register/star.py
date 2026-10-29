"""[已废弃] 插件（Star）元数据注册装饰器（N.E.K.O. shim 子集，与上游同行为）。"""

from __future__ import annotations

import warnings

from ..star import StarMetadata, star_map

_warned_register_star = False


def register_star(
    name: str,
    author: str,
    desc: str,
    version: str,
    repo: str | None = None,
):
    """注册一个插件（Star）。

    [DEPRECATED] v3.5.19 之后不再需要它：AstrBot 会自动识别继承自 ``Star`` 的类。
    元数据优先级低于 ``metadata.yaml``。
    """
    global _warned_register_star
    if not _warned_register_star:
        _warned_register_star = True
        warnings.warn(
            "The 'register_star' decorator is deprecated and will be removed in a future version.",
            DeprecationWarning,
            stacklevel=2,
        )

    def decorator(cls):
        if not star_map.get(cls.__module__):
            metadata = StarMetadata(
                name=name,
                author=author,
                desc=desc,
                version=version,
                repo=repo,
            )
            star_map[cls.__module__] = metadata
        else:
            star_map[cls.__module__].name = name
            star_map[cls.__module__].author = author
            star_map[cls.__module__].desc = desc
            star_map[cls.__module__].version = version
            star_map[cls.__module__].repo = repo

        return cls

    return decorator


__all__ = ["register_star"]
