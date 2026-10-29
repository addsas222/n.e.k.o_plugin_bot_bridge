"""``hoshino.trigger`` 的兼容实现：前缀 / 后缀 / 关键词 / 正则 / 全匹配触发器。

上游用 pygtrie 做前缀/后缀的最长匹配、用 zhconv 做繁简互通、用 ``util.normalize_str``
做规范化。shim 只依赖标准库，因此：

* 最长匹配用"按长度倒序的第一个命中"实现（与 trie 的 ``longest_prefix`` 等价）；
* 繁简互通**没有**（缺 zhconv）—— 记在兼容层 docstring 的已知缺口里；
* ``normalize_text=True`` 的匹配用 ``NFKC + 小写``（与上游 ``normalize_str`` 差一步 zhconv）。

匹配语义忠实于上游 ``hoshino/trigger.py``：

* ``prefix`` / ``suffix`` 会在运行 handler 期间**就地改写** ``event.message``（把命中的
  前后缀摘掉），handler 拿到的是参数；handler 返回后恢复原消息；
* 命中时写入 ``event['prefix']`` / ``event['suffix']`` / ``event['match']``；
* ``chain`` 的顺序就是分发顺序：prefix → suffix → 规范化 → rex → keyword。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Any

from aiocqhttp import _runtime as output
from aiocqhttp.control import SwitchException

from hoshino import _runtime

if TYPE_CHECKING:  # pragma: no cover — 只为类型标注，避免循环导入
    from hoshino.service import ServiceFunc


class BaseTrigger:
    """触发器的公共接口。"""

    #: 触发器类别（用于规格描述）。
    kind = "base"

    def __init__(self) -> None:
        self._entries: list[tuple[Any, "ServiceFunc"]] = []

    # ---- 注册 ------------------------------------------------------------ #

    def add(self, value: Any, sf: "ServiceFunc") -> None:
        raise NotImplementedError

    def _register(self, value: Any, sf: "ServiceFunc", *, duplicate_hint: bool = True) -> None:
        for existing, _ in self._entries:
            if existing == value:
                if duplicate_hint:
                    output.emit_log(
                        "warning",
                        f"{self.kind} 触发器 {value!r} 被重复注册（{sf.__name__}@{sf.sv.name}）",
                        source=sf.plugin_id,
                    )
                break
        self._entries.append((value, sf))

    def clear_plugin(self, plugin_id: str) -> int:
        """摘掉某个插件的全部注册（重复 load / 卸载时用），返回摘掉的数量。"""
        before = len(self._entries)
        self._entries = [(value, sf) for value, sf in self._entries if sf.plugin_id != plugin_id]
        return before - len(self._entries)

    def entries(self, plugin_id: str = "") -> list[tuple[Any, "ServiceFunc"]]:
        """按注册顺序列出条目（可按插件过滤）。"""
        if not plugin_id:
            return list(self._entries)
        return [(value, sf) for value, sf in self._entries if sf.plugin_id == plugin_id]

    # ---- 匹配 ------------------------------------------------------------ #

    def text_of(self, event: Any, sf: "ServiceFunc") -> str:
        """该处理器应该匹配的文本。"""
        return event.norm_text if sf.normalize_text else event.plain_text

    def find_handler(self, event: Any, plugin_id: str = "") -> Iterator["ServiceFunc"]:
        """列出这条事件命中的处理器（上游签名；``plugin_id`` 是本 shim 的过滤扩展）。"""
        raise NotImplementedError


class PrefixTrigger(BaseTrigger):
    """前缀触发器：命中最长的已注册前缀。"""

    kind = "prefix"

    def add(self, value: Any, sf: "ServiceFunc") -> None:
        if not isinstance(value, str):
            raise TypeError(f"prefix trigger expects str, got {type(value).__name__}")
        self._register(value, sf)

    def find_handler(self, event: Any, plugin_id: str = "") -> Iterator["ServiceFunc"]:
        first = event.message[0] if len(event.message) else None
        if first is None or first.type != "text":
            return
        first_text = str(first.data.get("text") or "").lstrip()
        matched, handlers = self._longest(first_text, plugin_id)
        if matched is None:
            return
        original = event.message.copy()
        event["prefix"] = matched
        remainder = first_text[len(matched) :].lstrip()
        try:
            if not remainder and len(event.message) > 1:
                del event.message[0]
            else:
                first.data["text"] = remainder
            # 消息被改写了，plain_text / norm_text 的缓存要失效（属性会按新消息重算）。
            event.reset_text_cache()
            for sf in handlers:
                sf._matched_value = matched
                yield sf
        finally:
            event.message = original
            event.reset_text_cache()

    def _longest(self, text: str, plugin_id: str) -> tuple[str | None, list["ServiceFunc"]]:
        best: str | None = None
        for value, _ in self.entries(plugin_id):
            if text.startswith(value) and (best is None or len(value) > len(best)):
                best = value
        if best is None:
            return None, []
        return best, [sf for value, sf in self.entries(plugin_id) if value == best]


class SuffixTrigger(BaseTrigger):
    """后缀触发器：命中最长的已注册后缀。"""

    kind = "suffix"

    def add(self, value: Any, sf: "ServiceFunc") -> None:
        if not isinstance(value, str):
            raise TypeError(f"suffix trigger expects str, got {type(value).__name__}")
        self._register(value, sf)

    def find_handler(self, event: Any, plugin_id: str = "") -> Iterator["ServiceFunc"]:
        last = event.message[-1] if len(event.message) else None
        if last is None or last.type != "text":
            return
        last_text = str(last.data.get("text") or "").rstrip()
        matched, handlers = self._longest(last_text, plugin_id)
        if matched is None:
            return
        original = event.message.copy()
        event["suffix"] = matched
        remainder = last_text[: -len(matched)].rstrip()
        try:
            if not remainder and len(event.message) > 1:
                del event.message[-1]
            else:
                last.data["text"] = remainder
            event.reset_text_cache()
            for sf in handlers:
                sf._matched_value = matched
                yield sf
        finally:
            event.message = original
            event.reset_text_cache()

    def _longest(self, text: str, plugin_id: str) -> tuple[str | None, list["ServiceFunc"]]:
        best: str | None = None
        for value, _ in self.entries(plugin_id):
            if text.endswith(value) and (best is None or len(value) > len(best)):
                best = value
        if best is None:
            return None, []
        return best, [sf for value, sf in self.entries(plugin_id) if value == best]


class KeywordTrigger(BaseTrigger):
    """关键词触发器：文本里出现关键词即命中。"""

    kind = "keyword"
    normalizes = True

    def add(self, value: Any, sf: "ServiceFunc") -> None:
        if not isinstance(value, str):
            raise TypeError(f"keyword trigger expects str, got {type(value).__name__}")
        stored = _runtime.normalize(value) if sf.normalize_text else value
        self._register(stored, sf)

    def find_handler(self, event: Any, plugin_id: str = "") -> Iterator["ServiceFunc"]:
        for keyword, sf in self.entries(plugin_id):
            if keyword and keyword in self.text_of(event, sf):
                sf._matched_value = keyword
                yield sf


class RexTrigger(BaseTrigger):
    """正则触发器：``re.search`` 命中即触发，并把 match 写进 ``event['match']``。"""

    kind = "rex"
    normalizes = True

    def add(self, value: Any, sf: "ServiceFunc") -> None:
        if isinstance(value, str):
            value = re.compile(value)
        if not isinstance(value, re.Pattern):
            raise TypeError(f"rex trigger expects str or re.Pattern, got {type(value).__name__}")
        self._register(value, sf, duplicate_hint=False)

    def find_handler(self, event: Any, plugin_id: str = "") -> Iterator["ServiceFunc"]:
        for pattern, sf in self.entries(plugin_id):
            match = pattern.search(self.text_of(event, sf))
            if match:
                event["match"] = match
                sf._matched_value = pattern.pattern
                yield sf


class _PlainTextExtractor(BaseTrigger):
    """把 ``event.plain_text`` 填上（上游链里的第一个"非触发器"环节）。"""

    kind = "plain_text"

    def find_handler(self, event: Any, plugin_id: str = "") -> Iterator["ServiceFunc"]:
        event.plain_text = event.message.extract_plain_text().strip()
        return iter(())


class _TextNormalizer(_PlainTextExtractor):
    """在 ``plain_text`` 之上再填 ``norm_text``。"""

    kind = "normalizer"

    def find_handler(self, event: Any, plugin_id: str = "") -> Iterator["ServiceFunc"]:
        event.plain_text = event.message.extract_plain_text().strip()
        event.norm_text = _runtime.normalize(event.plain_text)
        return iter(())


#: 上游同名实例（插件可以用 ``from hoshino.trigger import prefix`` 之类直接加触发器）。
prefix = PrefixTrigger()
suffix = SuffixTrigger()
keyword = KeywordTrigger()
rex = RexTrigger()

#: 分发顺序（与上游 ``hoshino/trigger.py`` 的 ``chain`` 一致）。
chain: list[BaseTrigger] = [prefix, suffix, _TextNormalizer(), rex, keyword]


def fullmatch(word: str, sf: "ServiceFunc") -> None:
    """全匹配触发器：实现方式与上游一致 —— 注册成前缀，再把「后面还有东西」的情况拒掉。"""
    from functools import wraps

    from hoshino.service import ServiceFunc

    if not isinstance(word, str):
        raise TypeError(f"fullmatch trigger expects str, got {type(word).__name__}")

    @wraps(sf.func)
    async def _wrapper(bot: Any, event: Any) -> Any:
        if event.message.extract_plain_text().strip():
            # 前缀已摘掉；还剩下内容说明不是「整条消息就等于这个词」。
            raise SwitchException(f"fullmatch trigger {word!r} did not consume the whole message")
        return await sf.func(bot, event)

    proxy = ServiceFunc(sf.sv, _wrapper, sf.only_to_me, sf.normalize_text)
    prefix.add(word, proxy)


def entries_of(plugin_id: str) -> Iterable[tuple[str, Any, "ServiceFunc"]]:
    """列出某个插件在整条链上注册的触发器（``(kind, value, sf)``）。"""
    for item in chain:
        for value, sf in item.entries(plugin_id):
            yield item.kind, value, sf


__all__ = [
    "BaseTrigger",
    "KeywordTrigger",
    "PrefixTrigger",
    "RexTrigger",
    "SuffixTrigger",
    "_PlainTextExtractor",
    "_TextNormalizer",
    "chain",
    "entries_of",
    "fullmatch",
    "keyword",
    "prefix",
    "rex",
    "suffix",
]
