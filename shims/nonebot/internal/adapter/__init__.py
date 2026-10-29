"""协议适配器基类（上游 ``nonebot.internal.adapter`` / ``nonebot.adapters`` 的子集）。

包含 ``Adapter`` / ``Bot`` / ``Event`` / ``Message`` / ``MessageSegment`` / ``MessageTemplate``。
上游这些类都继承 pydantic ``BaseModel``；shim 用普通类实现同样语义（见包文档偏差清单）。
"""

from __future__ import annotations

import abc
from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Generic, SupportsIndex, TypeVar, overload

from nonebot.utils import is_coroutine_callable

__all__ = [
    "Adapter",
    "Bot",
    "Event",
    "Message",
    "MessageSegment",
    "MessageTemplate",
]

TMS = TypeVar("TMS", bound="MessageSegment")
TM = TypeVar("TM", bound="Message")
E = TypeVar("E", bound="Event")


# --------------------------------------------------------------------------- #
# MessageSegment / Message
# --------------------------------------------------------------------------- #


@dataclass
class MessageSegment(abc.ABC, Generic[TM]):
    """消息段基类。"""

    type: str
    """消息段类型"""
    data: dict[str, Any] = field(default_factory=dict)
    """消息段数据"""

    @classmethod
    @abc.abstractmethod
    def get_message_class(cls) -> type[TM]:
        """获取消息数组类型"""
        raise NotImplementedError

    @abc.abstractmethod
    def __str__(self) -> str:
        """该消息段所代表的 str，在命令匹配部分使用"""
        raise NotImplementedError

    def __len__(self) -> int:
        return len(str(self))

    def __add__(self, other: "str | MessageSegment | Iterable[MessageSegment]") -> TM:
        return self.get_message_class()(self) + other

    def __radd__(self, other: "str | MessageSegment | Iterable[MessageSegment]") -> TM:
        return self.get_message_class()(other) + self

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def keys(self) -> Any:
        return self.data.keys()

    def values(self) -> Any:
        return self.data.values()

    def items(self) -> Any:
        return self.data.items()

    def join(self, iterable: "Iterable[MessageSegment | TM]") -> TM:
        return self.get_message_class()(self).join(iterable)

    def copy(self) -> "MessageSegment":
        return deepcopy(self)

    @abc.abstractmethod
    def is_text(self) -> bool:
        """当前消息段是否为纯文本"""
        raise NotImplementedError


