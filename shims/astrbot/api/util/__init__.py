"""``astrbot.api.util`` —— 上游的会话等待工具（``session_waiter`` 家族）。

上游签名：

    from astrbot.api.util import SessionController, SessionWaiter, session_waiter

    @session_waiter(timeout=30)
    async def waiter(controller: SessionController, event: AstrMessageEvent): ...

它的语义是「暂停当前事件，等用户接着说下一句，带超时与取消」。N.E.K.O. 的入站是
一次性的（``bridge_run_command`` 进来一条消息、出去一条回复），没有可挂起的事件栈，
所以这里**不能假装能等**：

* 装饰器本身不报错（否则插件在 import 阶段就炸，连别的功能也一起没了）；
* 被装饰的函数**一旦真的被调用**就抛 ``UnsupportedCapabilityError``，并在消息里
  说明「会话等待未映射到 N.E.K.O.」。
"""

from __future__ import annotations

from typing import Any, Callable

from astrbot import UnsupportedCapabilityError

__all__ = ["SessionController", "SessionWaiter", "session_waiter"]

_REASON = (
    "session_waiter 需要可挂起的事件会话；N.E.K.O. 的入站是一次性消息，"
    "没有等价实现。请把多轮交互改成「命令 + 状态存储（PluginKVStoreMixin）」两段式。"
)


class SessionController:
    """上游的会话控制器占位；任何方法调用都显式不支持。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise UnsupportedCapabilityError("astrbot.api.util.SessionController", _REASON)

    def __getattr__(self, name: str) -> Any:  # pragma: no cover — 构造已抛
        raise UnsupportedCapabilityError(f"astrbot.api.util.SessionController.{name}", _REASON)


class SessionWaiter:
    """上游的会话等待器占位；构造即显式不支持。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise UnsupportedCapabilityError("astrbot.api.util.SessionWaiter", _REASON)


def session_waiter(*dargs: Any, **dkwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """上游同名装饰器：装饰期放行，**调用期**显式不支持。"""

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            raise UnsupportedCapabilityError(
                f"astrbot.api.util.session_waiter({getattr(func, '__name__', 'handler')})",
                _REASON,
            )

        wrapper.__name__ = getattr(func, "__name__", "session_waiter")
        wrapper.__doc__ = func.__doc__
        wrapper.__wrapped__ = func  # type: ignore[attr-defined]
        return wrapper

    return decorator
