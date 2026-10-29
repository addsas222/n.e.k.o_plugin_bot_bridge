"""OneBot v11 错误类型（上游 ``onebot.v11.exception`` 的子集，字段与方法一致）。"""

from __future__ import annotations

from nonebot.exception import ActionFailed as BaseActionFailed
from nonebot.exception import AdapterException
from nonebot.exception import ApiNotAvailable as BaseApiNotAvailable
from nonebot.exception import NetworkError as BaseNetworkError
from nonebot.exception import NoLogException as BaseNoLogException

__all__ = [
    "ActionFailed",
    "ApiNotAvailable",
    "NetworkError",
    "NoLogException",
    "OneBotV11AdapterException",
]


class OneBotV11AdapterException(AdapterException):
    def __init__(self) -> None:
        super().__init__("OneBot V11")


class NoLogException(BaseNoLogException, OneBotV11AdapterException):
    """指示 NoneBot 隐藏该事件的日志（shim 扩展：可以带一条说明）。"""

    def __init__(self, msg: str | None = None) -> None:
        self.msg: str | None = msg
        if msg:
            AdapterException.__init__(self, "OneBot V11", msg)
        else:
            AdapterException.__init__(self, "OneBot V11")

    def __repr__(self) -> str:
        return f"NoLogException(message={self.msg!r})" if self.msg else "NoLogException()"


class NetworkError(BaseNetworkError, OneBotV11AdapterException):
    """网络错误。"""

    def __init__(self, msg: str | None = None) -> None:
        super().__init__()
        self.msg: str | None = msg
        """错误原因"""

    def __repr__(self) -> str:
        return f"NetWorkError(message={self.msg!r})"


class ApiNotAvailable(BaseApiNotAvailable, OneBotV11AdapterException):
    """在 API 连接不可用时抛出。

    shim 扩展：允许附带一条说明（上游只能无参构造，因为 MRO 里
    ``OneBotV11AdapterException.__init__`` 先于 ``AdapterException.__init__`` 被解析到）。
    """

    def __init__(self, msg: str | None = None) -> None:
        self.msg: str | None = msg
        if msg:
            AdapterException.__init__(self, "OneBot V11", msg)
        else:
            AdapterException.__init__(self, "OneBot V11")

    def __repr__(self) -> str:
        return f"ApiNotAvailable(message={self.msg!r})" if self.msg else "ApiNotAvailable()"


class ActionFailed(BaseActionFailed, OneBotV11AdapterException):
    """API 请求返回错误信息。

    参数:
        kwargs: 协议端返回的错误信息
    """

    def __init__(self, **kwargs: object) -> None:
        super().__init__()
        self.info = kwargs
        """所有错误信息"""

    def __repr__(self) -> str:
        return "ActionFailed(" + ", ".join(f"{k}={v!r}" for k, v in self.info.items()) + ")"