class Message(list[TMS], abc.ABC):  # noqa: UP006
    """消息序列。

    参数:
        message: 消息内容
    """

    def __init__(self, message: "str | None | Iterable[TMS] | TMS" = None) -> None:
        super().__init__()
        if message is None:
            return
        if isinstance(message, str):
            self.extend(self._construct(message))
        elif isinstance(message, MessageSegment):
            self.append(message)
        elif isinstance(message, Iterable):
            self.extend(message)
        else:  # pragma: no cover — 与上游一致，兜底走 _construct
            self.extend(self._construct(message))

    @classmethod
    def template(cls, format_string: "str | TM") -> "MessageTemplate[TM]":
        """创建消息模板（用法与 ``str.format`` 相同）。"""
        return MessageTemplate(format_string, cls.get_segment_class())

    @classmethod
    @abc.abstractmethod
    def get_segment_class(cls) -> type[TMS]:
        """获取消息段类型"""
        raise NotImplementedError

    def __str__(self) -> str:
        return "".join(str(seg) for seg in self)

    @staticmethod
    @abc.abstractmethod
    def _construct(msg: str) -> Iterable[TMS]:
        """构造消息数组"""
        raise NotImplementedError

    @abc.abstractmethod
    def extract_plain_text(self) -> str:
        """获取消息的纯文本内容"""
        raise NotImplementedError

    def __add__(self, other: "str | TMS | Iterable[TMS]") -> "Message":
        result = self.copy()
        result += other
        return result

    def __radd__(self, other: "str | TMS | Iterable[TMS]") -> "Message":
        result = self.__class__(other)
        return result + self

    def __iadd__(self, other: "str | TMS | Iterable[TMS]") -> "Message":
        if isinstance(other, str):
            self.extend(self._construct(other))
        elif isinstance(other, MessageSegment):
            self.append(other)
        elif isinstance(other, Iterable):
            self.extend(other)
        else:
            raise TypeError(f"Unsupported type {type(other)!r}")
        return self

    @overload
    def __getitem__(self, args: str) -> "Message": ...

    @overload
    def __getitem__(self, args: tuple[str, int]) -> TMS: ...

    @overload
    def __getitem__(self, args: tuple[str, slice]) -> "Message": ...

    @overload
    def __getitem__(self, args: int) -> TMS: ...

    @overload
    def __getitem__(self, args: slice) -> "Message": ...

    def __getitem__(self, args: Any) -> Any:  # type: ignore[override]
        arg1, arg2 = args if isinstance(args, tuple) else (args, None)
        if isinstance(arg1, int) and arg2 is None:
            return super().__getitem__(arg1)
        if isinstance(arg1, slice) and arg2 is None:
            return self.__class__(super().__getitem__(arg1))
        if isinstance(arg1, str) and arg2 is None:
            return self.__class__(seg for seg in self if seg.type == arg1)
        if isinstance(arg1, str) and isinstance(arg2, int):
            return [seg for seg in self if seg.type == arg1][arg2]
        if isinstance(arg1, str) and isinstance(arg2, slice):
            return self.__class__([seg for seg in self if seg.type == arg1][arg2])
        raise ValueError("Incorrect arguments to slice")

    def __contains__(self, value: Any) -> bool:
        if isinstance(value, str):
            return next((seg for seg in self if seg.type == value), None) is not None
        return super().__contains__(value)

    def has(self, value: "TMS | str") -> bool:
        """与 ``__contains__`` 相同。"""
        return value in self

    def index(self, value: Any, *args: SupportsIndex) -> int:
        if isinstance(value, str):
            for index, seg in enumerate(self):
                if seg.type == value:
                    return index
            raise ValueError(f"{value} is not in message")
        return super().index(value, *args)

    def copy(self) -> "Message":
        return self.__class__(deepcopy(list(self)))

    def join(self, iterable: "Iterable[str | TMS]") -> "Message":
        result = self.__class__()
        for index, item in enumerate(iterable):
            if index:
                result.extend(self)
            result += item
        return result


