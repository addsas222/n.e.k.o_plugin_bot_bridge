"""事件响应器的创建与运行（上游 ``nonebot.matcher`` / ``nonebot.internal.matcher`` 的子集）。

本模块是 shim 的核心，覆盖 ``Matcher`` / ``matchers`` / ``MatcherManager`` /
``MatcherSource`` / ``current_*`` 上下文变量。

与上游的实现差异：

* 上游 ``matchers`` 的存储器是 ``MatcherProvider``（插件化存储器），shim 用 ``defaultdict``；
* 上游用 anyio / exceptiongroup 并发跑 handler，shim 顺序执行；
* 上游 ``Rule.__or__`` 视情况抛错，``Matcher.run`` 用 ``StopPropagation`` 异常组，shim 直接按返回值处理；
* pause / reject 后创建临时响应器（优先级 0、按会话过滤）的行为与上游一致，
  但过期检查在分发时进行（上游用 ``expire_time`` + 定时清理）。
"""

from __future__ import annotations

import inspect
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import ModuleType
from typing import Any, ClassVar, Iterator, NoReturn, TypeVar, overload

from nonebot import _runtime
from nonebot.consts import (
    ARG_KEY,
    LAST_RECEIVE_KEY,
    PAUSE_PROMPT_RESULT_KEY,
    RECEIVE_KEY,
    REJECT_CACHE_TARGET,
    REJECT_PROMPT_RESULT_KEY,
    REJECT_TARGET,
)
from nonebot.dependencies import Dependent
from nonebot.exception import (
    FinishedException,
    ParserExit,
    PausedException,
    RejectedException,
    SkippedException,
    StopPropagation,
)
from nonebot.internal.adapter import Bot, Event, Message, MessageSegment, MessageTemplate
from nonebot.internal.params import (
    ArgParam,
    BotParam,
    DefaultParam,
    DependParam,
    Depends,
    EventParam,
    MatcherParam,
    StateParam,
)
from nonebot.internal.permission import Permission, User
from nonebot.internal.rule import Rule
from nonebot.log import logger
from nonebot.typing import T_DependencyCache, T_Handler, T_PermissionUpdater, T_State, T_TypeUpdater
from nonebot.utils import classproperty

__all__ = [
    "DEFAULT_PROVIDER_CLASS",
    "Matcher",
    "MatcherManager",
    "MatcherProvider",
    "MatcherSource",
    "Trigger",
    "current_bot",
    "current_event",
    "current_handler",
    "current_matcher",
    "matchers",
]

T = TypeVar("T")

current_bot: ContextVar[Bot] = ContextVar("current_bot")
current_event: ContextVar[Event] = ContextVar("current_event")
current_matcher: ContextVar["Matcher"] = ContextVar("current_matcher")
current_handler: ContextVar[Dependent[Any]] = ContextVar("current_handler")


@dataclass
class Trigger:
    """shim 扩展：记录 ``on_*`` 快捷方式声明的触发形式，供桥接层生成 ``CommandSpec``。"""

    kind: str = ""
    """触发类型：``command`` / ``message`` / ``keyword`` / ``startswith`` / ``endswith`` / ``fullmatch`` / ``regex`` / ``notice`` / ``request`` / ``meta_event`` / ``session``"""
    patterns: tuple[str, ...] = ()
    """框架原生的触发形式（命令文本 / 正则 / 关键词……），仅用于展示与适配器自检。"""
    label: str = ""
    """人类可读的标签，例如 ``command:/hello``。"""


@dataclass
class MatcherSource:
    """Matcher 源代码上下文信息。"""

    plugin_id: str | None = None
    """事件响应器所在插件标识符"""
    module_name: str | None = None
    """事件响应器所在模块的路径名"""
    lineno: int | None = None
    """事件响应器所在行号"""

    @property
    def plugin(self) -> Any:
        """事件响应器所在插件。"""
        from nonebot.plugin import get_plugin

        if self.plugin_id is not None:
            return get_plugin(self.plugin_id)
        return None

    @property
    def plugin_name(self) -> str | None:
        """事件响应器所在插件名。"""
        plugin = self.plugin
        return getattr(plugin, "name", None) if plugin is not None else None

    @property
    def module(self) -> ModuleType | None:
        import sys

        if self.module_name is not None:
            return sys.modules.get(self.module_name)
        return None

    @property
    def file(self) -> Any:
        module = self.module
        if module is None:
            return None
        try:
            source = inspect.getsourcefile(module)
        except TypeError:
            return None
        from pathlib import Path

        return Path(source).absolute() if source else None


