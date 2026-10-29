"""OneBot v12 错误类型（上游 ``onebot.v12.exception`` 的子集，字段与方法一致）。"""

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
    "OneBotV12AdapterException",
]


class OneBotV12AdapterException(AdapterException):
    def __init__(self) -> None:
        super().__init__("OneBot V12")


class NoLogException(BaseNoLogException, OneBotV12AdapterException):
    """指示 NoneBot 隐藏该事件的日志。"""

    def __init__(self, msg: str | None = None) -> None:
        self.msg: str | None = msg
        if msg:
            AdapterException.__init__(self, "OneBot V12", msg)
        else:
            AdapterException.__init__(self, "OneBot V12")

    def __repr__(self) -> str:
        return f"NoLogException(message={self.msg!r})" if self.msg else "NoLogException()"


class NetworkError(BaseNetworkError, OneBotV12AdapterException):
    """网络错误。"""

    def __init__(self, msg: str | None = None) -> None:
        super().__init__()
        self.msg: str | None = msg

    def __repr__(self) -> str:
        return f"NetWorkError(message={self.msg!r})"


class ApiNotAvailable(BaseApiNotAvailable, OneBotV12AdapterException):
    """API 连接不可用（桥接层里除发消息外一律如此）。"""

    def __init__(self, msg: str | None = None) -> None:
        self.msg: str | None = msg
        if msg:
            AdapterException.__init__(self, "OneBot V12", msg)
        else:
            AdapterException.__init__(self, "OneBot V12")

    def __repr__(self) -> str:
        return f"ApiNotAvailable(message={self.msg!r})" if self.msg else "ApiNotAvailable()"


class ActionFailed(BaseActionFailed, OneBotV12AdapterException):
    """API 请求返回错误信息（v12 的字段是 ``retcode`` / ``message`` / ``data``）。"""

    def __init__(self, **kwargs: object) -> None:
        super().__init__()
        self.info = kwargs
        self.retcode = kwargs.get("retcode")
        self.message = kwargs.get("message", "")

    def __repr__(self) -> str:
        return "ActionFailed(" + ", ".join(f"{k}={v!r}" for k, v in self.info.items()) + ")"
