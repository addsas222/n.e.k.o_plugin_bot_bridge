"""权限类（上游 ``nonebot.internal.permission`` 的子集）。

偏差：上游用 anyio 并发跑 checker 并用 ``run_coro_with_catch`` 把 ``SkippedException``
折算成 False；shim 顺序执行，语义相同（任一 checker 通过即通过）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nonebot.dependencies import Dependent, Param
from nonebot.exception import SkippedException
from nonebot.internal.adapter import Bot, Event
from nonebot.internal.params import BotParam, DefaultParam, DependParam, EventParam

__all__ = ["Permission", "USER", "User"]

HANDLER_PARAM_TYPES: tuple[type[Param], ...] = (
    DependParam,
    BotParam,
    EventParam,
    DefaultParam,
)


class Permission:
    """``Matcher`` 权限类；事件传递时在 ``Matcher`` 运行前检查。

    用法::

        Permission(async_function) | sync_function
    """

    HANDLER_PARAM_TYPES = HANDLER_PARAM_TYPES

    def __init__(self, *checkers: Any) -> None:
        self.checkers: tuple[Dependent[bool], ...] = tuple(
            checker
            if isinstance(checker, Dependent)
            else Dependent[bool].parse(call=checker, allow_types=HANDLER_PARAM_TYPES)
            for checker in checkers
        )

    def __repr__(self) -> str:
        return f"Permission({', '.join(repr(checker) for checker in self.checkers)})"

    async def __call__(
        self,
        bot: Bot,
        event: Event,
        stack: Any = None,
        dependency_cache: dict[Any, Any] | None = None,
    ) -> bool:
        """检查是否满足某个权限（任一 checker 通过即通过）。"""
        if not self.checkers:
            return True
        for checker in self.checkers:
            try:
                passed = await checker(
                    bot=bot, event=event, stack=stack, dependency_cache=dependency_cache
                )
            except SkippedException:
                passed = False
            if passed:
                return True
        return False

    def __and__(self, other: object) -> "Permission":
        raise RuntimeError("And operation between Permissions is not allowed.")

    def __rand__(self, other: object) -> "Permission":
        raise RuntimeError("And operation between Permissions is not allowed.")

    def __or__(self, other: "Permission | Callable[..., Any] | None") -> "Permission":
        if other is None:
            return self
        if isinstance(other, Permission):
            return Permission(*self.checkers, *other.checkers)
        return Permission(*self.checkers, other)

    def __ror__(self, other: "Permission | Callable[..., Any] | None") -> "Permission":
        if other is None:
            return self
        if isinstance(other, Permission):
            return Permission(*other.checkers, *self.checkers)
        return Permission(other, *self.checkers)

    def __invert__(self) -> "Permission":
        """取反（**shim 扩展**：上游 ``Permission`` 没有 ``__invert__``，``~perm`` 会抛 TypeError）。"""
        return Permission(_NegatedPermission(self))


class _NegatedPermission:
    """``~permission`` 的检查器（shim 扩展）。"""

    def __init__(self, permission: Permission) -> None:
        self._permission = permission

    def __repr__(self) -> str:
        return f"~{self._permission!r}"

    async def __call__(
        self,
        bot: Bot,
        event: Event,
        stack: Any = None,
        dependency_cache: dict[Any, Any] | None = None,
    ) -> bool:
        return not await self._permission(bot, event, stack, dependency_cache)


class User:
    """检查当前事件是否属于指定会话。

    参数:
        users: 会话 ID 元组
        perm: 需同时满足的权限
    """

    def __init__(self, users: tuple[str, ...], perm: Permission | None = None) -> None:
        self.users = users
        self.perm = perm

    def __repr__(self) -> str:
        return f"User(users={self.users}" + (f", permission={self.perm})" if self.perm else "") + ")"

    async def __call__(self, bot: Bot, event: Event) -> bool:
        try:
            session = event.get_session_id()
        except Exception:  # noqa: BLE001 — 事件没有会话概念时视为不匹配（上游同）
            return False
        return bool(session in self.users and (self.perm is None or await self.perm(bot, event)))

    @classmethod
    def _clean_permission(cls, perm: Permission) -> Permission | None:
        if len(perm.checkers) == 1:
            checker = next(iter(perm.checkers)).call
            if isinstance(checker, cls):
                return checker.perm
        return perm

    @classmethod
    def from_event(cls, event: Event, perm: Permission | None = None) -> "User":
        """从事件中获取会话 ID。"""
        try:
            session = event.get_session_id()
        except Exception:  # noqa: BLE001 — 无会话概念时用一个不会匹配的占位值
            session = ""
        return cls((session,), perm=perm and cls._clean_permission(perm))

    @classmethod
    def from_permission(cls, *users: str, perm: Permission | None = None) -> "User":
        """指定会话与权限。"""
        return cls(users, perm=perm and cls._clean_permission(perm))


def USER(*users: str, perm: Permission | None = None) -> Permission:
    """匹配当前事件属于指定会话。"""
    return Permission(User.from_permission(*users, perm=perm))
