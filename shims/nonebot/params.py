"""依赖注入的各类参数（上游 ``nonebot.params`` 的子集）。

用法与上游一致：``async def handler(arg: str = CommandArg())``。
"""

from __future__ import annotations

from re import Match
from typing import Any, Literal, overload

from nonebot.consts import (
    ENDSWITH_KEY,
    FULLMATCH_KEY,
    KEYWORD_KEY,
    PAUSE_PROMPT_RESULT_KEY,
    PREFIX_KEY,
    RAW_CMD_KEY,
    RECEIVE_KEY,
    REGEX_MATCHED,
    REJECT_PROMPT_RESULT_KEY,
    SHELL_ARGS,
    SHELL_ARGV,
    STARTSWITH_KEY,
    CMD_ARG_KEY,
    CMD_KEY,
    CMD_START_KEY,
    CMD_WHITESPACE_KEY,
)
from nonebot.internal.adapter import Event, Message
from nonebot.internal.params import Arg as Arg
from nonebot.internal.params import ArgParam as ArgParam
from nonebot.internal.params import ArgPlainText as ArgPlainText
from nonebot.internal.params import ArgPromptResult as ArgPromptResult
from nonebot.internal.params import ArgStr as ArgStr
from nonebot.internal.params import BotParam as BotParam
from nonebot.internal.params import DefaultParam as DefaultParam
from nonebot.internal.params import DependParam as DependParam
from nonebot.internal.params import Depends as Depends
from nonebot.internal.params import EventParam as EventParam
from nonebot.internal.params import ExceptionParam as ExceptionParam
from nonebot.internal.params import MatcherParam as MatcherParam
from nonebot.internal.params import StateParam as StateParam
from nonebot.matcher import Matcher
from nonebot.typing import T_State

__all__ = [
    "Arg",
    "ArgParam",
    "ArgPlainText",
    "ArgPromptResult",
    "ArgStr",
    "BotParam",
    "Command",
    "CommandArg",
    "CommandStart",
    "CommandWhitespace",
    "DefaultParam",
    "DependParam",
    "Depends",
    "Endswith",
    "EventMessage",
    "EventParam",
    "EventPlainText",
    "EventToMe",
    "EventType",
    "ExceptionParam",
    "Fullmatch",
    "Keyword",
    "LastReceived",
    "MatcherParam",
    "PausePromptResult",
    "RawCommand",
    "ReceivePromptResult",
    "Received",
    "RegexDict",
    "RegexGroup",
    "RegexMatched",
    "RegexStr",
    "ShellCommandArgs",
    "ShellCommandArgv",
    "Startswith",
    "StateParam",
]


async def _event_type(event: Event) -> str:
    return event.get_type()


def EventType() -> str:
    """``Event`` 类型参数。"""
    return Depends(_event_type)


async def _event_message(event: Event) -> Message:
    return event.get_message()


def EventMessage() -> Any:
    """``Event`` 消息参数。"""
    return Depends(_event_message)


async def _event_plain_text(event: Event) -> str:
    return event.get_plaintext()


def EventPlainText() -> str:
    """``Event`` 纯文本消息参数。"""
    return Depends(_event_plain_text)  # type: ignore[return-value]


async def _event_to_me(event: Event) -> bool:
    return event.is_tome()


def EventToMe() -> bool:
    """``Event`` ``to_me`` 参数。"""
    return Depends(_event_to_me)  # type: ignore[return-value]


def _command(state: T_State) -> Message:
    return state[PREFIX_KEY][CMD_KEY]


def Command() -> tuple[str, ...]:
    """消息命令元组。"""
    return Depends(_command)  # type: ignore[return-value]


def _raw_command(state: T_State) -> Message:
    return state[PREFIX_KEY][RAW_CMD_KEY]


def RawCommand() -> str:
    """消息命令文本。"""
    return Depends(_raw_command)  # type: ignore[return-value]


def _command_arg(state: T_State) -> Message:
    return state[PREFIX_KEY][CMD_ARG_KEY]


def CommandArg() -> Any:
    """消息命令参数。"""
    return Depends(_command_arg)


def _command_start(state: T_State) -> str:
    return state[PREFIX_KEY][CMD_START_KEY]


def CommandStart() -> str:
    """消息命令开头。"""
    return Depends(_command_start)  # type: ignore[return-value]


def _command_whitespace(state: T_State) -> str:
    return state[PREFIX_KEY][CMD_WHITESPACE_KEY]


