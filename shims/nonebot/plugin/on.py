"""事件响应器快捷定义（上游 ``nonebot.plugin.on`` 的子集）。

shim 支持：``on`` / ``on_message`` / ``on_notice`` / ``on_request`` / ``on_metaevent`` /
``on_startswith`` / ``on_endswith`` / ``on_fullmatch`` / ``on_keyword`` / ``on_command`` /
``on_regex`` / ``on_type``，以及 ``MatcherGroup`` / ``CommandGroup``（同步注册，无异步
插件管理器）。未实现：``on_shell_command``（需要 ``shell_command`` 规则）。

每个 ``on_*`` 返回的 Matcher 类都会带上 ``_trigger``（shim 扩展），桥接层据此生成
``CommandSpec.patterns``。触发规则仍是 NoneBot 原生规则，匹配行为不做任何改写。
"""

from __future__ import annotations

import inspect
import re
from datetime import datetime, timedelta
from typing import Any

from nonebot import _runtime
from nonebot.dependencies import Dependent
from nonebot.internal.adapter import Event
from nonebot.internal.permission import Permission
from nonebot.internal.rule import Rule
from nonebot.matcher import Matcher, MatcherSource, Trigger
from nonebot.rule import (
    ArgumentParser,
    command,
    endswith,
    fullmatch,
    is_type,
    keyword,
    regex,
    shell_command,
    startswith,
)
from nonebot.typing import T_Handler, T_PermissionChecker, T_RuleChecker, T_State

__all__ = [
    "CommandGroup",
    "MatcherGroup",
    "get_matcher_source",
    "on",
    "on_command",
    "on_endswith",
    "on_fullmatch",
    "on_keyword",
    "on_message",
    "on_metaevent",
    "on_notice",
    "on_regex",
    "on_request",
    "on_shell_command",
    "on_startswith",
    "on_type",
]


def get_matcher_source(depth: int = 1) -> MatcherSource | None:
    """获取事件响应器定义处的源码上下文（用于错误定位与 ``CommandSpec`` 展示）。"""
    current_frame = inspect.currentframe()
    if current_frame is None:
        return None
    frame = current_frame
    remaining = depth + 1
    while remaining > 0:
        frame = frame.f_back if frame is not None else None
        if frame is None:
            raise ValueError("Depth out of range")
        remaining -= 1
    module = inspect.getmodule(frame)
    module_name = module.__name__ if module is not None else None
    record = _runtime.current_plugin()
    if record is None and module_name:
        owner = _runtime.plugin_id_for_module(module_name)
        if owner:
            record = _runtime.plugin_record(owner)
    return MatcherSource(
        plugin_id=record.plugin_id if record is not None else None,
        module_name=module_name,
        lineno=frame.f_lineno,
    )


def _set_trigger(matcher: type[Matcher], kind: str, patterns: tuple[str, ...], label: str) -> type[Matcher]:
    matcher._trigger = Trigger(kind=kind, patterns=patterns, label=label)
    return matcher


def on(
    type: str = "",  # noqa: A002 — 与上游参数名一致
    rule: Rule | T_RuleChecker | None = None,
    permission: Permission | T_PermissionChecker | None = None,
    *,
    handlers: list[T_Handler | Dependent[Any]] | None = None,
    temp: bool = False,
    expire_time: datetime | timedelta | None = None,
    priority: int = 1,
    block: bool = False,
    state: T_State | None = None,
    _depth: int = 0,
) -> type[Matcher]:
    """注册一个基础事件响应器，可自定义类型。"""
    matcher = Matcher.new(
        type,
        Rule() & rule,
        Permission() | permission,
        temp=temp,
        expire_time=expire_time,
        priority=priority,
        block=block,
        handlers=handlers,
        source=get_matcher_source(_depth + 1),
        default_state=state,
    )
    return _set_trigger(matcher, type or "event", (), type or "event")


def on_metaevent(*args: Any, _depth: int = 0, **kwargs: Any) -> type[Matcher]:
    """注册一个元事件响应器。"""
    return on("meta_event", *args, **kwargs, _depth=_depth + 1)


def on_message(*args: Any, _depth: int = 0, **kwargs: Any) -> type[Matcher]:
    """注册一个消息事件响应器。"""
    return on("message", *args, **kwargs, _depth=_depth + 1)


def on_notice(*args: Any, _depth: int = 0, **kwargs: Any) -> type[Matcher]:
    """注册一个通知事件响应器。"""
    return on("notice", *args, **kwargs, _depth=_depth + 1)


def on_request(*args: Any, _depth: int = 0, **kwargs: Any) -> type[Matcher]:
    """注册一个请求事件响应器。"""
    return on("request", *args, **kwargs, _depth=_depth + 1)


