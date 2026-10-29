"""本模块包含了 NoneBot 事件处理过程中使用到的常量（上游 ``nonebot.consts` 的忠实子集）。

对应上游文件：``nonebot/consts.py``（值完全一致，见 shim 包文档的偏差清单）。
"""

from __future__ import annotations

# used by Matcher
RECEIVE_KEY = "_receive_{id}"
LAST_RECEIVE_KEY = "_last_receive"
ARG_KEY = "{key}"
REJECT_TARGET = "_current_target"
REJECT_CACHE_TARGET = "_next_target"
PAUSE_PROMPT_RESULT_KEY = "_pause_result"
REJECT_PROMPT_RESULT_KEY = "_reject_{key}_result"

# used by Rule
PREFIX_KEY = "_prefix"
CMD_KEY = "command"
RAW_CMD_KEY = "raw_command"
CMD_ARG_KEY = "command_arg"
CMD_START_KEY = "command_start"
CMD_WHITESPACE_KEY = "command_whitespace"

SHELL_ARGS = "_args"
SHELL_ARGV = "_argv"

REGEX_MATCHED = "_matched"
STARTSWITH_KEY = "_startswith"
ENDSWITH_KEY = "_endswith"
FULLMATCH_KEY = "_fullmatch"
KEYWORD_KEY = "_keyword"

WINDOWS = False

__all__ = [
    "ARG_KEY",
    "CMD_ARG_KEY",
    "CMD_KEY",
    "CMD_START_KEY",
    "CMD_WHITESPACE_KEY",
    "ENDSWITH_KEY",
    "FULLMATCH_KEY",
    "KEYWORD_KEY",
    "LAST_RECEIVE_KEY",
    "PAUSE_PROMPT_RESULT_KEY",
    "PREFIX_KEY",
    "RAW_CMD_KEY",
    "RECEIVE_KEY",
    "REGEX_MATCHED",
    "REJECT_CACHE_TARGET",
    "REJECT_PROMPT_RESULT_KEY",
    "REJECT_TARGET",
    "SHELL_ARGS",
    "SHELL_ARGV",
    "STARTSWITH_KEY",
    "WINDOWS",
]
