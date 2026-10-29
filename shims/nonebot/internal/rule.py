"""规则类（上游 ``nonebot.internal.rule`` 的子集）。

偏差：上游用 anyio 并发跑 checker 并用 ``exceptiongroup`` 捕获 ``SkippedException``；
shim 顺序执行，跳过语义相同（任意 checker 抛 ``SkippedException`` 即视为未通过）。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from nonebot.dependencies import Dependent, Param
from nonebot.exception import SkippedException
from nonebot.internal.adapter import Bot, Event
from nonebot.internal.params import BotParam, DefaultParam, DependParam, EventParam, StateParam
from nonebot.typing import T_State

__all__ = ["Rule"]

HANDLER_PARAM_TYPES: tuple[type[Param], ...] = (
    DependParam,
    BotParam,
    EventParam,
    StateParam,
    DefaultParam,
)


class Rule:
    """``Matcher`` 规则类；事件传递时在 ``Matcher`` 运行前检查。

    用法::

        Rule(async_function) & sync_function
    """

    HANDLER_PARAM_TYPES = HANDLER_PARAM_TYPES

    def __init__(self, *checkers: Any) -> None:
        self.checkers: tuple[Dependent[bool], ...] = tuple(
            checker if isinstance(checker, Dependent) else Dependent[bool].parse(call=checker, allow_types=HANDLER_PARAM_TYPES)
            for checker in checkers
        )

    def __repr__(self) -> str:
        return f"Rule({', '.join(repr(checker) for checker in self.checkers)})"

    async def __call__(
        self,
        bot: Bot,
        event: Event,
        state: T_State,
        stack: Any = None,
        dependency_cache: dict[Any, Any] | None = None,
    ) -> bool:
        """检查是否符合所有规则。"""
        if not self.checkers:
            return True
        result = True
        for checker in self.checkers:
            try:
                passed = await checker(
                    bot=bot, event=event, state=state, stack=stack, dependency_cache=dependency_cache
                )
            except SkippedException:
                # 与上游一致：checker 被跳过 => 规则不通过（且不再检查后续 checker）
                return False
            result = result and bool(passed)
        return result

    def __and__(self, other: "Rule | Callable[..., Any] | None") -> "Rule":
        if other is None:
            return self
        if isinstance(other, Rule):
            return Rule(*self.checkers, *other.checkers)
        return Rule(*self.checkers, other)

    def __rand__(self, other: "Rule | Callable[..., Any] | None") -> "Rule":
        if other is None:
            return self
        if isinstance(other, Rule):
            return Rule(*other.checkers, *self.checkers)
        return Rule(other, *self.checkers)

    def __or__(self, other: object) -> "Rule":
        raise RuntimeError("Or operation between rules is not allowed.")

    def __ror__(self, other: object) -> "Rule":
        raise RuntimeError("Or operation between rules is not allowed.")

    def __invert__(self) -> "Rule":
        """取反（**shim 扩展**：上游 ``Rule`` 没有 ``__invert__``，``~rule`` 会抛 TypeError）。"""
        return Rule(_NegatedRule(self))


class _NegatedRule:
    """``~rule`` 的检查器（shim 扩展）。"""

    def __init__(self, rule: Rule) -> None:
        self._rule = rule

    def __repr__(self) -> str:
        return f"~{self._rule!r}"

    async def __call__(
        self,
        bot: Bot,
        event: Event,
        state: T_State,
        stack: Any = None,
        dependency_cache: dict[Any, Any] | None = None,
    ) -> bool:
        return not await self._rule(bot, event, state, stack, dependency_cache)
