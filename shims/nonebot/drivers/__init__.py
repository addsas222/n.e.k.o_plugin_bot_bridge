"""驱动（上游 ``nonebot.drivers`` 的极小子集）。

上游 Driver 负责网络连接（aiohttp / httpx / fastapi / websockets 混入）与生命周期钩子。
N.E.K.O. 桥接层不需要网络驱动：事件的来源是 N.E.K.O. 对话总线，Bot 也是桥接层合成的。
因此 shim 只提供插件真正会用到的部分：

* ``get_driver().config``（``command_start`` / ``command_sep`` / ``superusers`` / 自定义项）
* ``get_driver().on_startup`` / ``on_shutdown`` / ``on_bot_connect`` / ``on_bot_disconnect``
* ``get_driver().bots`` / ``_adapters``

不支持的 Driver 能力（``run`` / ``register_adapter`` 之外的网络接口）会显式抛错。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Any

from nonebot import _runtime

__all__ = ["Config", "Driver", "LoguruHandler", "get_driver"]

DEFAULT_COMMAND_START = frozenset({"/"})
DEFAULT_COMMAND_SEP = frozenset({"."})


class Config:
    """NoneBot 全局配置（上游是 pydantic ``BaseSettings``，会从 .env 读取）。

    shim 直接接收桥接层整理好的字典：``command_start`` / ``command_sep`` /
    ``superusers`` / ``session_expire_timeout`` 有默认值，其余键原样变成属性。
    未定义的键会像 pydantic 一样抛 ``AttributeError``（不会静默返回 None）。
    """

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        data = dict(values or {})
        self.command_start: set[str] = set(data.pop("command_start", None) or DEFAULT_COMMAND_START)
        self.command_sep: set[str] = set(data.pop("command_sep", None) or DEFAULT_COMMAND_SEP)
        self.superusers: set[str] = set(data.pop("superusers", None) or set())
        timeout = data.pop("session_expire_timeout", None)
        self.session_expire_timeout: timedelta = (
            timeout if isinstance(timeout, timedelta) else timedelta(seconds=float(timeout or 120))
        )
        self.log_level: str = str(data.pop("log_level", "INFO"))
        self.driver: str = str(data.pop("driver", "~none"))
        for key, value in data.items():
            setattr(self, key, value)

    def __repr__(self) -> str:
        return f"Config(command_start={sorted(self.command_start)}, command_sep={sorted(self.command_sep)})"

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)

    def dict(self) -> dict[str, Any]:
        """与 pydantic v1 对齐的导出方法。"""
        return {key: value for key, value in vars(self).items()}

    def model_dump(self) -> dict[str, Any]:
        """与 pydantic v2 对齐的导出方法。"""
        return self.dict()


class Driver:
    """驱动基类（只保留桥接层支持的接口）。"""

    def __init__(self, config: Config | Mapping[str, Any] | None = None) -> None:
        self.config: Config = config if isinstance(config, Config) else Config(config)
        self.bots: dict[str, Any] = {}
        self._adapters: dict[str, Any] = {}

    @property
    def type(self) -> str:
        """驱动类型字符串（上游形如 ``~fastapi+~httpx``）。

        桥接层没有网络驱动，这里如实返回配置里的 ``driver`` 值（默认 ``~none``）。
        依赖具体驱动的插件会据此走到它们自己的「驱动不支持」分支，而不是抛
        ``AttributeError`` 把整个插件搞挂 —— 真实案例：``nonebot_plugin_docs`` 读
        ``driver.type`` 来 import 对应的路由实现。
        """
        return str(getattr(self.config, "driver", "~none") or "~none")

    def __repr__(self) -> str:
        return f"Driver(type={self.type!r}, config={self.config!r})"

    # ------------------------------------------------------------ 生命周期
    def on_startup(self, func: Any) -> Any:
        """注册一个启动时执行的函数。"""
        _runtime.record_hook("startup", func)
        return func

    def on_shutdown(self, func: Any) -> Any:
        """注册一个停止时执行的函数。"""
        _runtime.record_hook("shutdown", func)
        return func

    def on_bot_connect(self, func: Any) -> Any:
        """注册一个 Bot 连接时执行的函数。"""
        _runtime.record_hook("bot_connect", func)
        return func

    def on_bot_disconnect(self, func: Any) -> Any:
        """注册一个 Bot 断开时执行的函数。"""
        _runtime.record_hook("bot_disconnect", func)
        return func

    # -------------------------------------------------------------- 适配器
    def register_adapter(self, name: str, adapter: Any) -> None:
        self._adapters[name] = adapter

    def get_adapter(self, name: str) -> Any:
        if name not in self._adapters:
            raise ValueError(f"Adapter {name} not registered.")
        return self._adapters[name]

    async def run(self, *args: Any, **kwargs: Any) -> None:
        """上游负责把事件循环跑起来；桥接层的事件循环由 N.E.K.O. 提供。"""
        raise RuntimeError("nonebot driver.run() is not supported inside the N.E.K.O. bridge")


class LoguruHandler:
    """兼容占位：上游把标准库 logging 转发到 loguru；shim 的日志已统一转发给宿主。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


def get_driver() -> Driver:
    """获取全局 Driver 实例（由桥接层在加载插件前绑定）。"""
    driver = _runtime.driver_or_none()
    if driver is None:
        raise ValueError("NoneBot has not been initialized.")
    return driver
