"""规则（上游 ``nonebot.rule`` 的子集）。

包含 ``Rule`` / ``TrieRule``、``startswith`` / ``endswith`` / ``fullmatch`` /
``keyword`` / ``command`` / ``regex`` / ``to_me`` / ``is_type`` 这些快捷规则工厂，
以及 shell 风格的 ``shell_command`` + ``ArgumentParser`` + ``ParserExit``。

``TrieRule`` 的命令前缀表用普通 dict + 最长前缀扫描实现（上游用 ``pygtrie.CharTrie``，
那是第三方依赖）。``ArgumentParser`` 直接继承标准库 ``argparse``，只把
``error``/``exit`` 转成 :class:`ParserExit`（上游同样如此，避免 ``SystemExit`` 打穿事件循环）。
"""

from __future__ import annotations

import argparse
import re
import shlex
from itertools import product
from typing import Any, NamedTuple, NoReturn, TypedDict

from nonebot import get_driver
from nonebot.consts import (
    CMD_ARG_KEY,
    CMD_KEY,
    CMD_START_KEY,
    CMD_WHITESPACE_KEY,
    ENDSWITH_KEY,
    FULLMATCH_KEY,
    KEYWORD_KEY,
    PREFIX_KEY,
    RAW_CMD_KEY,
    REGEX_MATCHED,
    SHELL_ARGS,
    SHELL_ARGV,
    STARTSWITH_KEY,
)
from nonebot.exception import ParserExit as ParserExit
from nonebot.internal.adapter import Bot, Event, Message, MessageSegment
from nonebot.internal.rule import Rule as Rule
from nonebot.log import logger
from nonebot.params import Command, CommandArg, CommandWhitespace, EventToMe
from nonebot.typing import T_State

__all__ = [
    "CMD_RESULT",
    "TRIE_VALUE",
    "ArgumentParser",
    "CommandRule",
    "EndswithRule",
    "FullmatchRule",
    "IsTypeRule",
    "KeywordsRule",
    "ParserExit",
    "RegexRule",
    "Rule",
    "ShellCommandRule",
    "StartswithRule",
    "ToMeRule",
    "TrieRule",
    "command",
    "endswith",
    "fullmatch",
    "is_type",
    "keyword",
    "regex",
    "shell_command",
    "startswith",
    "to_me",
]


class CMD_RESULT(TypedDict):
    command: tuple[str, ...] | None
    raw_command: str | None
    command_arg: Message | None
    command_start: str | None
    command_whitespace: str | None


class TRIE_VALUE(NamedTuple):
    command_start: str
    command: tuple[str, ...]


class TrieRule:
    """命令前缀表。

    ``add_prefix`` 注册 ``/cmd`` 这类前缀；``get_value`` 在事件分发前解析出
    ``state[PREFIX_KEY]``（命令元组 / 命令参数 / 命令与参数之间的空白）。
    """

    prefix: dict[str, TRIE_VALUE] = {}

    @classmethod
    def add_prefix(cls, prefix: str, value: TRIE_VALUE) -> None:
        if prefix in cls.prefix:
            logger.warning(f'Duplicated prefix rule "{prefix}"')
            return
        cls.prefix[prefix] = value

    @classmethod
    def longest_prefix(cls, text: str) -> tuple[str, TRIE_VALUE] | None:
        """最长前缀匹配（替代上游的 ``CharTrie.longest_prefix``）。"""
        best_key = ""
        best_value: TRIE_VALUE | None = None
        for key, value in cls.prefix.items():
            if len(key) > len(best_key) and text.startswith(key):
                best_key = key
                best_value = value
        return (best_key, best_value) if best_value is not None else None

    @classmethod
    def get_value(cls, bot: Bot, event: Event, state: T_State) -> CMD_RESULT:
        prefix = CMD_RESULT(
            command=None,
            raw_command=None,
            command_arg=None,
            command_start=None,
            command_whitespace=None,
        )
        state[PREFIX_KEY] = prefix
        if event.get_type() != "message":
            return prefix

        try:
            message = event.get_message()
        except (ValueError, NotImplementedError):
            return prefix
        if not message:
            return prefix
        message_seg: MessageSegment = message[0]
        if not message_seg.is_text():
            return prefix

        segment_text = str(message_seg).lstrip()
        matched = cls.longest_prefix(segment_text)
        if matched is None:
            return prefix
        key, value = matched
        prefix[RAW_CMD_KEY] = key
        prefix[CMD_START_KEY] = value.command_start
        prefix[CMD_KEY] = value.command

        msg = message.copy()
        msg.pop(0)

        # 命令与参数之间的空白符
        arg_str = segment_text[len(key) :]
        arg_str_stripped = arg_str.lstrip()
        while not arg_str_stripped and msg and msg[0].is_text():
            arg_str += str(msg.pop(0))
            arg_str_stripped = arg_str.lstrip()

        has_arg = arg_str_stripped or msg
        if has_arg and (stripped_len := len(arg_str) - len(arg_str_stripped)) > 0:
            prefix[CMD_WHITESPACE_KEY] = arg_str[:stripped_len]

        if arg_str_stripped:
            new_message = msg.__class__(arg_str_stripped)
            for new_segment in reversed(new_message):
                msg.insert(0, new_segment)
        prefix[CMD_ARG_KEY] = msg
        return prefix


