"""依赖注入的具体参数类型（上游 ``nonebot.internal.params`` 的子集）。

覆盖：``Depends`` / ``DependParam`` / ``BotParam`` / ``EventParam`` / ``StateParam`` /
``MatcherParam`` / ``ArgParam`` / ``DefaultParam`` / ``ExceptionParam``。
"""

from __future__ import annotations

import inspect
from typing import Annotated, Any, Literal, get_args, get_origin

from nonebot.adapters import Bot, Event
from nonebot.compat import PydanticUndefined
from nonebot.consts import ARG_KEY, REJECT_PROMPT_RESULT_KEY
from nonebot.dependencies import Dependent, Param
from nonebot.typing import _STATE_FLAG, T_State, origin_is_annotated
from nonebot.utils import generic_check_issubclass

__all__ = [
    "Arg",
    "ArgInner",
    "ArgParam",
    "ArgPlainText",
    "ArgPromptResult",
    "ArgStr",
    "BotParam",
    "DefaultParam",
    "DependParam",
    "Depends",
    "DependsInner",
    "EventParam",
    "ExceptionParam",
    "MatcherParam",
    "StateParam",
]


class DependsInner:
    """``Depends`` 的标记对象。"""

    def __init__(
        self,
        dependency: Any | None = None,
        *,
        use_cache: bool = True,
        validate: bool = False,
    ) -> None:
        self.dependency = dependency
        self.use_cache = use_cache
        self.validate = validate

    def __repr__(self) -> str:
        name = getattr(self.dependency, "__name__", self.dependency)
        return f"DependsInner({name})"


def Depends(
    dependency: Any | None = None,
    *,
    use_cache: bool = True,
    validate: bool = False,
) -> Any:
    """子依赖装饰器。"""
    return DependsInner(dependency, use_cache=use_cache, validate=validate)


def _annotated_depends(annotation: Any) -> DependsInner | None:
    if get_origin(annotation) is Annotated:
        for extra in reversed(get_args(annotation)[1:]):
            if isinstance(extra, DependsInner):
                return extra
    return None


class DependParam(Param):
    """``Depends`` 注入参数。"""

    def __init__(
        self,
        *,
        dependent: Dependent[Any],
        use_cache: bool = True,
        validate: bool = False,
        **kwargs: Any,
    ) -> None:
        super().__init__(validate=validate, **kwargs)
        self.dependent = dependent
        self.use_cache = use_cache

    def __repr__(self) -> str:
        return f"Depends({self.dependent}, use_cache={self.use_cache})"

    @classmethod
    def _build(cls, inner: DependsInner, type_annotation: Any, allow_types: tuple[type[Param], ...]) -> "DependParam":
        dependency = inner.dependency
        if dependency is None:
            if type_annotation is inspect.Parameter.empty:
                raise ValueError("Dependency cannot be empty")
            dependency = type_annotation
        sub = Dependent.parse(call=dependency, allow_types=allow_types)
        return cls(dependent=sub, use_cache=inner.use_cache, validate=bool(inner.validate))

    @classmethod
    def _check_param(cls, param: inspect.Parameter, allow_types: tuple[type[Param], ...]) -> "Param | None":
        type_annotation = param.annotation
        inner = _annotated_depends(param.annotation)
        if isinstance(param.default, DependsInner):
            inner = param.default
        if inner is None:
            return None
        annotation = type_annotation
        if get_origin(annotation) is Annotated:
            args = get_args(annotation)
            if args:
                annotation = args[0]
        return cls._build(inner, annotation, allow_types)

    @classmethod
    def _check_parameterless(cls, value: Any, allow_types: tuple[type[Param], ...]) -> "Param | None":
        if isinstance(value, DependsInner):
            if value.dependency is None:
                raise ValueError("Dependency cannot be empty")
            return cls._build(value, inspect.Parameter.empty, allow_types)
        return None

    async def _solve(self, dependency_cache: dict[Any, Any] | None = None, **kwargs: Any) -> Any:
        cache = dependency_cache if dependency_cache is not None else {}
        call = self.dependent.call
        if self.use_cache and call in cache:
            return cache[call]
        values = await self.dependent.solve(**{**kwargs, "dependency_cache": cache})
        result = await Dependent._run(call, values)
        if self.use_cache:
            cache[call] = result
        return result


class BotParam(Param):
    """``Bot`` 注入参数。"""

    @classmethod
    def _check_param(cls, param: inspect.Parameter, allow_types: tuple[type[Param], ...]) -> "Param | None":
        if generic_check_issubclass(param.annotation, Bot):
            return cls()
        if param.annotation is param.empty and param.name == "bot":
            return cls()
        return None

    async def _solve(self, bot: Bot, **kwargs: Any) -> Any:
        return bot


