"""``astrbot.api.web`` —— 插件注册 Web API 用的请求/响应包装（上游同名模块）。

上游这个模块建在 FastAPI / Starlette 之上（``PluginRequest`` 包 ``Request``、
``json_response`` 包 ``JSONResponse``…）。N.E.K.O. 的插件侧**没有**给第三方插件
开放 HTTP 路由，宿主也不转发请求进来，因此这里只保证：

* **import 不失败**（否则插件整包加载不了，连跟 Web 无关的功能也一起没了）；
* **一旦真的构造请求/生成响应就显式失败**，错误信息说明没有 HTTP 面。

这样插件作者看到的是「这功能不支持」，而不是「插件加载失败，原因不明」。
"""

from __future__ import annotations

from typing import Any

from astrbot import UnsupportedCapabilityError

__all__ = [
    "PluginMultiDict",
    "PluginRequest",
    "PluginRequestProxy",
    "PluginUploadFile",
    "bind_request_context",
    "error_response",
    "file_response",
    "json_response",
    "stream_response",
]

_REASON = (
    "N.E.K.O. 没有给第三方插件开放 HTTP 路由；astrbot.api.web 的请求/响应对象无等价实现。"
)


def _unsupported(name: str) -> UnsupportedCapabilityError:
    return UnsupportedCapabilityError(f"astrbot.api.web.{name}", _REASON)


class PluginMultiDict:
    """占位：构造即显式不支持（上游是保留重复键的请求参数容器）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise _unsupported("PluginMultiDict")


class PluginUploadFile:
    """占位：构造即显式不支持。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise _unsupported("PluginUploadFile")


class PluginRequest:
    """占位：构造即显式不支持。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise _unsupported("PluginRequest")


class PluginRequestProxy:
    """占位：构造即显式不支持。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise _unsupported("PluginRequestProxy")


def bind_request_context(request_: Any = None) -> Any:
    raise _unsupported("bind_request_context")


def json_response(*args: Any, **kwargs: Any) -> Any:
    raise _unsupported("json_response")


def error_response(*args: Any, **kwargs: Any) -> Any:
    raise _unsupported("error_response")


def file_response(*args: Any, **kwargs: Any) -> Any:
    raise _unsupported("file_response")


def stream_response(*args: Any, **kwargs: Any) -> Any:
    raise _unsupported("stream_response")