def on_startswith(
    msg: str | tuple[str, ...],
    rule: Rule | T_RuleChecker | None = None,
    ignorecase: bool = False,
    _depth: int = 0,
    **kwargs: Any,
) -> type[Matcher]:
    """注册一个消息事件响应器，当消息文本以指定内容开头时响应。"""
    patterns = (msg,) if isinstance(msg, str) else tuple(msg)
    matcher = on_message(startswith(msg, ignorecase) & rule, **kwargs, _depth=_depth + 1)
    return _set_trigger(matcher, "startswith", patterns, f"startswith:{'|'.join(patterns)}")


def on_endswith(
    msg: str | tuple[str, ...],
    rule: Rule | T_RuleChecker | None = None,
    ignorecase: bool = False,
    _depth: int = 0,
    **kwargs: Any,
) -> type[Matcher]:
    """注册一个消息事件响应器，当消息文本以指定内容结尾时响应。"""
    patterns = (msg,) if isinstance(msg, str) else tuple(msg)
    matcher = on_message(endswith(msg, ignorecase) & rule, **kwargs, _depth=_depth + 1)
    return _set_trigger(matcher, "endswith", patterns, f"endswith:{'|'.join(patterns)}")


def on_fullmatch(
    msg: str | tuple[str, ...],
    rule: Rule | T_RuleChecker | None = None,
    ignorecase: bool = False,
    _depth: int = 0,
    **kwargs: Any,
) -> type[Matcher]:
    """注册一个消息事件响应器，当消息文本与指定内容全匹配时响应。"""
    patterns = (msg,) if isinstance(msg, str) else tuple(msg)
    matcher = on_message(fullmatch(msg, ignorecase) & rule, **kwargs, _depth=_depth + 1)
    return _set_trigger(matcher, "fullmatch", patterns, f"fullmatch:{'|'.join(patterns)}")


def on_keyword(
    keywords: set[str],
    rule: Rule | T_RuleChecker | None = None,
    _depth: int = 0,
    **kwargs: Any,
) -> type[Matcher]:
    """注册一个消息事件响应器，当消息纯文本包含关键词时响应。"""
    patterns = tuple(sorted(keywords))
    matcher = on_message(keyword(*keywords) & rule, **kwargs, _depth=_depth + 1)
    return _set_trigger(matcher, "keyword", patterns, f"keyword:{'|'.join(patterns)}")


def on_command(
    cmd: str | tuple[str, ...],
    rule: Rule | T_RuleChecker | None = None,
    aliases: set[str | tuple[str, ...]] | None = None,
    force_whitespace: str | bool | None = None,
    _depth: int = 0,
    **kwargs: Any,
) -> type[Matcher]:
    """注册一个消息事件响应器，当消息以指定命令开头时响应。"""
    commands = {cmd} | (aliases or set())
    kwargs.setdefault("block", False)
    primary = ".".join(cmd) if isinstance(cmd, tuple) else str(cmd)
    others = sorted(
        ".".join(item) if isinstance(item, tuple) else str(item) for item in (aliases or set())
    )
    patterns = tuple([primary, *(item for item in others if item != primary)])
    matcher = on_message(command(*commands, force_whitespace=force_whitespace) & rule, **kwargs, _depth=_depth + 1)
    return _set_trigger(matcher, "command", patterns, f"command:{'|'.join(patterns)}")


def on_shell_command(
    cmd: str | tuple[str, ...],
    parser: ArgumentParser,
    rule: Rule | T_RuleChecker | None = None,
    aliases: set[str | tuple[str, ...]] | None = None,
    _depth: int = 0,
    **kwargs: Any,
) -> type[Matcher]:
    """注册一个 shell 风格命令响应器（上游同义）。

    参数用 ``shlex`` 切分后交给 ``parser``（:class:`nonebot.rule.ArgumentParser`）解析；
    解析结果由 ``ShellCommandArgs`` / ``ShellCommandArgv`` 依赖注入取用。参数不合法时
    ``ParserExit`` 会被 Matcher 捕获并作为提示发回，不会把事件循环带下去。
    """
    commands = {cmd} | (aliases or set())
    kwargs.setdefault("block", False)
    primary = ".".join(cmd) if isinstance(cmd, tuple) else str(cmd)
    others = sorted(
        ".".join(item) if isinstance(item, tuple) else str(item) for item in (aliases or set())
    )
    patterns = tuple([primary, *(item for item in others if item != primary)])
    matcher = on_message(shell_command(cmd, parser) & rule, **kwargs, _depth=_depth + 1)
    return _set_trigger(matcher, "command", patterns, f"command:{'|'.join(patterns)}")


def on_regex(
    pattern: str,
    flags: int | re.RegexFlag = 0,
    rule: Rule | T_RuleChecker | None = None,
    _depth: int = 0,
    **kwargs: Any,
) -> type[Matcher]:
    """注册一个消息事件响应器，当消息匹配正则表达式时响应。"""
    matcher = on_message(regex(pattern, flags) & rule, **kwargs, _depth=_depth + 1)
    return _set_trigger(matcher, "regex", (pattern,), f"regex:{pattern}")


