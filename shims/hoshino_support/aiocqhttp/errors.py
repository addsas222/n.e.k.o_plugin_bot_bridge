"""能力缺失错误。

Hoshino 插件用到的 OneBot / aiocqhttp 接口里，有一大批在 N.E.K.O. 侧**没有等价实现**
（群成员查询、禁言、撤回、加群审批…）。这类调用一律抛 :class:`UnsupportedCapabilityError`，
并带上 capability 名字，由兼容层登记进 ``LoadedPlugin.native["unsupported_calls"]``；
**绝不假装成功**（既不返回空 dict，也不吞掉异常）。

它与上游的 ``CQHttpError`` **不是同一族**：``CQHttpError`` 表示「真正的 OneBot 调用失败了」，
插件通常 ``except CQHttpError`` 静默降级；能力缺失必须响亮地失败，所以这里继承
``NotImplementedError``，让插件作者一眼看出是桥接层的边界而不是网络抖动。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = ["UnsupportedCapabilityError", "install_recorder"]

#: 能力缺失的登记钩子（由 ``_runtime`` 安装；errors 是叶子模块，不反过来 import 运行时）。
_recorder: Callable[[str], None] | None = None


def install_recorder(recorder: Callable[[str], None] | None) -> None:
    """安装"能力缺失"登记钩子（运行时用它把 capability 记进 ``native["unsupported_calls"]``）。"""
    global _recorder
    _recorder = recorder


class UnsupportedCapabilityError(NotImplementedError):
    """插件请求了一项 N.E.K.O. 没有等价实现的能力。"""

    code = "unsupported_capability"

    def __init__(self, capability: str, detail: str = "") -> None:
        message = f"{capability} has no N.E.K.O. equivalent"
        if detail:
            message = f"{message}: {detail}"
        super().__init__(message)
        self.capability = capability
        self.detail = detail
        recorder = _recorder
        if recorder is not None:
            try:
                recorder(self.capability)
            except Exception:  # noqa: BLE001 — 登记失败不能改变错误本身
                pass

    def to_payload(self) -> dict[str, Any]:
        """结构化失败负载（桥接层直接把它塞进返回值）。"""
        return {
            "success": False,
            "code": self.code,
            "capability": self.capability,
            "detail": self.detail,
            "error": str(self),
        }
