"""``astrbot.core.platform.register`` —— 平台适配器注册装饰器。

上游：

    @register_platform_adapter("telegram", "Telegram 适配器")
    class TelegramAdapter(Platform): ...

N.E.K.O. 不加载第三方平台适配器（它自己的对话通道是唯一的入站口），所以这里
**装饰期放行、登记为未支持**：插件能正常加载（否则整包 import 就废了），但适配器
永远不会被实例化。真要取用适配器能力时，``Context`` 上的对应方法会显式失败。
"""

from __future__ import annotations

from typing import Any, Callable, TypeVar

from astrbot import _runtime as RUNTIME

__all__ = ["register_platform_adapter"]

_T = TypeVar("_T", bound=type)


def register_platform_adapter(*args: Any, **kwargs: Any) -> Callable[[_T], _T]:
    """登记一个平台适配器（装饰器）。返回值原样透传被装饰的类。"""

    adapter_name = str(args[0]) if args else str(kwargs.get("adapter_name") or "unknown")

    def decorator(cls: _T) -> _T:
        RUNTIME.record_unsupported(
            getattr(cls, "__name__", "PlatformAdapter"),
            f"platform_adapter:{adapter_name}（N.E.K.O. 不加载第三方平台适配器）",
        )
        return cls

    return decorator
