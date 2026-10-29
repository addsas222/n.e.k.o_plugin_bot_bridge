"""异常族（``aiocqhttp.exceptions`` 的子集）。

上游的 ``CQHttpError`` 表示「OneBot 调用失败」，插件经常 ``except CQHttpError`` 静默降级。
shim 只在**真的调用了 OneBot 动作**时才会用到它们 —— 而绝大多数动作在 N.E.K.O. 侧
没有等价实现，那种情况抛的是 :class:`~aiocqhttp.errors.UnsupportedCapabilityError`
（刻意不属于本族，见该类的 docstring）。
"""

from __future__ import annotations

from .errors import UnsupportedCapabilityError

__all__ = [
    "ActionFailed",
    "ApiError",
    "ApiNotAvailable",
    "CQHttpError",
    "UnsupportedCapabilityError",
]


class CQHttpError(Exception):
    """OneBot 调用失败的基类（上游同名）。"""


class ApiError(CQHttpError):
    """API 返回了非 ok 状态。"""

    def __init__(self, action: str = "", message: str = "") -> None:
        super().__init__(f"OneBot API '{action}' failed: {message or 'unknown reason'}")
        self.action = action
        self.message = message


class ActionFailed(CQHttpError):
    """动作执行失败（上游同名；``retcode`` 保留为结构化字段）。"""

    def __init__(self, action: str = "", retcode: int = -1, message: str = "") -> None:
        super().__init__(f"OneBot action '{action}' failed (retcode={retcode}): {message or 'unknown reason'}")
        self.action = action
        self.retcode = retcode
        self.message = message


class ApiNotAvailable(CQHttpError):
    """动作在当前连接上不可用。"""

    def __init__(self, action: str = "") -> None:
        super().__init__(f"OneBot API '{action}' is not available")
        self.action = action
