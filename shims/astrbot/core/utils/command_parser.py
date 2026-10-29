"""指令文本解析 Mixin（N.E.K.O. shim 子集，与上游同语义）。"""

from __future__ import annotations

import re


class CommandTokens:
    """按空白切分出的指令 token 列表。"""

    def __init__(self) -> None:
        self.tokens: list[str] = []
        self.len = 0

    def get(self, idx: int) -> str | None:
        """取第 ``idx`` 个 token（越界返回 ``None``）。"""
        if idx >= self.len:
            return None
        return self.tokens[idx].strip()


class CommandParserMixin:
    """供插件解析指令文本的辅助方法。"""

    def parse_commands(self, message: str) -> CommandTokens:
        """按空白把消息切成 token。"""
        cmd_tokens = CommandTokens()
        cmd_tokens.tokens = re.split(r"\s+", message)
        cmd_tokens.len = len(cmd_tokens.tokens)
        return cmd_tokens

    def regex_match(self, message: str, command: str) -> bool:
        """消息是否匹配正则。"""
        return re.search(command, message, re.MULTILINE) is not None


__all__ = ["CommandParserMixin", "CommandTokens"]