class StartswithRule:
    """检查消息纯文本是否以指定字符串开头。"""

    def __init__(self, msg: tuple[str, ...], ignorecase: bool = False) -> None:
        self.msg = msg
        self.ignorecase = ignorecase

    def __repr__(self) -> str:
        return f"Startswith(msg={self.msg}, ignorecase={self.ignorecase})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, StartswithRule)
            and frozenset(self.msg) == frozenset(other.msg)
            and self.ignorecase == other.ignorecase
        )

    def __hash__(self) -> int:
        return hash((frozenset(self.msg), self.ignorecase))

    async def __call__(self, event: Event, state: T_State) -> bool:
        try:
            text = event.get_plaintext()
        except Exception:  # noqa: BLE001 — 事件没有消息内容时不匹配（上游同）
            return False
        if match := re.match(
            f"^(?:{'|'.join(re.escape(prefix) for prefix in self.msg)})",
            text,
            re.IGNORECASE if self.ignorecase else 0,
        ):
            state[STARTSWITH_KEY] = match.group()
            return True
        return False


def startswith(msg: str | tuple[str, ...], ignorecase: bool = False) -> Rule:
    """匹配消息纯文本开头。"""
    if isinstance(msg, str):
        msg = (msg,)
    return Rule(StartswithRule(msg, ignorecase))


class EndswithRule:
    """检查消息纯文本是否以指定字符串结尾。"""

    def __init__(self, msg: tuple[str, ...], ignorecase: bool = False) -> None:
        self.msg = msg
        self.ignorecase = ignorecase

    def __repr__(self) -> str:
        return f"Endswith(msg={self.msg}, ignorecase={self.ignorecase})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, EndswithRule)
            and frozenset(self.msg) == frozenset(other.msg)
            and self.ignorecase == other.ignorecase
        )

    def __hash__(self) -> int:
        return hash((frozenset(self.msg), self.ignorecase))

    async def __call__(self, event: Event, state: T_State) -> bool:
        try:
            text = event.get_plaintext()
        except Exception:  # noqa: BLE001 — 事件没有消息内容时不匹配（上游同）
            return False
        if match := re.search(
            f"(?:{'|'.join(re.escape(suffix) for suffix in self.msg)})$",
            text,
            re.IGNORECASE if self.ignorecase else 0,
        ):
            state[ENDSWITH_KEY] = match.group()
            return True
        return False


def endswith(msg: str | tuple[str, ...], ignorecase: bool = False) -> Rule:
    """匹配消息纯文本结尾。"""
    if isinstance(msg, str):
        msg = (msg,)
    return Rule(EndswithRule(msg, ignorecase))


