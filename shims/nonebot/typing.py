"""类型定义（上游 ``nonebot.typing`` 的子集）。

``T_State`` 与上游一样是 ``Annotated[dict, _STATE_FLAG]`` —— 依赖注入靠这个标记
把「状态参数」和「普通 dict 参数」区分开。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, get_origin


class StateFlag:
    """``T_State`` 的标记类型。"""

    def __repr__(self) -> str:
        return "StateFlag()"


_STATE_FLAG = StateFlag()

T_State = Annotated[dict[Any, Any], _STATE_FLAG]
"""事件处理状态（会话状态）。"""

T_Handler = Callable[..., Any]
"""事件处理函数。"""

T_RuleChecker = Callable[..., Any]
"""规则检查函数。"""

T_PermissionChecker = Callable[..., Any]
"""权限检查函数。"""

_DependentCallable = Callable[..., Any]

T_DependencyCache = dict[_DependentCallable, Any]
"""子依赖缓存。"""

T_TypeUpdater = Callable[..., Any]
T_PermissionUpdater = Callable[..., Any]
T_EventPreProcessor = Callable[..., Any]
T_EventPostProcessor = Callable[..., Any]
T_RunPreProcessor = Callable[..., Any]
T_RunPostProcessor = Callable[..., Any]
T_CallingAPIHook = Callable[..., Any]
T_CalledAPIHook = Callable[..., Any]


def origin_is_annotated(origin: type[Any] | None) -> bool:
    """判断 ``typing.get_origin()`` 的结果是否表示 ``Annotated``。"""
    if origin is None:
        return False
    if origin is Annotated:
        return True
    try:
        return get_origin(origin) is Annotated
    except TypeError:
        return False


__all__ = [
    "T_CalledAPIHook",
    "T_CallingAPIHook",
    "T_DependencyCache",
    "T_EventPostProcessor",
    "T_EventPreProcessor",
    "T_Handler",
    "T_PermissionChecker",
    "T_PermissionUpdater",
    "T_RuleChecker",
    "T_RunPostProcessor",
    "T_RunPreProcessor",
    "T_State",
    "T_TypeUpdater",
    "origin_is_annotated",
]