class EventParam(Param):
    """``Event`` 注入参数。"""

    @classmethod
    def _check_param(cls, param: inspect.Parameter, allow_types: tuple[type[Param], ...]) -> "Param | None":
        if generic_check_issubclass(param.annotation, Event):
            return cls()
        if param.annotation is param.empty and param.name == "event":
            return cls()
        return None

    async def _solve(self, event: Event, **kwargs: Any) -> Any:
        return event


class StateParam(Param):
    """``T_State`` 注入参数。"""

    @classmethod
    def _check_param(cls, param: inspect.Parameter, allow_types: tuple[type[Param], ...]) -> "Param | None":
        if origin_is_annotated(get_origin(param.annotation)):
            if _STATE_FLAG in get_args(param.annotation):
                return cls()
        if param.annotation is param.empty and param.name == "state":
            return cls()
        return None

    async def _solve(self, state: T_State, **kwargs: Any) -> Any:
        return state


class MatcherParam(Param):
    """``Matcher`` 注入参数。"""

    @classmethod
    def _check_param(cls, param: inspect.Parameter, allow_types: tuple[type[Param], ...]) -> "Param | None":
        from nonebot.matcher import Matcher

        if generic_check_issubclass(param.annotation, Matcher):
            return cls()
        if param.annotation is param.empty and param.name == "matcher":
            return cls()
        return None

    async def _solve(self, matcher: Any, **kwargs: Any) -> Any:
        return matcher


class DefaultParam(Param):
    """默认值注入参数（优先级最低）。"""

    @classmethod
    def _check_param(cls, param: inspect.Parameter, allow_types: tuple[type[Param], ...]) -> "Param | None":
        if param.default != param.empty:
            return cls(default=param.default)
        return None

    async def _solve(self, **kwargs: Any) -> Any:
        return PydanticUndefined


class ExceptionParam(Param):
    """异常注入参数（运行后处理用）。"""

    @classmethod
    def _check_param(cls, param: inspect.Parameter, allow_types: tuple[type[Param], ...]) -> "Param | None":
        if generic_check_issubclass(param.annotation, BaseException):
            return cls()
        if param.annotation is param.empty and param.name == "exception":
            return cls()
        return None

    async def _solve(self, exception: BaseException | None = None, **kwargs: Any) -> Any:
        return exception


class ArgInner:
    """``Arg`` 系列标记对象。"""

    def __init__(self, key: str | None, type: Literal["message", "str", "plaintext", "prompt"]) -> None:
        self.key = key
        self.type = type

    def __repr__(self) -> str:
        return f"ArgInner(key={self.key!r}, type={self.type!r})"


def Arg(key: str | None = None) -> Any:
    """``got`` 参数消息。"""
    return ArgInner(key, "message")


def ArgStr(key: str | None = None) -> str:
    """``got`` 参数消息文本。"""
    return ArgInner(key, "str")  # type: ignore[return-value]


def ArgPlainText(key: str | None = None) -> str:
    """``got`` 参数消息纯文本。"""
    return ArgInner(key, "plaintext")  # type: ignore[return-value]


def ArgPromptResult(key: str | None = None) -> Any:
    """``got`` prompt 发送结果。"""
    return ArgInner(key, "prompt")


class ArgParam(Param):
    """``Arg`` 注入参数。"""

    def __init__(self, *, key: str, type: Literal["message", "str", "plaintext", "prompt"], **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.key = key
        self.type = type

    def __repr__(self) -> str:
        return f"ArgParam(key={self.key!r}, type={self.type!r})"

    @classmethod
    def _check_param(cls, param: inspect.Parameter, allow_types: tuple[type[Param], ...]) -> "Param | None":
        if isinstance(param.default, ArgInner):
            return cls(key=param.default.key or param.name, type=param.default.type)
        if get_origin(param.annotation) is Annotated:
            for extra in reversed(get_args(param.annotation)[1:]):
                if isinstance(extra, ArgInner):
                    return cls(key=extra.key or param.name, type=extra.type)
        return None

    async def _solve(self, matcher: Any, **kwargs: Any) -> Any:
        if self.type == "message":
            return matcher.get_arg(self.key)
        if self.type == "str":
            message = matcher.get_arg(self.key)
            return str(message) if message is not None else None
        if self.type == "plaintext":
            message = matcher.get_arg(self.key)
            return message.extract_plain_text() if message is not None else None
        if self.type == "prompt":
            return matcher.state.get(REJECT_PROMPT_RESULT_KEY.format(key=ARG_KEY.format(key=self.key)))
        raise ValueError(f"Unknown Arg type: {self.type}")