class MessageTemplate(Generic[TM]):
    """消息模板（``str.format`` 风格，支持以消息段工厂作为格式说明符）。

    ::

        Message.template("你好，{name:at}").format(name=123)

    偏差：上游支持 ``!r`` / ``!s`` 转换与 ``{arg:segment_type}``；shim 同样支持，
    但不支持上游的 ``separator`` 等扩展。
    """

    def __init__(self, format_string: str, factory: type[MessageSegment]) -> None:
        self.format_string = str(format_string)
        self.factory = factory

    def __repr__(self) -> str:
        return f"MessageTemplate({self.format_string!r})"

    def format(self, *args: Any, **kwargs: Any) -> Any:
        message_cls = self.factory.get_message_class()
        from string import Formatter

        result = message_cls()
        formatter = Formatter()
        for literal_text, field_name, format_spec, conversion in formatter.parse(self.format_string):
            if literal_text:
                result += literal_text
            if field_name is None:
                continue
            if field_name == "":
                value = args[0] if args else ""
            else:
                try:
                    value = kwargs[field_name]
                except KeyError:
                    value = self._resolve_field(field_name, args, kwargs)
            if conversion == "r":
                value = repr(value)
            elif conversion == "s":
                value = str(value)
            if format_spec:
                factory = getattr(self.factory, format_spec, None)
                if factory is None:
                    raise ValueError(f"unknown message segment format spec: {format_spec!r}")
                result += factory(value)
            else:
                result += str(value)
        return result

    @staticmethod
    def _resolve_field(field_name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
        from string import Formatter

        try:
            return Formatter().get_field(field_name, args, kwargs)[0]
        except (KeyError, AttributeError, IndexError) as exc:
            raise KeyError(f"missing field {field_name!r} for message template") from exc


# --------------------------------------------------------------------------- #
# Event
# --------------------------------------------------------------------------- #


class Event(abc.ABC):
    """Event 基类。提供获取关键信息的方法，其余信息可直接获取。"""

    @abc.abstractmethod
    def get_type(self) -> str:
        """获取事件类型。"""
        raise NotImplementedError

    @abc.abstractmethod
    def get_event_name(self) -> str:
        """获取事件名称。"""
        raise NotImplementedError

    @abc.abstractmethod
    def get_event_description(self) -> str:
        """获取事件描述。"""
        raise NotImplementedError

    def __str__(self) -> str:
        return f"[{self.get_event_name()}]: {self.get_event_description()}"

    def get_log_string(self) -> str:
        """获取事件日志信息；抛出 :class:`NoLogException` 可隐藏该事件日志。"""
        return f"[{self.get_event_name()}]: {self.get_event_description()}"

    @abc.abstractmethod
    def get_user_id(self) -> str:
        """获取事件主体 id。"""
        raise NotImplementedError

    @abc.abstractmethod
    def get_session_id(self) -> str:
        """获取会话 id。"""
        raise NotImplementedError

    @abc.abstractmethod
    def get_message(self) -> Message:
        """获取事件消息内容。"""
        raise NotImplementedError

    def get_plaintext(self) -> str:
        """获取消息纯文本。"""
        return self.get_message().extract_plain_text()

    @abc.abstractmethod
    def is_tome(self) -> bool:
        """获取事件是否与机器人有关。"""
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Adapter / Bot
# --------------------------------------------------------------------------- #


class Adapter(abc.ABC):
    """协议适配器基类。"""

    def __init__(self, driver: Any, **kwargs: Any) -> None:
        self.driver = driver
        """Driver 实例"""
        self.bots: dict[str, "Bot"] = {}
        """本协议适配器已建立连接的 Bot 实例"""

    def __repr__(self) -> str:
        return f"Adapter(name={self.get_name()!r})"

    @classmethod
    @abc.abstractmethod
    def get_name(cls) -> str:
        """当前协议适配器的名称"""
        raise NotImplementedError

    @property
    def config(self) -> Any:
        """全局 NoneBot 配置"""
        return getattr(self.driver, "config", None)

    def bot_connect(self, bot: "Bot") -> None:
        """告知 NoneBot 建立了一个新的 Bot 连接。"""
        self.bots[bot.self_id] = bot

    def bot_disconnect(self, bot: "Bot") -> None:
        """告知 NoneBot 断开了一个 Bot 连接。"""
        self.bots.pop(bot.self_id, None)


class Bot(abc.ABC):
    """Bot 基类。用于处理上报消息，并提供 API 调用接口。

    参数:
        adapter: 协议适配器实例
        self_id: 机器人 ID
    """

    _calling_api_hook: list[Any] = []
    """call_api 时执行的函数（上游是 set，shim 用 list 保证顺序确定）。"""
    _called_api_hook: list[Any] = []
    """call_api 后执行的函数"""

    def __init__(self, adapter: Adapter, self_id: str) -> None:
        self.adapter: Adapter = adapter
        """协议适配器实例"""
        self.self_id: str = self_id
        """机器人 ID"""

    def __repr__(self) -> str:
        return f"Bot(type={self.type!r}, self_id={self.self_id!r})"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(f"'{self.__class__.__name__}' object has no attribute '{name}'")
        return partial(self.call_api, name)

    @property
    def type(self) -> str:
        """协议适配器名称"""
        return self.adapter.get_name()

    @property
    def config(self) -> Any:
        """全局 NoneBot 配置"""
        return self.adapter.config

    async def call_api(self, api: str, **data: Any) -> Any:
        """调用机器人 API 接口。"""
        for hook in list(self._calling_api_hook):
            result = hook(bot=self, api=api, data=data)
            if is_coroutine_callable(hook):
                await result
        result = await self._call_api(api, **data)
        for hook in list(self._called_api_hook):
            hook_result = hook(bot=self, api=api, data=data, result=result)
            if is_coroutine_callable(hook):
                await hook_result
        return result

    async def _call_api(self, api: str, **data: Any) -> Any:
        raise NotImplementedError

    @classmethod
    def on_calling_api(cls, func: Any) -> Any:
        """注册一个 ``call_api`` 前执行的函数。"""
        if func not in cls._calling_api_hook:
            cls._calling_api_hook.append(func)
        return func

    @classmethod
    def on_called_api(cls, func: Any) -> Any:
        """注册一个 ``call_api`` 后执行的函数。"""
        if func not in cls._called_api_hook:
            cls._called_api_hook.append(func)
        return func

    async def send(self, event: Event, message: "str | Message | MessageSegment", **kwargs: Any) -> Any:
        """根据 ``event`` 向触发事件的主体回复消息（子类实现）。"""
        raise NotImplementedError