class FullmatchRule:
    """检查消息纯文本是否与指定字符串全匹配。"""

    def __init__(self, msg: tuple[str, ...], ignorecase: bool = False) -> None:
        self.msg = tuple(map(str.casefold, msg)) if ignorecase else msg
        self.ignorecase = ignorecase

    def __repr__(self) -> str:
        return f"Fullmatch(msg={self.msg}, ignorecase={self.ignorecase})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, FullmatchRule)
            and frozenset(self.msg) == frozenset(other.msg)
            and self.ignorecase == other.ignorecase
        )

    def __hash__(self) -> int:
        return hash((frozenset(self.msg), self.ignorecase))

    async def __call__(self, event: Event, state: T_State) -> bool:
        try:
            text = event.get_plaintext()
        except Exception:  # noqa: BLE001 — 事件没有消息内容时不匹配（上游同）
            return False
        if not text:
            return False
        text = text.casefold() if self.ignorecase else text
        if text in self.msg:
            state[FULLMATCH_KEY] = text
            return True
        return False


def fullmatch(msg: str | tuple[str, ...], ignorecase: bool = False) -> Rule:
    """完全匹配消息。"""
    if isinstance(msg, str):
        msg = (msg,)
    return Rule(FullmatchRule(msg, ignorecase))


class KeywordsRule:
    """检查消息纯文本是否包含指定关键字。"""

    def __init__(self, *keywords: str) -> None:
        self.keywords = keywords

    def __repr__(self) -> str:
        return f"Keywords(keywords={self.keywords})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, KeywordsRule) and frozenset(self.keywords) == frozenset(other.keywords)

    def __hash__(self) -> int:
        return hash(frozenset(self.keywords))

    async def __call__(self, event: Event, state: T_State) -> bool:
        try:
            text = event.get_plaintext()
        except Exception:  # noqa: BLE001 — 事件没有消息内容时不匹配（上游同）
            return False
        if not text:
            return False
        if key := next((k for k in self.keywords if k in text), None):
            state[KEYWORD_KEY] = key
            return True
        return False


def keyword(*keywords: str) -> Rule:
    """匹配消息纯文本关键词。"""
    return Rule(KeywordsRule(*keywords))


class CommandRule:
    """检查消息是否为指定命令。"""

    def __init__(self, cmds: list[tuple[str, ...]], force_whitespace: str | bool | None = None) -> None:
        self.cmds = tuple(cmds)
        self.force_whitespace = force_whitespace

    def __repr__(self) -> str:
        return f"Command(cmds={self.cmds})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, CommandRule) and frozenset(self.cmds) == frozenset(other.cmds)

    def __hash__(self) -> int:
        return hash((frozenset(self.cmds),))

    async def __call__(
        self,
        cmd: tuple[str, ...] | None = Command(),
        cmd_arg: Message | None = CommandArg(),
        cmd_whitespace: str | None = CommandWhitespace(),
    ) -> bool:
        if cmd not in self.cmds:
            return False
        if self.force_whitespace is None or not cmd_arg:
            return True
        if isinstance(self.force_whitespace, str):
            return self.force_whitespace == cmd_whitespace
        return self.force_whitespace == (cmd_whitespace is not None)


def command(*cmds: str | tuple[str, ...], force_whitespace: str | bool | None = None) -> Rule:
    """匹配消息命令（``/test``、``/test.sub`` 形式，取决于 ``command_start`` / ``command_sep``）。"""
    config = get_driver().config
    command_start = set(getattr(config, "command_start", None) or {"/"})
    command_sep = set(getattr(config, "command_sep", None) or {"."})
    commands: list[tuple[str, ...]] = []
    for raw_command in cmds:
        one = (raw_command,) if isinstance(raw_command, str) else raw_command
        commands.append(one)
        if len(one) == 1:
            for start in command_start:
                TrieRule.add_prefix(f"{start}{one[0]}", TRIE_VALUE(start, one))
        else:
            for start, sep in product(command_start, command_sep):
                TrieRule.add_prefix(f"{start}{sep.join(one)}", TRIE_VALUE(start, one))
    return Rule(CommandRule(commands, force_whitespace))


class ArgumentParser(argparse.ArgumentParser):
    """把 ``argparse`` 的 ``error`` / ``exit`` 转成 :class:`ParserExit`（上游同义）。

    只改这两个钩子；``add_argument`` / ``add_subparsers`` 等全部沿用标准库，
    所以插件里写的参数定义可以原样使用。
    """

    def _print_message(self, message: str, file: Any = None) -> None:  # noqa: D401
        """上游同样不往 stdout 打帮助文本（由插件自己决定怎么回话）。"""

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        raise ParserExit(status, message)

    def error(self, message: str) -> NoReturn:
        raise ParserExit(2, message)


