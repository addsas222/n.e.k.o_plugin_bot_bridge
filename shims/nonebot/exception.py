"""本模块包含了 NoneBot 运行时可能抛出的异常（上游 ``nonebot.exception`` 的子集）。

只有 ``TypeMisMatch`` / ``MockApiException`` 携带的 ``ModelField`` 类型来自 pydantic，
shim 用 :mod:`nonebot.compat` 里的轻量替身表示，行为等价（见包文档偏差清单）。
"""

from __future__ import annotations

from typing import Any

from nonebot.compat import ModelField


class NoneBotException(Exception):
    """所有 NoneBot 发生的异常基类。"""

    def __str__(self) -> str:
        return self.__repr__()


# Rule Exception
class ParserExit(NoneBotException):
    """``nonebot.rule.shell_command`` 处理消息失败时返回的异常。"""

    def __init__(self, status: int = 0, message: str | None = None) -> None:
        self.status = status
        self.message = message

    def __repr__(self) -> str:
        return (
            f"ParserExit(status={self.status}"
            + (f", message={self.message!r}" if self.message else "")
            + ")"
        )


# Processor Exception
class ProcessException(NoneBotException):
    """事件处理过程中发生的异常基类。"""


class IgnoredException(ProcessException):
    """指示 NoneBot 应该忽略该事件。"""

    def __init__(self, reason: Any) -> None:
        self.reason: Any = reason

    def __repr__(self) -> str:
        return f"IgnoredException(reason={self.reason!r})"


class SkippedException(ProcessException):
    """指示 NoneBot 立即结束当前 ``Dependent`` 的运行。"""


class TypeMisMatch(SkippedException):
    """当前 ``Handler`` 的参数类型不匹配。"""

    def __init__(self, param: ModelField, value: Any) -> None:
        self.param: ModelField = param
        self.value: Any = value

    def __repr__(self) -> str:
        return f"TypeMisMatch(param={self.param.name}, value={self.value!r})"


class MockApiException(ProcessException):
    """指示 NoneBot 阻止本次 API 调用或修改本次调用返回值。"""

    def __init__(self, result: Any) -> None:
        self.result = result

    def __repr__(self) -> str:
        return f"MockApiException(result={self.result!r})"


class StopPropagation(ProcessException):
    """指示 NoneBot 终止事件向下层传播。"""


# Matcher Exceptions
class MatcherException(NoneBotException):
    """所有 Matcher 发生的异常基类。"""


class PausedException(MatcherException):
    """指示 NoneBot 结束当前 ``Handler`` 并等待下一条消息后继续下一个 ``Handler``。"""


class RejectedException(MatcherException):
    """指示 NoneBot 结束当前 ``Handler`` 并等待下一条消息后重新运行当前 ``Handler``。"""


class FinishedException(MatcherException):
    """指示 NoneBot 结束当前 ``Handler`` 且后续 ``Handler`` 不再被运行。"""


# Adapter Exceptions
class AdapterException(NoneBotException):
    """代表 ``Adapter`` 抛出的异常。"""

    def __init__(self, adapter_name: str, *args: object) -> None:
        super().__init__(*args)
        self.adapter_name: str = adapter_name


class NoLogException(AdapterException):
    """指示 NoneBot 对当前 ``Event`` 进行处理但不显示 Log 信息。"""


class ApiNotAvailable(AdapterException):
    """在 API 连接不可用时抛出。"""


class NetworkError(AdapterException):
    """在网络出现问题时抛出。"""


class ActionFailed(AdapterException):
    """API 请求成功返回数据，但 API 操作失败。"""


# Driver Exceptions
class DriverException(NoneBotException):
    """``Driver`` 抛出的异常基类。"""


class WebSocketClosed(DriverException):
    """WebSocket 连接已关闭。"""

    def __init__(self, code: int, reason: str | None = None) -> None:
        self.code = code
        self.reason = reason

    def __repr__(self) -> str:
        return (
            f"WebSocketClosed(code={self.code}"
            + (f", reason={self.reason!r}" if self.reason else "")
            + ")"
        )


__all__ = [
    "ActionFailed",
    "AdapterException",
    "ApiNotAvailable",
    "DriverException",
    "FinishedException",
    "IgnoredException",
    "MatcherException",
    "MockApiException",
    "NetworkError",
    "NoLogException",
    "NoneBotException",
    "ParserExit",
    "PausedException",
    "ProcessException",
    "RejectedException",
    "SkippedException",
    "StopPropagation",
    "TypeMisMatch",
    "WebSocketClosed",
]