def on_type(
    types: type[Event] | tuple[type[Event], ...],
    rule: Rule | T_RuleChecker | None = None,
    *,
    _depth: int = 0,
    **kwargs: Any,
) -> type[Matcher]:
    """注册一个事件响应器，当事件为指定类型时响应。"""
    event_types = types if isinstance(types, tuple) else (types,)
    matcher = on(rule=is_type(*event_types) & rule, **kwargs, _depth=_depth + 1)
    names = "|".join(type_.__name__ for type_ in event_types)
    return _set_trigger(matcher, "type", tuple(type_.__name__ for type_ in event_types), f"type:{names}")


class _Group:
    """事件响应器组合的基类。"""

    def __init__(self, **kwargs: Any) -> None:
        self.matchers: list[type[Matcher]] = []
        self._base_kwargs = dict(kwargs)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(matchers={self.matchers})"

    def _merge_kwargs(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        merged = dict(self._base_kwargs)
        merged.update(kwargs)
        return merged

    def _store(self, matcher: type[Matcher]) -> type[Matcher]:
        self.matchers.append(matcher)
        return matcher


class MatcherGroup(_Group):
    """事件响应器组合（同步注册）。"""

    def on(self, *args: Any, **kwargs: Any) -> type[Matcher]:
        return self._store(on(*args, **self._merge_kwargs(kwargs), _depth=1))

    def on_message(self, *args: Any, **kwargs: Any) -> type[Matcher]:
        return self._store(on_message(*args, **self._merge_kwargs(kwargs), _depth=1))

    def on_notice(self, *args: Any, **kwargs: Any) -> type[Matcher]:
        return self._store(on_notice(*args, **self._merge_kwargs(kwargs), _depth=1))

    def on_request(self, *args: Any, **kwargs: Any) -> type[Matcher]:
        return self._store(on_request(*args, **self._merge_kwargs(kwargs), _depth=1))

    def on_metaevent(self, *args: Any, **kwargs: Any) -> type[Matcher]:
        return self._store(on_metaevent(*args, **self._merge_kwargs(kwargs), _depth=1))

    def on_startswith(self, msg: str | tuple[str, ...], **kwargs: Any) -> type[Matcher]:
        return self._store(on_startswith(msg, **self._merge_kwargs(kwargs), _depth=1))

    def on_endswith(self, msg: str | tuple[str, ...], **kwargs: Any) -> type[Matcher]:
        return self._store(on_endswith(msg, **self._merge_kwargs(kwargs), _depth=1))

    def on_fullmatch(self, msg: str | tuple[str, ...], **kwargs: Any) -> type[Matcher]:
        return self._store(on_fullmatch(msg, **self._merge_kwargs(kwargs), _depth=1))

    def on_keyword(self, keywords: set[str], **kwargs: Any) -> type[Matcher]:
        return self._store(on_keyword(keywords, **self._merge_kwargs(kwargs), _depth=1))

    def on_command(self, cmd: str | tuple[str, ...], **kwargs: Any) -> type[Matcher]:
        return self._store(on_command(cmd, **self._merge_kwargs(kwargs), _depth=1))

    def on_regex(self, pattern: str, flags: int | re.RegexFlag = 0, **kwargs: Any) -> type[Matcher]:
        return self._store(on_regex(pattern, flags, **self._merge_kwargs(kwargs), _depth=1))

    def on_type(self, types: type[Event] | tuple[type[Event], ...], **kwargs: Any) -> type[Matcher]:
        return self._store(on_type(types, **self._merge_kwargs(kwargs), _depth=1))


class CommandGroup(MatcherGroup):
    """命令组：以 ``cmd`` 为前缀的命令集合（``cmd.sub`` 形式）。"""

    def __init__(self, cmd: str | tuple[str, ...], **kwargs: Any) -> None:
        self.base_cmd = cmd if isinstance(cmd, tuple) else (cmd,)
        self.base_aliases: set[tuple[str, ...]] = set(kwargs.pop("aliases", set()) or set())
        super().__init__(**kwargs)

    def command(
        self,
        cmd: str | tuple[str, ...],
        aliases: set[str | tuple[str, ...]] | None = None,
        **kwargs: Any,
    ) -> type[Matcher]:
        """注册一条子命令（``cmd.sub``）。"""
        suffix = cmd if isinstance(cmd, tuple) else (cmd,)
        full_cmd = (*self.base_cmd, *suffix)
        merged = self._merge_kwargs(kwargs)
        merged_aliases = set(merged.pop("aliases", set()) or set()) | set(aliases or set())
        merged_aliases |= {(*base, *suffix) for base in self.base_aliases}
        return self._store(on_command(full_cmd, aliases=merged_aliases or None, **merged, _depth=1))

    def on_command(self, cmd: str | tuple[str, ...], **kwargs: Any) -> type[Matcher]:
        """上游 ``CommandGroup.on_command`` 的等价写法（``cmd`` 为完整命令）。"""
        return self._store(on_command(cmd, **self._merge_kwargs(kwargs), _depth=1))