class ShellCommandRule:
    """按 shell 词法切分命令参数并交给 ``argparse`` 解析（上游同义）。

    解析结果写进 ``state[SHELL_ARGS]``（``argparse.Namespace``）与
    ``state[SHELL_ARGV]``（原始 argv 列表），分别由 :func:`nonebot.params.ShellCommandArgs`
    与 :func:`nonebot.params.ShellCommandArgv` 取用。
    """

    def __init__(self, cmds: list[tuple[str, ...]], parser: ArgumentParser) -> None:
        self.cmds = tuple(cmds)
        self.parser = parser

    def __repr__(self) -> str:
        return f"ShellCommand(cmds={self.cmds})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ShellCommandRule) and frozenset(self.cmds) == frozenset(other.cmds)

    def __hash__(self) -> int:
        return hash((frozenset(self.cmds),))

    async def __call__(
        self,
        state: T_State,
        cmd: tuple[str, ...] | None = Command(),
        cmd_arg: Message | None = CommandArg(),
    ) -> bool:
        if cmd not in self.cmds:
            return False
        raw = str(cmd_arg) if cmd_arg is not None else ""
        try:
            argv = shlex.split(raw)
        except ValueError as exc:  # 引号没配对这类词法错误
            raise ParserExit(2, f"参数无法解析：{exc}") from exc
        try:
            namespace = self.parser.parse_args(argv)
        except SystemExit as exc:  # argparse 绕过 error() 直接退出的情形
            raise ParserExit(int(getattr(exc, "code", 0) or 0), None) from exc
        state[SHELL_ARGS] = namespace
        state[SHELL_ARGV] = argv
        return True


def shell_command(cmd: str | tuple[str, ...], parser: ArgumentParser) -> Rule:
    """匹配 shell 风格命令并用 ``parser`` 解析参数（上游同义）。"""
    commands = [(cmd,)] if isinstance(cmd, str) else [tuple(cmd)]
    return Rule(ShellCommandRule(commands, parser))


class RegexRule:
    """检查消息字符串是否符合指定正则表达式。"""

    def __init__(self, regex: str, flags: int = 0) -> None:
        self.regex = regex
        self.flags = flags

    def __repr__(self) -> str:
        return f"Regex(regex={self.regex!r}, flags={self.flags})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, RegexRule) and self.regex == other.regex and self.flags == other.flags

    def __hash__(self) -> int:
        return hash((self.regex, self.flags))

    async def __call__(self, event: Event, state: T_State) -> bool:
        try:
            msg = event.get_message()
        except Exception:  # noqa: BLE001 — 事件没有消息内容时不匹配（上游同）
            return False
        if matched := re.search(self.regex, str(msg), self.flags):
            state[REGEX_MATCHED] = matched
            return True
        return False


def regex(regex: str, flags: int | re.RegexFlag = 0) -> Rule:
    """匹配符合正则表达式的消息字符串（使用 ``search``，匹配对象存进 ``RegexGroup()``）。"""
    return Rule(RegexRule(regex, flags))


class ToMeRule:
    """检查事件是否与机器人有关。"""

    def __repr__(self) -> str:
        return "ToMe()"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ToMeRule)

    def __hash__(self) -> int:
        return hash((self.__class__,))

    async def __call__(self, to_me: bool = EventToMe()) -> bool:
        return to_me


def to_me() -> Rule:
    """匹配与机器人有关的事件。"""
    return Rule(ToMeRule())


class IsTypeRule:
    """检查事件类型是否为指定类型。"""

    def __init__(self, *types: type[Event]) -> None:
        self.types = types

    def __repr__(self) -> str:
        return f"IsType(types={tuple(type_.__name__ for type_ in self.types)})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, IsTypeRule) and self.types == other.types

    def __hash__(self) -> int:
        return hash((self.types,))

    async def __call__(self, event: Event) -> bool:
        return isinstance(event, self.types)


def is_type(*types: type[Event]) -> Rule:
    """匹配事件类型。"""
    return Rule(IsTypeRule(*types))
