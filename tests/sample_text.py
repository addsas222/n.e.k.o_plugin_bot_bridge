"""把外来命令的触发规则变成几条「像人话」的候选文本。

只用于离线/真机自检脚本：外来命令的模式是正则，直接拿去当输入几乎一定匹配不上
（``^\\s*/?hello\\b`` 这种）。这里做保守的清理，并额外给出命令 id / 名字作为候选，
由调用方依次尝试，直到某一条命中。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

_NAMED_GROUP = re.compile(r"\(\?P<[^>]+>[^)]*\)")
_SIMPLE_GROUP = re.compile(r"\(([^()]*)\)")
_CLASS = re.compile(r"\[([^\]]+)\]")
_LEADING_PUNCT = re.compile(r"^[^0-9A-Za-z\u4e00-\u9fff]+")


def clean_pattern(pattern: str) -> str:
    """把一条命令正则压成可读文本（不追求等价，只求能命中）。"""
    text = str(pattern or "")
    text = text.strip("^$")
    text = _NAMED_GROUP.sub("示例", text)
    text = _SIMPLE_GROUP.sub(lambda match: match.group(1).replace("?:", ""), text)
    text = _CLASS.sub(lambda match: match.group(1)[:1], text)
    text = text.replace("\\s*", " ").replace("\\s+", " ").replace("\\s", " ")
    text = text.replace("\\b", "").replace("\\.", ".").replace("\\", "")
    text = text.replace("?P<", "").replace("?:", "")
    # 可选标记（/?、.* 之类）只是噪声。
    text = text.replace("/?", "/").replace(".*", "").replace(".+", "示例")
    text = re.sub(r"\?+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def candidates(command: Mapping[str, object]) -> list[str]:
    """给出尝试用的一组文本（去重、去空）。

    除了清理后的模式，还会给出 ``/xxx`` 形式 —— 多数机器人框架（NoneBot2 的
    ``COMMAND_START``、AstrBot 的唤醒前缀 + 斜杠）都要求命令以斜杠开头。
    """
    result: list[str] = []
    patterns = command.get("patterns")
    if isinstance(patterns, Sequence) and not isinstance(patterns, (str, bytes)):
        for pattern in patterns:
            cleaned = clean_pattern(str(pattern))
            _push(result, cleaned)
            _push(result, _LEADING_PUNCT.sub("", cleaned))
            _push(result, cleaned.replace("/", "").strip())
    _push(result, str(command.get("id") or "").replace("_", " ").strip())
    _push(result, str(command.get("name") or "").strip())
    for item in list(result):
        if not item.startswith("/"):
            _push(result, f"/{item}")
    return result


def _push(target: list[str], value: str) -> None:
    text = value.strip()
    if text and text not in target and not text.startswith("?") and re.search(r"[0-9A-Za-z\u4e00-\u9fff]", text):
        target.append(text)


__all__ = ["candidates", "clean_pattern"]
