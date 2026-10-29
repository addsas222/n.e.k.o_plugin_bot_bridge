"""``hoshino.R`` 的兼容实现：资源文件（图片）句柄。

上游 ``R.img('pcr/xxx.jpg')`` 返回一个 :class:`ResImg`，插件用 ``.path``（本地路径）或
``.cqcode``（``[CQ:image,…]``）把图发出去；``RES_DIR`` / ``RES_URL`` / ``RES_PROTOCOL`` 来自
``hoshino.config``。

shim 的映射：

* ``RES_DIR`` 缺省是 ``<host.data_dir(plugin_id)>/resources``（桥接配置可以覆盖），
  句柄会挡住越界访问（与上游一样）；
* ``RES_PROTOCOL='file'``（默认）时 ``cqcode`` 返回 ``file:///`` 图片段；``'http'`` 需要
  一个对外提供静态资源的 HTTP 服务 —— shim 没有，显式抛能力缺失；
* ``.url`` 同理（N.E.K.O. 不对外提供资源 URL）；
* ``.open()`` 用 PIL（插件自己的依赖）打开图片；装了 PIL 就能用，没装则显式报能力缺失。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from aiocqhttp._runtime import RUNTIME, UnsupportedCapabilityError
from aiocqhttp.message import MessageSegment

from hoshino import _runtime

__all__ = ["ResImg", "ResObj", "get", "img", "res_dir"]


def res_dir() -> Path:
    """资源根目录（``hoshino.config.RES_DIR``，缺省落到插件数据目录）。"""
    configured = str(_runtime.config_snapshot().get("RES_DIR") or "")
    if configured:
        return Path(os.path.expanduser(configured))
    host = RUNTIME.host_or_none()
    plugin_id = _runtime.current_plugin_id()
    if host is not None and plugin_id:
        return host.data_dir(plugin_id) / "resources"
    return Path.cwd() / "resources"


class ResObj:
    """一个资源文件句柄（相对 ``RES_DIR``）。"""

    def __init__(self, res_path: Any) -> None:
        root = Path(os.path.abspath(res_dir()))
        relative = Path(str(res_path))
        full = Path(os.path.abspath(root / relative))
        if root != full and root not in full.parents:
            raise ValueError(f"Cannot access outside RESOURCE_DIR: {res_path}")
        self._relative = os.path.normpath(str(relative))

    @property
    def path(self) -> str:
        """资源文件的本地路径（供 Hoshino 内部使用）。"""
        return os.path.join(str(res_dir()), self._relative)

    @property
    def exist(self) -> bool:
        """文件在不在。"""
        return os.path.exists(self.path)

    @property
    def url(self) -> str:
        """上游返回对外 URL；N.E.K.O. 不提供静态资源服务，显式不支持。"""
        raise UnsupportedCapabilityError(
            "hoshino.R.url",
            "N.E.K.O. 不对外提供资源 URL；请用 .path 读本地文件，或 .cqcode 发图片",
        )

    def __str__(self) -> str:
        return self.path

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self._relative!r}>"


class ResImg(ResObj):
    """图片资源。"""

    @property
    def cqcode(self) -> MessageSegment:
        """图片消息段（``RES_PROTOCOL`` 决定用哪种形式）。"""
        protocol = str(_runtime.config_snapshot().get("RES_PROTOCOL") or "file").lower()
        if protocol == "file":
            return MessageSegment.image(f"file:///{os.path.abspath(self.path)}")
        if protocol == "http":
            raise UnsupportedCapabilityError(
                "hoshino.R.cqcode",
                "RES_PROTOCOL=http 需要 N.E.K.O. 提供静态资源服务；请改用 file 或 base64",
            )
        from hoshino.util import pic2b64

        return MessageSegment.image(pic2b64(self.path))

    def open(self) -> Any:
        """用 PIL 打开图片（PIL 是插件自己的依赖，shim 不代管）。"""
        try:
            from PIL import Image  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover — 取决于宿主环境
            raise UnsupportedCapabilityError(
                "hoshino.R.open",
                "当前环境没有安装 PIL/Pillow，无法打开图片",
            ) from exc
        try:
            return Image.open(self.path)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"缺少图片资源：{self.path}") from exc


def get(path: str, *paths: str) -> ResObj:
    """取一个通用资源句柄（上游同名）。"""
    return ResObj(os.path.join(path, *paths))


def img(path: str, *paths: str) -> ResImg:
    """取一个图片资源句柄（上游同名，自动加 ``img/`` 前缀）。"""
    return ResImg(os.path.join("img", path, *paths))