class MatcherProvider(defaultdict):  # type: ignore[type-arg]
    """事件响应器存储器（shim 用 ``defaultdict[int, list[type[Matcher]]]``）。"""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(list, *args, **kwargs)


DEFAULT_PROVIDER_CLASS = MatcherProvider
"""默认存储器类型。"""


class MatcherManager(MatcherProvider):
    """事件响应器管理器（``dict[int, list[type[Matcher]]]``）。"""


matchers = MatcherManager()


class MatcherMeta(type):
    def __repr__(cls) -> str:
        module = getattr(cls, "module_name", None)
        return f"{cls.__name__}(type={cls.type!r}" + (f", module={module}" if module else "") + ")"


class Matcher(metaclass=MatcherMeta):
    """事件响应器类。"""

    _source: ClassVar[MatcherSource | None] = None
    _trigger: ClassVar[Trigger | None] = None

    type: ClassVar[str] = ""
    """事件响应器类型"""
    rule: ClassVar[Rule] = Rule()
    """事件响应器匹配规则"""
    permission: ClassVar[Permission] = Permission()
    """事件响应器触发权限"""
    handlers: ClassVar[list[Dependent[Any]]] = []
    """事件响应器拥有的事件处理函数列表"""
    priority: ClassVar[int] = 1
    """事件响应器优先级"""
    block: ClassVar[bool] = False
    """事件响应器是否阻止事件传播"""
    temp: ClassVar[bool] = False
    """事件响应器是否为临时"""
    expire_time: ClassVar[datetime | None] = None
    """事件响应器过期时间点"""

    _default_state: ClassVar[T_State] = {}
    """事件响应器默认状态"""

    _default_type_updater: ClassVar[Dependent[str] | None] = None
    _default_permission_updater: ClassVar[Dependent[Permission] | None] = None

    HANDLER_PARAM_TYPES: ClassVar[tuple[type[Any], ...]] = (
        DependParam,
        BotParam,
        EventParam,
        StateParam,
        ArgParam,
        MatcherParam,
        DefaultParam,
    )

    def __init__(self) -> None:
        self.remain_handlers: list[Dependent[Any]] = list(self.handlers)
        self.state: T_State = dict(self._default_state)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(type={self.type!r})"

    # ------------------------------------------------------------------ 创建
    @classmethod
    def new(
        cls,
        type_: str = "",
        rule: Rule | None = None,
        permission: Permission | None = None,
        handlers: list[T_Handler | Dependent[Any]] | None = None,
        temp: bool = False,
        priority: int = 1,
        block: bool = False,
        *,
        plugin: Any | None = None,
        module: ModuleType | None = None,
        source: MatcherSource | None = None,
        expire_time: datetime | timedelta | None = None,
        default_state: T_State | None = None,
        default_type_updater: T_TypeUpdater | Dependent[str] | None = None,
        default_permission_updater: T_PermissionUpdater | Dependent[Permission] | None = None,
    ) -> type["Matcher"]:
        """创建一个新的事件响应器，并存储至 :data:`matchers`。"""
        if source is None and (plugin is not None or module is not None):
            source = MatcherSource(
                plugin_id=getattr(plugin, "id_", None),
                module_name=getattr(module, "__name__", None),
            )
        if source is None:
            source = cls._current_source()

        resolved_rule = rule or Rule()
        resolved_permission = permission or Permission()
        parsed_handlers: list[Dependent[Any]] = [
            handler if isinstance(handler, Dependent) else Dependent[Any].parse(call=handler, allow_types=cls.HANDLER_PARAM_TYPES)
            for handler in (handlers or [])
        ]

        new_matcher = type(
            cls.__name__,
            (cls,),
            {
                "_source": source,
                "_trigger": cls._trigger,
                "type": type_,
                "rule": resolved_rule,
                "permission": resolved_permission,
                "handlers": parsed_handlers,
                "temp": temp,
                "expire_time": (
                    expire_time
                    if isinstance(expire_time, datetime)
                    else (datetime.now() + expire_time if expire_time is not None else None)
                ),
                "priority": priority,
                "block": block,
                "_default_state": default_state or {},
                "_default_type_updater": (
                    default_type_updater
                    if isinstance(default_type_updater, Dependent)
                    else (
                        Dependent[str].parse(call=default_type_updater, allow_types=cls.HANDLER_PARAM_TYPES)
                        if default_type_updater
                        else None
                    )
                ),
                "_default_permission_updater": (
                    default_permission_updater
                    if isinstance(default_permission_updater, Dependent)
                    else (
                        Dependent[Permission].parse(call=default_permission_updater, allow_types=cls.HANDLER_PARAM_TYPES)
                        if default_permission_updater
                        else None
                    )
                ),
            },
        )

        matchers[priority].append(new_matcher)
        if source is not None and source.plugin_id:
            _runtime.register_session_matcher(source.plugin_id, new_matcher)
        else:
            _runtime.register_matcher(new_matcher)
        logger.trace(f"Define new matcher {new_matcher}")
        return new_matcher  # type: ignore[return-value]

    @classmethod
    def _current_source(cls) -> MatcherSource | None:
        """从桥接运行时的导入上下文推断源码位置（shim 扩展）。"""
        record = _runtime.current_plugin()
        if record is None:
            return None
        return MatcherSource(plugin_id=record.plugin_id, module_name=record.module_name)

    @classmethod
    def destroy(cls) -> None:
        """销毁当前的事件响应器。"""
        bucket = matchers.get(cls.priority)
        if bucket and cls in bucket:
            bucket.remove(cls)

    @classmethod
    def set_trigger(cls, trigger: Trigger) -> None:
        """设置触发描述（shim 扩展，``on_*`` 快捷方式使用）。"""
        cls._trigger = trigger

    # ------------------------------------------------------------ 插件上下文
    @classproperty
    def plugin(cls) -> Any:
        source = cls._source
        return source.plugin if source else None

    @classproperty
    def plugin_id(cls) -> str | None:
        source = cls._source
        return source.plugin_id if source else None

    @classproperty
    def plugin_name(cls) -> str | None:
        source = cls._source
        return source.plugin_name if source else None

    @classproperty
    def module(cls) -> ModuleType | None:
        source = cls._source
        return source.module if source else None

    @classproperty
    def module_name(cls) -> str | None:
        source = cls._source
        return source.module_name if source else None

    # ---------------------------------------------------------------- 检查
    @classmethod
    async def check_perm(
        cls,
        bot: Bot,
        event: Event,
        stack: Any = None,
        dependency_cache: T_DependencyCache | None = None,
    ) -> bool:
        """检查是否满足触发权限。"""
        event_type = event.get_type()
        return event_type == (cls.type or event_type) and await cls.permission(bot, event, stack, dependency_cache)

    @classmethod
    async def check_rule(
        cls,
        bot: Bot,
        event: Event,
        state: T_State,
        stack: Any = None,
        dependency_cache: T_DependencyCache | None = None,
    ) -> bool:
        """检查是否满足匹配规则。"""
        event_type = event.get_type()
        return event_type == (cls.type or event_type) and await cls.rule(bot, event, state, stack, dependency_cache)

    # -------------------------------------------------------- 更新函数注册
    @classmethod
    def type_updater(cls, func: T_TypeUpdater) -> T_TypeUpdater:
        """装饰一个函数来更改当前事件响应器的默认响应事件类型更新函数。"""
        cls._default_type_updater = Dependent[str].parse(call=func, allow_types=cls.HANDLER_PARAM_TYPES)
        return func

    @classmethod
    def permission_updater(cls, func: T_PermissionUpdater) -> T_PermissionUpdater:
        """装饰一个函数来更改当前事件响应器的默认会话权限更新函数。"""
        cls._default_permission_updater = Dependent[Permission].parse(call=func, allow_types=cls.HANDLER_PARAM_TYPES)
        return func

    # ------------------------------------------------------------ 添加 handler
    @classmethod
    def append_handler(cls, handler: T_Handler, parameterless: Any | None = None) -> Dependent[Any]:
        parsed = Dependent[Any].parse(call=handler, parameterless=parameterless, allow_types=cls.HANDLER_PARAM_TYPES)
        cls.handlers.append(parsed)
        return parsed

    @classmethod
    def handle(cls, parameterless: Any | None = None) -> Any:
        """装饰一个函数来向事件响应器直接添加一个处理函数。"""

        def _decorator(func: T_Handler) -> T_Handler:
            cls.append_handler(func, parameterless=parameterless)
            return func

        return _decorator

    @classmethod
    def receive(cls, id: str = "", parameterless: Any | None = None) -> Any:
        """装饰一个函数来指示 NoneBot 在接收用户新的一条消息后继续运行该函数。"""

        async def _receive(event: Event, matcher: "Matcher") -> None:
            matcher.set_target(RECEIVE_KEY.format(id=id))
            if matcher.get_target() == RECEIVE_KEY.format(id=id):
                matcher.set_receive(id, event)
                return
            if matcher.get_receive(id, ...) is not ...:
                return
            await matcher.reject()

        _parameterless = (Depends(_receive), *(parameterless or ()))

        def _decorator(func: T_Handler) -> T_Handler:
            cls._append_with_parameterless(func, _parameterless)
            return func

        return _decorator

    @classmethod
    def got(cls, key: str, prompt: Any | None = None, parameterless: Any | None = None) -> Any:
        """装饰一个函数来指示 NoneBot 获取一个参数 ``key``。"""

        async def _key_getter(event: Event, matcher: "Matcher") -> None:
            matcher.set_target(ARG_KEY.format(key=key))
            if matcher.get_target() == ARG_KEY.format(key=key):
                matcher.set_arg(key, event.get_message())
                return
            if matcher.get_arg(key, ...) is not ...:
                return
            await matcher.reject(prompt)

        _parameterless = (Depends(_key_getter), *(parameterless or ()))

        def _decorator(func: T_Handler) -> T_Handler:
            cls._append_with_parameterless(func, _parameterless)
            return func

        return _decorator

    @classmethod
    def _append_with_parameterless(cls, func: T_Handler, parameterless: tuple[Any, ...]) -> None:
        """把 parameterless 依赖附加到最近一个相同 call 的 handler 上（上游行为）。"""
        if cls.handlers and cls.handlers[-1].call is func:
            last = cls.handlers[-1]
            parsed = Dependent.parse_parameterless(tuple(parameterless), cls.HANDLER_PARAM_TYPES)
            cls.handlers[-1] = Dependent(call=last.call, params=last.params, parameterless=parsed + last.parameterless)
        else:
            cls.append_handler(func, parameterless=parameterless)

    # ---------------------------------------------------------------- 会话操作
    @classmethod
    async def send(cls, message: Any, **kwargs: Any) -> Any:
        """发送一条消息给当前交互用户。"""
        bot = current_bot.get()
        event = current_event.get()
        if isinstance(message, MessageTemplate):
            state = current_matcher.get().state
            _message = message.format(**state)
        else:
            _message = message
        return await bot.send(event=event, message=_message, **kwargs)

    @classmethod
    async def finish(cls, message: Any | None = None, **kwargs: Any) -> NoReturn:
        """发送一条消息给当前交互用户并结束当前事件响应器。"""
        if message is not None:
            await cls.send(message, **kwargs)
        raise FinishedException

    @classmethod
    async def pause(cls, prompt: Any | None = None, **kwargs: Any) -> NoReturn:
        """发送一条消息给当前交互用户并暂停事件响应器。"""
        try:
            matcher: Matcher | None = current_matcher.get()
        except LookupError:
            matcher = None
        if prompt is not None:
            result = await cls.send(prompt, **kwargs)
            if matcher is not None:
                matcher.state[PAUSE_PROMPT_RESULT_KEY] = result
        raise PausedException

    @classmethod
    async def reject(cls, prompt: Any | None = None, **kwargs: Any) -> NoReturn:
        """发送一条消息给当前交互用户并将当前事件处理流程中断在当前位置。"""
        try:
            matcher: Matcher | None = current_matcher.get()
            key: str | None = matcher.get_target()
        except LookupError:
            matcher = None
            key = None
        state_key = REJECT_PROMPT_RESULT_KEY.format(key=key) if key is not None else None
        if prompt is not None:
            result = await cls.send(prompt, **kwargs)
            if key is not None and matcher is not None and state_key is not None:
                matcher.state[state_key] = result
        raise RejectedException

    @classmethod
    async def reject_arg(cls, key: str, prompt: Any | None = None, **kwargs: Any) -> NoReturn:
        """最近使用 ``got`` 接收的消息不符合预期。"""
        matcher = current_matcher.get()
        arg_key = ARG_KEY.format(key=key)
        matcher.set_target(arg_key)
        if prompt is not None:
            result = await cls.send(prompt, **kwargs)
            matcher.state[REJECT_PROMPT_RESULT_KEY.format(key=arg_key)] = result
        raise RejectedException

    @classmethod
    async def reject_receive(cls, id: str = "", prompt: Any | None = None, **kwargs: Any) -> NoReturn:
        """最近使用 ``receive`` 接收的消息不符合预期。"""
        matcher = current_matcher.get()
        receive_key = RECEIVE_KEY.format(id=id)
        matcher.set_target(receive_key)
        if prompt is not None:
            result = await cls.send(prompt, **kwargs)
            matcher.state[REJECT_PROMPT_RESULT_KEY.format(key=receive_key)] = result
        raise RejectedException

    @classmethod
    def skip(cls) -> NoReturn:
        """跳过当前事件处理函数。"""
        raise SkippedException

    # ---------------------------------------------------------------- 状态存取
    @overload
    def get_receive(self, id: str) -> Event | None: ...

    @overload
    def get_receive(self, id: str, default: T) -> Event | T: ...

    def get_receive(self, id: str, default: Any = None) -> Any:
        """获取一个 ``receive`` 事件。"""
        return self.state.get(RECEIVE_KEY.format(id=id), default)

    def set_receive(self, id: str, event: Event) -> None:
        """设置一个 ``receive`` 事件。"""
        self.state[RECEIVE_KEY.format(id=id)] = event
        self.state[LAST_RECEIVE_KEY] = event

    @overload
    def get_last_receive(self) -> Event | None: ...

    @overload
    def get_last_receive(self, default: T) -> Event | T: ...

    def get_last_receive(self, default: Any = None) -> Any:
        """获取最近一次 ``receive`` 事件。"""
        return self.state.get(LAST_RECEIVE_KEY, default)

    @overload
    def get_arg(self, key: str) -> Message | None: ...

    @overload
    def get_arg(self, key: str, default: T) -> Message | T: ...

    def get_arg(self, key: str, default: Any = None) -> Any:
        """获取一个 ``got`` 消息。"""
        return self.state.get(ARG_KEY.format(key=key), default)

    def set_arg(self, key: str, message: Message) -> None:
        """设置一个 ``got`` 消息。"""
        self.state[ARG_KEY.format(key=key)] = message

    def set_target(self, target: str, cache: bool = True) -> None:
        if cache:
            self.state[REJECT_CACHE_TARGET] = target
        else:
            self.state[REJECT_TARGET] = target

    @overload
    def get_target(self) -> str | None: ...

    @overload
    def get_target(self, default: T) -> str | T: ...

    def get_target(self, default: Any = None) -> Any:
        return self.state.get(REJECT_TARGET, default)

    def stop_propagation(self) -> None:
        """阻止事件传播。"""
        self.block = True

    # ---------------------------------------------------------------- 运行
    async def update_type(
        self,
        bot: Bot,
        event: Event,
        stack: Any = None,
        dependency_cache: T_DependencyCache | None = None,
    ) -> str:
        updater = self.__class__._default_type_updater
        return (
            await updater(bot=bot, event=event, state=self.state, matcher=self, stack=stack, dependency_cache=dependency_cache)
            if updater
            else "message"
        )

    async def update_permission(
        self,
        bot: Bot,
        event: Event,
        stack: Any = None,
        dependency_cache: T_DependencyCache | None = None,
    ) -> Permission:
        updater = self.__class__._default_permission_updater
        if updater:
            return await updater(
                bot=bot, event=event, state=self.state, matcher=self, stack=stack, dependency_cache=dependency_cache
            )
        return Permission(User.from_event(event, perm=self.permission))

    async def resolve_reject(self) -> None:
        handler = current_handler.get()
        self.remain_handlers.insert(0, handler)
        if REJECT_CACHE_TARGET in self.state:
            self.state[REJECT_TARGET] = self.state[REJECT_CACHE_TARGET]

    @contextmanager
    def ensure_context(self, bot: Bot, event: Event) -> Iterator[None]:
        bot_token = current_bot.set(bot)
        event_token = current_event.set(event)
        matcher_token = current_matcher.set(self)
        try:
            yield
        finally:
            current_bot.reset(bot_token)
            current_event.reset(event_token)
            current_matcher.reset(matcher_token)

    async def simple_run(
        self,
        bot: Bot,
        event: Event,
        state: T_State,
        stack: Any = None,
        dependency_cache: T_DependencyCache | None = None,
    ) -> None:
        logger.trace(f"{self} run with incoming args: bot={bot}, event={event!r}, state={state!r}")
        with self.ensure_context(bot, event):
            try:
                self.state.update(state)
                while self.remain_handlers:
                    handler = self.remain_handlers.pop(0)
                    current_handler.set(handler)
                    logger.debug(f"Running handler {handler}")
                    try:
                        await handler(
                            matcher=self,
                            bot=bot,
                            event=event,
                            state=self.state,
                            stack=stack,
                            dependency_cache=dependency_cache,
                        )
                    except SkippedException:
                        logger.debug(f"Handler {handler} skipped")
            finally:
                logger.info(f"{self} running complete")

    async def run(
        self,
        bot: Bot,
        event: Event,
        state: T_State,
        stack: Any = None,
        dependency_cache: T_DependencyCache | None = None,
    ) -> None:
        """运行所有 handler。

        ``FinishedException`` 结束本响应器；``RejectedException`` / ``PausedException``
        结束后，按上游做法创建一个临时响应器（优先级 0、按会话过滤）等待下一条消息。
        """
        exc: FinishedException | RejectedException | PausedException | None = None
        try:
            await self.simple_run(bot, event, state, stack, dependency_cache)
        except ParserExit as error:
            # ``on_shell_command`` 的参数不合法：把 argparse 的提示发回去并结束，
            # 与上游一致（绝不让 ParserExit 冒到事件循环）。
            text = str(getattr(error, "message", "") or "").strip()
            if text:
                await self.send(text)
            return
        except (FinishedException, RejectedException, PausedException) as error:
            exc = error

        if isinstance(exc, FinishedException) or exc is None:
            return

        if isinstance(exc, RejectedException):
            await self.resolve_reject()

        type_ = await self.update_type(bot, event, stack, dependency_cache)
        permission = await self.update_permission(bot, event, stack, dependency_cache)
        source = self.__class__._source
        session_matcher = self.__class__.new(
            type_,
            Rule(),
            permission,
            list(self.remain_handlers),
            temp=True,
            priority=0,
            block=True,
            source=source,
            expire_time=self._session_expire_timeout(bot),
            default_state=self.state,
            default_type_updater=self.__class__._default_type_updater,
            default_permission_updater=self.__class__._default_permission_updater,
        )
        origin = getattr(self.__class__._trigger, "label", "") or self.__class__.__name__
        session_matcher._trigger = Trigger(kind="session", patterns=(), label=f"session:{origin}")

    @staticmethod
    def _session_expire_timeout(bot: Bot) -> timedelta:
        config = getattr(bot, "config", None)
        timeout = getattr(config, "session_expire_timeout", None)
        if isinstance(timeout, timedelta):
            return timeout
        return timedelta(minutes=2)
