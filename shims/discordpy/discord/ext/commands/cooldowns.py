"""``discord.ext.commands.cooldowns``：冷却与并发限制。

与上游语义一致（同一命令 + 同一个 bucket 键 → 冷却窗口内的次数限制），但计数在内存里：
N.E.K.O. 没有多进程网关，也就没有跨分片的冷却需求。``BucketType`` 的取值与上游一致。
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from ...errors import MaxConcurrencyReached

__all__ = ["BucketType", "Cooldown", "CooldownMapping", "MaxConcurrency"]


class BucketType(Enum):
    """冷却 / 并发的分桶方式（与上游同名同义）。"""

    default = 0
    user = 1
    guild = 2
    channel = 3
    member = 4
    category = 5
    role = 6

    def get_key(self, msg: Any) -> Any:
        """给一条消息 / 上下文算出桶键（上游 ``BucketType.get_key``）。"""
        if self is BucketType.default:
            return 0
        author = getattr(msg, "author", None)
        guild = getattr(msg, "guild", None)
        channel = getattr(msg, "channel", None)
        if self is BucketType.user:
            return getattr(author, "id", 0)
        if self is BucketType.guild:
            return getattr(guild, "id", 0)
        if self is BucketType.channel:
            return getattr(channel, "id", 0)
        if self is BucketType.member:
            return (getattr(guild, "id", 0), getattr(author, "id", 0))
        if self is BucketType.category:
            return getattr(getattr(channel, "category", None), "id", 0)
        if self is BucketType.role:
            roles = getattr(author, "roles", None) or ()
            top = max(roles, key=lambda role: getattr(role, "position", 0), default=None)
            return getattr(top, "id", 0)
        return 0

    def __str__(self) -> str:
        return self.name


class Cooldown:
    """一次冷却规则（``rate`` 次 / ``per`` 秒，按 ``bucket`` 分桶）。

    用标准的令牌桶实现：满桶 ``rate`` 个令牌，按 ``rate / per`` 的速率回填。
    ``update_rate_limit`` 返回 ``None`` 表示放行，返回秒数表示要等多久（上游同义）。
    """

    def __init__(self, rate: int, per: float, bucket: BucketType = BucketType.default) -> None:
        if rate < 1:
            raise ValueError("rate must be an integer of value 1 or greater")
        self.rate = int(rate)
        self.per = float(per)
        self.type = bucket
        self._tokens = float(rate)
        self._last = time.monotonic()

    def copy(self) -> "Cooldown":
        return Cooldown(self.rate, self.per, self.type)

    def reset(self) -> None:
        self._tokens = float(self.rate)
        self._last = time.monotonic()

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._last)
        if elapsed:
            self._tokens = min(float(self.rate), self._tokens + elapsed * (self.rate / self.per))
            self._last = now

    def get_tokens(self, current: float | None = None) -> int:
        self._refill(time.monotonic() if current is None else current)
        return int(self._tokens)

    def update_rate_limit(self, current: float | None = None) -> float | None:
        """记一次调用；超限时返回还要等多少秒（上游同义）。"""
        now = time.monotonic() if current is None else current
        self._refill(now)
        self._tokens -= 1.0
        if self._tokens >= 0.0:
            return None
        return -self._tokens * (self.per / self.rate)


class CooldownMapping:
    """按桶键分散的 :class:`Cooldown` 表（上游 ``CooldownMapping``）。"""

    def __init__(self, original: Cooldown | None = None) -> None:
        self._cooldown = original
        self._cache: dict[Any, Cooldown] = {}

    @property
    def valid(self) -> bool:
        return self._cooldown is not None

    @classmethod
    def from_cooldown(cls, rate: int, per: float, bucket: BucketType) -> "CooldownMapping":
        return cls(Cooldown(rate, per, bucket))

    def copy(self) -> "CooldownMapping":
        return CooldownMapping(self._cooldown.copy() if self._cooldown is not None else None)

    def get_bucket(self, message: Any, current: float | None = None) -> Cooldown | None:
        if self._cooldown is None:
            return None
        key = self._cooldown.type.get_key(message)
        bucket = self._cache.get(key)
        if bucket is None:
            bucket = self._cooldown.copy()
            self._cache[key] = bucket
        return bucket

    def update_rate_limit(self, message: Any, current: float | None = None) -> float | None:
        bucket = self.get_bucket(message, current)
        return None if bucket is None else bucket.update_rate_limit(current)


class MaxConcurrency:
    """并发上限（``wait=True`` 时排队而不是报错，与上游一致）。"""

    def __init__(self, number: int, *, per: BucketType, wait: bool = False) -> None:
        self.number = int(number)
        self.per = per
        self.wait = bool(wait)
        self._semaphores: dict[Any, Any] = {}

    def copy(self) -> "MaxConcurrency":
        return MaxConcurrency(self.number, per=self.per, wait=self.wait)

    async def acquire(self, message: Any) -> "_Release":
        """拿到名额；``wait=False`` 且名额已满时立刻抛 ``MaxConcurrencyReached``。"""
        import asyncio

        semaphore = self._semaphores.get(self.per.get_key(message))
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.number)
            self._semaphores[self.per.get_key(message)] = semaphore
        if not self.wait and semaphore.locked():
            raise MaxConcurrencyReached(self.number, self.per)
        await semaphore.acquire()
        return _Release(semaphore)

    def release(self, message: Any) -> None:
        """手动释放一个名额（上游同义）。"""
        semaphore = self._semaphores.get(self.per.get_key(message))
        if semaphore is not None and semaphore.locked():
            semaphore.release()


class _Release:
    """``async with`` 用的一次性释放器。"""

    __slots__ = ("_semaphore",)

    def __init__(self, semaphore: Any) -> None:
        self._semaphore = semaphore

    async def __aenter__(self) -> "_Release":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._semaphore.release()
