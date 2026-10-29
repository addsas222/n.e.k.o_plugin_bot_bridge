"""``discord.ext.tasks`` —— 后台定时循环（``@tasks.loop``）。

上游这是 discord.py 的官方扩展：把一个协程包成按固定间隔重复执行的任务，配合
``before_loop`` / ``after_loop`` / ``error`` 钩子。它**不依赖 Discord 网关**，
所以在桥接层里可以真跑 —— 这里用 ``asyncio`` 实现同一套语义：

* ``start()`` 起一个后台 task；``stop()`` / ``cancel()`` 取消；
* ``seconds`` / ``minutes`` / ``hours`` 决定间隔，``count`` 限定次数；
* ``reconnect`` 只在等网关重连时有意义，保留参数、行为等价于 True；
* ``change_interval()`` 可在运行中改间隔（下一轮生效）。

与上游的差异：没有 ``reconnect`` 的网关语义（本来也没有网关），其余同名同签名。
"""

from __future__ import annotations

import asyncio
import datetime
import inspect
from typing import Any, Callable

__all__ = ["Loop", "loop"]


class Loop:
    """包装一个协程并按间隔重复执行（对应上游 ``discord.ext.tasks.Loop``）。"""

    def __init__(
        self,
        coro: Callable[..., Any],
        *,
        seconds: float | None = None,
        minutes: float | None = None,
        hours: float | None = None,
        count: int | None = None,
        reconnect: bool = True,
    ) -> None:
        if not callable(coro):
            raise TypeError("tasks.loop 需要装饰一个协程函数")
        self.coro = coro
        self.reconnect = bool(reconnect)
        self.count = count
        self._seconds = float(seconds or 0) or (
            float(minutes or 0) * 60.0 + float(hours or 0) * 3600.0
        ) or 0.0
        self._task: asyncio.Task[Any] | None = None
        self._before_loop: Callable[..., Any] | None = None
        self._after_loop: Callable[..., Any] | None = None
        self._error: Callable[..., Any] | None = None
        self._cancelled = False
        self._failed = False
        self._current = 0
        self._last_exception: BaseException | None = None
        self._next_iteration: datetime.datetime | None = None

    # -- 钩子 -------------------------------------------------------------- #
    def before_loop(self, coro: Callable[..., Any]) -> Callable[..., Any]:
        self._before_loop = coro
        return coro

    def after_loop(self, coro: Callable[..., Any]) -> Callable[..., Any]:
        self._after_loop = coro
        return coro

    def error(self, coro: Callable[..., Any]) -> Callable[..., Any]:
        self._error = coro
        return coro

    # -- 运行控制 ---------------------------------------------------------- #
    @property
    def seconds(self) -> float | None:
        return self._seconds or None

    def update(self, **kwargs: Any) -> None:
        if "seconds" in kwargs:
            self._seconds = float(kwargs["seconds"] or 0)
        if "minutes" in kwargs:
            self._seconds = float(kwargs["minutes"] or 0) * 60.0
        if "hours" in kwargs:
            self._seconds = float(kwargs["hours"] or 0) * 3600.0
        if "count" in kwargs:
            self.count = kwargs["count"]

    def change_interval(self, *, seconds: float | None = None, minutes: float | None = None, hours: float | None = None) -> None:
        """运行中改间隔（下一轮生效）。"""
        if seconds is not None:
            self._seconds = float(seconds)
        elif minutes is not None:
            self._seconds = float(minutes) * 60.0
        elif hours is not None:
            self._seconds = float(hours) * 3600.0
        else:
            raise ValueError("change_interval 需要 seconds / minutes / hours 之一")

    def start(self, *args: Any, **kwargs: Any) -> asyncio.Task[Any]:
        if self._task is not None and not self._task.done():
            raise RuntimeError("这个 Loop 已经在跑了；先 stop() 再 start()")
        self._cancelled = False
        self._failed = False
        self._current = 0
        self._task = asyncio.get_event_loop().create_task(self._run(*args, **kwargs))
        return self._task

    def stop(self) -> None:
        self._cancelled = True
        if self._task is not None and not self._task.done():
            self._task.cancel()

    def cancel(self) -> None:
        self.stop()

    def restart(self, *args: Any, **kwargs: Any) -> None:
        self.stop()
        self.start(*args, **kwargs)

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def is_being_cancelled(self) -> bool:
        return self._cancelled

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def current_loop(self) -> int:
        return self._current

    @property
    def next_iteration(self) -> datetime.datetime | None:
        return self._next_iteration

    def get_task(self) -> asyncio.Task[Any] | None:
        return self._task

    # -- 执行体 ------------------------------------------------------------ #
    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """直接 ``await my_loop()`` 时按普通协程调用（上游同义）。"""
        return await self.coro(*args, **kwargs)

    async def _maybe_await(self, func: Callable[..., Any], *args: Any) -> Any:
        result = func(*args)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _run(self, *args: Any, **kwargs: Any) -> None:
        try:
            if self._before_loop is not None:
                await self._maybe_await(self._before_loop)
            while True:
                if self.count is not None and self._current >= self.count:
                    break
                if self._seconds:
                    self._next_iteration = datetime.datetime.now() + datetime.timedelta(
                        seconds=self._seconds
                    )
                    await asyncio.sleep(self._seconds)
                else:
                    # 间隔为 0：让出一次事件循环，避免忙等把宿主拖死
                    await asyncio.sleep(0)
                if self._cancelled:
                    break
                try:
                    await self.coro(*args, **kwargs)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — 上游把异常交给 error 钩子
                    self._failed = True
                    self._last_exception = exc
                    if self._error is None:
                        raise
                    await self._maybe_await(self._error, exc)
                    break
                self._current += 1
        except asyncio.CancelledError:
            pass
        finally:
            if self._after_loop is not None and not self._failed:
                try:
                    await self._maybe_await(self._after_loop)
                except Exception:  # noqa: BLE001 — after_loop 失败不该再抛
                    pass


def loop(
    *,
    seconds: float | None = None,
    minutes: float | None = None,
    hours: float | None = None,
    count: int | None = None,
    reconnect: bool = True,
) -> Callable[[Callable[..., Any]], Loop]:
    """``@tasks.loop(seconds=30)`` 装饰器（上游同签名）。"""

    def decorator(coro: Callable[..., Any]) -> Loop:
        return Loop(
            coro,
            seconds=seconds,
            minutes=minutes,
            hours=hours,
            count=count,
            reconnect=reconnect,
        )

    return decorator
