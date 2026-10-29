"""``discord.ext.commands.view``：参数分词（``StringView`` / ``quoted_word``）。

与上游同名同义：支持引号包裹的参数（``!say "hello world"``）、引号不配对时报
``ArgumentParsingError`` 家族异常。``Command`` 的参数解析建立在这上面。
"""

from __future__ import annotations

from typing import Any

from ...errors import (
    ExpectedClosingQuoteError,
    InvalidEndOfQuotedStringError,
    UnexpectedQuoteError,
)

__all__ = ["StringView", "quoted_word"]

#: 当作参数分隔符的空白字符（上游 ``str.isspace``）。
_WHITESPACE = frozenset(" \t\n\r\v\f")


class StringView:
    """一个带游标的字符串视图（上游 ``discord.ext.commands.view.StringView``）。"""

    def __init__(self, buffer: str) -> None:
        self.index = 0
        self.buffer = str(buffer)
        self.end = len(self.buffer)
        self.previous = 0

    # ---- 基本操作 -------------------------------------------------------- #

    @property
    def current(self) -> str | None:
        return None if self.eof else self.buffer[self.index]

    @property
    def eof(self) -> bool:
        return self.index >= self.end

    def undo(self) -> None:
        """回退到上一次 ``get_word`` / ``skip_ws`` 之间的位置（上游语义）。"""
        self.index = self.previous

    def skip_ws(self) -> bool:
        """跳过空白；返回是否跳过了至少一个字符。"""
        pos = 0
        while not self.eof and self.buffer[self.index] in _WHITESPACE:
            pos += 1
            self.index += 1
        self.previous = self.index
        return pos > 0

    def skip_string(self, string: str) -> bool:
        """跳过给定前缀（区分大小写）；跳不动时游标不动。"""
        length = len(string)
        if self.buffer[self.index : self.index + length] == string:
            self.index += length
            return True
        return False

    def read_rest(self) -> str:
        """吃掉剩下的全部内容（并跳过末尾空白）。"""
        result = self.buffer[self.index :]
        self.index = self.end
        self.previous = self.index
        return result

    def read(self, n: int) -> str:
        """吃 n 个字符（不检查边界，与上游一致）。"""
        result = self.buffer[self.index : self.index + n]
        self.index += n
        self.previous = self.index
        return result

    def get_word(self) -> str:
        """取一个词（到下一个空白或结尾）；返回 ``''`` 表示已经到了末尾。"""
        result = []
        while not self.eof:
            char = self.buffer[self.index]
            if char in _WHITESPACE:
                break
            result.append(char)
            self.index += 1
        self.previous = self.index
        return "".join(result)

    def get_quoted_word(self) -> str | None:
        """取一个参数，支持引号（上游 ``StringView.get_quoted_word`` 的语义）。

        规则：跳过前导空白 → 若以 ``"`` / ``'`` 开头则一直读到配对的引号，且引号后面
        必须是空白或结尾；否则读到下一个空白。参数中间出现引号报错（与上游一致）。
        """
        current = self.current
        if current is None:
            return None
        if current in _WHITESPACE:
            self.skip_ws()
            if not self.skip_string("'"):
                if not self.skip_string('"'):
                    self.skip_ws()
                    return self._read_unquoted()
                else:
                    return self._read_quoted('"')
            else:
                return self._read_quoted("'")
        if current in ("'", '"'):
            if not self.skip_string(current):
                raise UnexpectedQuoteError(current)
            return self._read_quoted(current)
        return self._read_unquoted()

    # ---- 内部 ------------------------------------------------------------ #

    def _read_unquoted(self) -> str:
        result = []
        while not self.eof:
            char = self.buffer[self.index]
            if char in _WHITESPACE:
                break
            if char in ("'", '"'):
                raise UnexpectedQuoteError(char)
            result.append(char)
            self.index += 1
        self.previous = self.index
        return "".join(result)

    def _read_quoted(self, close_quote: str) -> str:
        result = []
        while not self.eof:
            char = self.buffer[self.index]
            if char == close_quote:
                self.index += 1
                closing = self.current
                if closing is not None and closing not in _WHITESPACE:
                    raise InvalidEndOfQuotedStringError(closing)
                break
            result.append(char)
            self.index += 1
        else:
            raise ExpectedClosingQuoteError(close_quote)
        self.previous = self.index
        return "".join(result)

    def __repr__(self) -> str:
        return f"<StringView index={self.index} end={self.end}>"

    def __len__(self) -> int:
        return self.end


def quoted_word(view: StringView) -> str | None:
    """``StringView.get_quoted_word`` 的函数式别名（上游同名函数）。"""
    return view.get_quoted_word()


def get_signature_parameters(func: Any) -> dict[str, Any]:
    """返回函数签名的参数表（跳过 ``self`` / ``ctx`` / ``context``）。

    ``from __future__ import annotations`` 会让注解变成字符串，因此这里用
    ``typing.get_type_hints`` 解析；解析不了（前向引用指向插件外部名字）就退回原始注解，
    由参数转换层如实报「不支持的注解」。
    """
    import inspect
    import typing

    signature = inspect.signature(func)
    hints: dict[str, Any] = {}
    try:
        hints = typing.get_type_hints(func)
    except Exception:  # noqa: BLE001 — 注解解析失败不该让命令注册失败
        hints = {}
    params: dict[str, Any] = {}
    for name, param in signature.parameters.items():
        if name in ("self", "ctx", "context"):
            continue
        if param.annotation is not inspect.Parameter.empty and name in hints:
            param = param.replace(annotation=hints[name])
        params[name] = param
    return params
