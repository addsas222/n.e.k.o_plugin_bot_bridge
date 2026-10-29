"""``hoshino.util.textfilter.filter`` 的兼容实现（纯标准库）。

上游这三个类来自 HoshinoBot 的 ``hoshino/util/textfilter/filter.py``（原版是一个
流传很广的 DFA 敏感词实现）。本文件按**同名同签名**重写，行为对齐：

* :class:`NaiveFilter` —— 逐词 ``str.replace``；
* :class:`BSFilter` —— 命中后按词长整段替换（上游用它避免短词覆盖长词）；
* :class:`DFAFilter` —— 前缀树一次扫描，命中即替换为等长 ``repl``；
* :func:`test_first_character` —— 判断词表是否所有词首字相同（上游的建树优化判据）。

与上游的差异只有一处，且是刻意的：**没有内置词库**。上游把
``sensitive_words.txt`` 放在同目录；shim 不随包分发词表，``parse()`` 之前
``filter()`` 会抛 :class:`UnsupportedCapabilityError`，不假装过滤过。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

from aiocqhttp._runtime import UnsupportedCapabilityError

__all__ = ["BSFilter", "DFAFilter", "NaiveFilter", "test_first_character"]


def test_first_character(words: Iterable[str]) -> bool:
    """词表里所有非空词的首字是否相同（上游同名）。"""
    first = None
    for word in words:
        text = str(word or "").strip()
        if not text:
            continue
        if first is None:
            first = text[0]
        elif text[0] != first:
            return False
    return first is not None


def _load_words(path: Any) -> list[str]:
    if isinstance(path, (str, os.PathLike)):
        raw = Path(path).read_text(encoding="utf-8", errors="replace")
        return [line.strip() for line in raw.splitlines() if line.strip()]
    return [str(item).strip() for item in (path or ()) if str(item).strip()]


class NaiveFilter:
    """最朴素的逐词替换（上游同名）。"""

    def __init__(self) -> None:
        self.words: set[str] = set()

    def parse(self, path: Any) -> int:
        for word in _load_words(path):
            self.words.add(word)
        return len(self.words)

    def filter(self, message: str, repl: str = "*") -> str:
        if not self.words:
            raise UnsupportedCapabilityError(
                "hoshino.util.textfilter.NaiveFilter.filter",
                "未装载词库；请先 parse(路径或词表)",
            )
        text = str(message)
        for word in sorted(self.words, key=len, reverse=True):
            text = text.replace(word, repl * len(word))
        return text


class BSFilter:
    """按词长从长到短替换，避免短词先命中把长词切碎（上游同名）。"""

    def __init__(self) -> None:
        self.words: set[str] = set()

    def add(self, keyword: str) -> bool:
        key = str(keyword or "").strip()
        if not key:
            return False
        self.words.add(key)
        return True

    def parse(self, path: Any) -> int:
        for word in _load_words(path):
            self.add(word)
        return len(self.words)

    def filter(self, message: str, repl: str = "*") -> str:
        if not self.words:
            raise UnsupportedCapabilityError(
                "hoshino.util.textfilter.BSFilter.filter",
                "未装载词库；请先 parse(路径或词表)",
            )
        text = str(message)
        for word in sorted(self.words, key=len, reverse=True):
            text = text.replace(word, repl * len(word))
        return text


class DFAFilter:
    """前缀树（DFA）一次扫描的敏感词过滤器（上游同名）。"""

    def __init__(self) -> None:
        self._trie: dict[str, Any] = {}

    # -- 建树 ---------------------------------------------------------------
    def add(self, keyword: str) -> bool:
        key = str(keyword or "").strip()
        if not key:
            return False
        node = self._trie
        for char in key:
            node = node.setdefault(char, {})
        node["\x00"] = True
        return True

    def parse(self, path: Any) -> int:
        count = 0
        for word in _load_words(path):
            if self.add(word):
                count += 1
        return count

    # -- 过滤 ---------------------------------------------------------------
    def filter(self, message: str, repl: str = "*") -> str:
        if not self._trie:
            raise UnsupportedCapabilityError(
                "hoshino.util.textfilter.DFAFilter.filter",
                "未装载词库；请先 parse(路径或词表)",
            )
        text = str(message)
        out: list[str] = []
        index = 0
        length = len(text)
        while index < length:
            node = self._trie
            cursor = index
            last_hit = -1
            while cursor < length:
                node = node.get(text[cursor])  # type: ignore[assignment]
                if node is None:
                    break
                cursor += 1
                if "\x00" in node:
                    last_hit = cursor
            if last_hit > index:
                out.append(repl * (last_hit - index))
                index = last_hit
            else:
                out.append(text[index])
                index += 1
        return "".join(out)
