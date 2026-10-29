"""AstrBot 消息链 / 消息事件结果（N.E.K.O. shim 子集）。

类名、字段名与 ``astrbot.core.message.message_event_result`` 一致；除了
``typing_extensions.deprecated`` 换成标准库 ``warnings`` 之外没有语义差异。
"""

from __future__ import annotations

import enum
import warnings
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from .components import At, AtAll, BaseMessageComponent, Image, Json, Plain


@dataclass
class MessageChain:
    """一条富文本消息：有序的消息段列表。"""

    chain: list[BaseMessageComponent] = field(default_factory=list)
    use_t2i_: bool | None = None
    use_markdown_: bool | None = None
    type: str | None = None
    """消息链承载的消息类型（可选，业务场景标记）。"""

    def derive(self, chain: list[BaseMessageComponent] | None = None) -> MessageChain:
        """基于当前消息链复制一份元数据，返回新的消息链。"""
        new = MessageChain(chain=chain if chain is not None else [])
        new.use_t2i_ = self.use_t2i_
        new.use_markdown_ = self.use_markdown_
        new.type = self.type
        return new

    def message(self, message: str) -> MessageChain:
        """追加一段文本。"""
        self.chain.append(Plain(message))
        return self

    def at(self, name: str, qq: str | int) -> MessageChain:
        """追加一个 @ 消息段。"""
        self.chain.append(At(name=name, qq=qq))
        return self

    def at_all(self) -> MessageChain:
        """追加一个 @全体成员 消息段。"""
        self.chain.append(AtAll())
        return self

    def error(self, message: str) -> MessageChain:
        """[已废弃] 追加一段错误文本。"""
        warnings.warn(
            "MessageChain.error() is deprecated, use MessageChain.message() instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.chain.append(Plain(message))
        return self

    def url_image(self, url: str) -> MessageChain:
        """追加一张网络图片。"""
        self.chain.append(Image.fromURL(url))
        return self

    def file_image(self, path: str) -> MessageChain:
        """追加一张本地图片。"""
        self.chain.append(Image.fromFileSystem(path))
        return self

    def base64_image(self, base64_str: str) -> MessageChain:
        """追加一张 base64 图片。"""
        self.chain.append(Image.fromBase64(base64_str))
        return self

    def use_t2i(self, use_t2i: bool) -> MessageChain:
        """设置是否使用文本转图片服务。"""
        self.use_t2i_ = use_t2i
        return self

    def use_markdown(self, use: bool | None = True) -> MessageChain:
        """设置是否使用 Markdown 发送。"""
        self.use_markdown_ = use
        return self

    def get_plain_text(self, with_other_comps_mark: bool = False) -> str:
        """取出链中的纯文本（空格连接）。

        Args:
            with_other_comps_mark: 为 True 时给非文本消息段附上占位标记。
        """
        if not with_other_comps_mark:
            return " ".join(
                comp.text for comp in self.chain if isinstance(comp, Plain)
            )
        texts: list[str] = []
        for comp in self.chain:
            if isinstance(comp, Plain):
                texts.append(comp.text)
            elif isinstance(comp, Json):
                texts.append(f"{comp.data}")
            else:
                texts.append(f"[{comp.__class__.__name__}]")
        return " ".join(texts)

    def squash_plain(self) -> MessageChain | None:
        """把所有 Plain 段合并到第一个 Plain 段中。"""
        if not self.chain:
            return None
        new_chain: list[BaseMessageComponent] = []
        first_plain: Plain | None = None
        plain_texts: list[str] = []
        for comp in self.chain:
            if isinstance(comp, Plain):
                if first_plain is None:
                    first_plain = comp
                    new_chain.append(comp)
                plain_texts.append(comp.text)
            else:
                new_chain.append(comp)
        if first_plain is not None:
            first_plain.text = "".join(plain_texts)
        self.chain = new_chain
        return self

    def to_dict(self) -> dict[str, Any]:
        """shim 扩展：导出为 ``{"chain": [消息段字典...]}``。"""
        return {"chain": [comp.toDict() for comp in self.chain]}


class EventResultType(enum.Enum):
    """事件传播结果类型。"""

    CONTINUE = enum.auto()
    STOP = enum.auto()


class ResultContentType(enum.Enum):
    """事件结果内容的类型。"""

    LLM_RESULT = enum.auto()
    AGENT_RUNNER_ERROR = enum.auto()
    GENERAL_RESULT = enum.auto()
    STREAMING_RESULT = enum.auto()
    STREAMING_FINISH = enum.auto()


@dataclass
class MessageEventResult(MessageChain):
    """一条消息链 + 事件处理结果。"""

    result_type: EventResultType | None = field(
        default_factory=lambda: EventResultType.CONTINUE,
    )
    result_content_type: ResultContentType | None = field(
        default_factory=lambda: ResultContentType.GENERAL_RESULT,
    )
    async_stream: AsyncGenerator[Any, None] | None = None

    def stop_event(self) -> MessageEventResult:
        """终止事件传播。"""
        self.result_type = EventResultType.STOP
        return self

    def continue_event(self) -> MessageEventResult:
        """继续事件传播。"""
        self.result_type = EventResultType.CONTINUE
        return self

    def is_stopped(self) -> bool:
        """是否终止事件传播。"""
        return self.result_type == EventResultType.STOP

    def set_async_stream(
        self,
        stream: AsyncGenerator[Any, None],
    ) -> MessageEventResult:
        """设置异步流。"""
        self.async_stream = stream
        return self

    def set_result_content_type(self, typ: ResultContentType) -> MessageEventResult:
        """设置结果内容类型。"""
        self.result_content_type = typ
        return self

    def is_llm_result(self) -> bool:
        """是否为 LLM 结果。"""
        return self.result_content_type == ResultContentType.LLM_RESULT

    def is_model_result(self) -> bool:
        """是否为模型执行结果（含 runner 错误）。"""
        return self.result_content_type in (
            ResultContentType.LLM_RESULT,
            ResultContentType.AGENT_RUNNER_ERROR,
        )


# 上游为兼容旧代码保留的别名
CommandResult = MessageEventResult

__all__ = [
    "CommandResult",
    "EventResultType",
    "MessageChain",
    "MessageEventResult",
    "ResultContentType",
]
