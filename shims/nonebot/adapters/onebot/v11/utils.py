"""OneBot v11 杂项（上游 ``onebot.v11.utils`` 的子集，函数语义完全一致）。"""

from __future__ import annotations

from typing import Any

from nonebot.utils import logger_wrapper

from .exception import ActionFailed

__all__ = ["b2s", "escape", "f2s", "handle_api_result", "log", "unescape"]

log = logger_wrapper("OneBot V11")


def escape(s: str, *, escape_comma: bool = True) -> str:
    """对字符串进行 CQ 码转义。"""
    s = s.replace("&", "&amp;").replace("[", "&#91;").replace("]", "&#93;")
    if escape_comma:
        s = s.replace(",", "&#44;")
    return s


def unescape(s: str) -> str:
    """对字符串进行 CQ 码去转义。"""
    return s.replace("&#44;", ",").replace("&#91;", "[").replace("&#93;", "]").replace("&amp;", "&")


def b2s(b: bool | None) -> str | None:
    """把布尔值转成 OneBot 协议里的字符串形式。"""
    if b is None:
        return None
    return "true" if b else "false"


def f2s(file: Any) -> str:
    """把文件对象转成 OneBot 协议里的 ``file`` 参数（``base64://`` 形式）。"""
    import base64
    from io import BytesIO
    from pathlib import Path

    if isinstance(file, str):
        return file
    if isinstance(file, Path):
        file = file.read_bytes()
    if isinstance(file, BytesIO):
        file = file.getvalue()
    if isinstance(file, (bytes, bytearray)):
        return f"base64://{base64.b64encode(bytes(file)).decode()}"
    raise TypeError(f"Unsupported file type: {type(file)}")


def handle_api_result(result: dict[str, Any] | None) -> Any:
    """处理 API 请求返回值。

    异常:
        ActionFailed: API 调用失败
    """
    if isinstance(result, dict):
        if result.get("status") == "failed":
            raise ActionFailed(**result)
        return result.get("data")
    return None