def CommandWhitespace() -> str:
    """消息命令与参数之间的空白。"""
    return Depends(_command_whitespace)  # type: ignore[return-value]


def _shell_args(state: T_State) -> Any:
    return state.get(SHELL_ARGS)


def ShellCommandArgs() -> Any:
    """``shell_command`` 解析出的 ``argparse.Namespace``（未命中时为 ``None``）。"""
    return Depends(_shell_args)


def _shell_argv(state: T_State) -> list[str]:
    return list(state.get(SHELL_ARGV) or [])


def ShellCommandArgv() -> list[str]:
    """``shell_command`` 的原始 argv 列表（``shlex.split`` 之后、``argparse`` 之前）。"""
    return Depends(_shell_argv)  # type: ignore[return-value]


def _regex_matched(state: T_State) -> Match[str]:
    return state[REGEX_MATCHED]


def RegexMatched() -> Match[str]:
    """正则匹配结果。"""
    return Depends(_regex_matched, use_cache=False)  # type: ignore[return-value]


def _regex_str(groups: tuple[str | int, ...]) -> Any:
    def _regex_str_dependency(state: T_State) -> Any:
        return _regex_matched(state).group(*groups)

    return _regex_str_dependency


@overload
def RegexStr(group: Literal[0] = 0, /) -> str: ...


@overload
def RegexStr(group: str | int, /) -> str | Any: ...


@overload
def RegexStr(group1: str | int, group2: str | int, /, *groups: str | int) -> tuple[str | Any, ...]: ...


def RegexStr(*groups: str | int) -> str | tuple[str | Any, ...] | Any:
    """正则匹配结果文本。"""
    return Depends(_regex_str(groups), use_cache=False)


def _regex_group(state: T_State) -> tuple[Any, ...]:
    return _regex_matched(state).groups()


def RegexGroup() -> tuple[Any, ...]:
    """正则匹配结果 group 元组。"""
    return Depends(_regex_group, use_cache=False)  # type: ignore[return-value]


def _regex_dict(state: T_State) -> dict[str, Any]:
    return _regex_matched(state).groupdict()


def RegexDict() -> dict[str, Any]:
    """正则匹配结果 group 字典。"""
    return Depends(_regex_dict, use_cache=False)  # type: ignore[return-value]


def _startswith(state: T_State) -> str:
    return state[STARTSWITH_KEY]


def Startswith() -> str:
    """响应触发前缀。"""
    return Depends(_startswith, use_cache=False)  # type: ignore[return-value]


def _endswith(state: T_State) -> str:
    return state[ENDSWITH_KEY]


def Endswith() -> str:
    """响应触发后缀。"""
    return Depends(_endswith, use_cache=False)  # type: ignore[return-value]


def _fullmatch(state: T_State) -> str:
    return state[FULLMATCH_KEY]


def Fullmatch() -> str:
    """响应触发完整消息。"""
    return Depends(_fullmatch, use_cache=False)  # type: ignore[return-value]


def _keyword(state: T_State) -> str:
    return state[KEYWORD_KEY]


def Keyword() -> str:
    """响应触发关键字。"""
    return Depends(_keyword, use_cache=False)  # type: ignore[return-value]


def Received(id: str | None = None, default: Any = None) -> Any:
    """``receive`` 事件参数。"""

    def _received(matcher: Matcher) -> Any:
        return matcher.get_receive(id or "", default)

    return Depends(_received, use_cache=False)


def LastReceived(default: Any = None) -> Any:
    """``last_receive`` 事件参数。"""

    def _last_received(matcher: Matcher) -> Any:
        return matcher.get_last_receive(default)

    return Depends(_last_received, use_cache=False)


def ReceivePromptResult(id: str | None = None) -> Any:
    """``receive`` prompt 发送结果。"""

    def _receive_prompt_result(matcher: Matcher) -> Any:
        return matcher.state.get(REJECT_PROMPT_RESULT_KEY.format(key=RECEIVE_KEY.format(id=id)))

    return Depends(_receive_prompt_result, use_cache=False)


def PausePromptResult() -> Any:
    """``pause`` prompt 发送结果。"""

    def _pause_prompt_result(matcher: Matcher) -> Any:
        return matcher.state.get(PAUSE_PROMPT_RESULT_KEY)

    return Depends(_pause_prompt_result, use_cache=False)
