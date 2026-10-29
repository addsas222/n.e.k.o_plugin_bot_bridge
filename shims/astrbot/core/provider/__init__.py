"""AstrBot Provider（模型提供商）与相关数据类。

N.E.K.O. 只有一个对话模型槽位（``plugin.toml`` 里声明），因此这里提供一个
"Provider 代理"：``text_chat()`` 落到 ``HostServices.llm_generate``，其余上游能力
（流式、音频输入、ToolSet、工具调用结果回传、多 provider 查询）一律抛
:class:`astrbot.UnsupportedCapabilityError`。

``contexts``（OpenAI 格式）会原样透传给 ``llm_generate``；``image_urls`` 会编码成
OpenAI 多模态 content parts。
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncGenerator, Mapping, Sequence
from enum import Enum
from typing import Any

from ..._runtime import UnsupportedCapabilityError
from ..message.message_event_result import MessageChain


class ProviderError(RuntimeError):
    """N.E.K.O. 模型网关返回失败时抛出（``success=False``）。"""

    code = "provider_error"

    def __init__(self, detail: str, *, error_code: str = "") -> None:
        super().__init__(detail or "N.E.K.O. model gateway failed")
        self.detail = detail
        self.error_code = error_code


class ProviderType(Enum):
    """Provider 能力类型（与上游取值一致）。"""

    CHAT_COMPLETION = "chat_completion"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_TO_SPEECH = "text_to_speech"
    EMBEDDING = "embedding"
    RERANK = "rerank"


@dataclasses.dataclass
class ProviderMeta:
    """Provider 实例的元数据。"""

    id: str
    model: str | None
    type: str
    provider_type: ProviderType = ProviderType.CHAT_COMPLETION


@dataclasses.dataclass
class ProviderMetaData(ProviderMeta):
    """Provider 适配器注册元数据。"""

    desc: str = ""
    cls_type: Any = None
    default_config_tmpl: dict[str, Any] | None = None
    provider_display_name: str | None = None


@dataclasses.dataclass
class ProviderRequest:
    """一次 LLM 请求（供 ``@filter.on_llm_request`` 钩子修改）。"""

    prompt: str | None = None
    session_id: str | None = ""
    image_urls: list[str] = dataclasses.field(default_factory=list)
    audio_urls: list[str] = dataclasses.field(default_factory=list)
    extra_user_content_parts: list[Any] = dataclasses.field(default_factory=list)
    func_tool: Any = None
    contexts: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    system_prompt: str = ""
    conversation: Any = None
    tool_calls_result: Any = None
    model: str | None = None


class LLMResponse:
    """一次 LLM 响应（字段与上游同名）。"""

    def __init__(
        self,
        role: str,
        completion_text: str | None = None,
        result_chain: MessageChain | None = None,
        tools_call_args: list[dict[str, Any]] | None = None,
        tools_call_name: list[str] | None = None,
        tools_call_ids: list[str] | None = None,
        tools_call_extra_content: dict[str, dict[str, Any]] | None = None,
        reasoning_content: str | None = None,
        reasoning_signature: str | None = None,
        raw_completion: Any = None,
        is_chunk: bool = False,
        id: str | None = None,
        usage: Any = None,
    ) -> None:
        self.role = role
        self.completion_text = completion_text
        self.result_chain = result_chain
        self.tools_call_args = tools_call_args or []
        self.tools_call_name = tools_call_name or []
        self.tools_call_ids = tools_call_ids or []
        self.tools_call_extra_content = tools_call_extra_content or {}
        self.reasoning_content = reasoning_content
        self.reasoning_signature = reasoning_signature
        self.raw_completion = raw_completion
        """shim 差异：这里放 N.E.K.O. ``llm_generate`` 的原始结果字典，而非 OpenAI 对象。"""
        self.is_chunk = is_chunk
        self.id = id
        self.usage = usage

    def __repr__(self) -> str:
        return (
            f"LLMResponse(role={self.role!r}, model={self._model()!r}, "
            f"text_len={len(self.completion_text or '')})"
        )

    def _model(self) -> str:
        raw = self.raw_completion
        if isinstance(raw, Mapping):
            return str(raw.get("model", ""))
        return ""

    def __str__(self) -> str:
        return self.completion_text or ""


class Provider:
    """落到 N.E.K.O. 模型网关的 Provider 代理。"""

    def __init__(
        self,
        host: Any,
        plugin_id: str,
        *,
        provider_id: str = "neko",
        model: str = "",
        provider_type: ProviderType = ProviderType.CHAT_COMPLETION,
    ) -> None:
        self._neko_host = host
        self._neko_plugin_id = plugin_id
        self._provider_id = provider_id or "neko"
        self._model = model
        self.provider_type = provider_type

    # -- 元数据 ------------------------------------------------------------ #

    def meta(self) -> ProviderMeta:
        """Provider 元数据。"""
        return ProviderMeta(
            id=self._provider_id,
            model=self._model or None,
            type="neko",
            provider_type=self.provider_type,
        )

    def get_model(self) -> str:
        """当前模型名（桥接层不指定时为宿主默认模型，这里返回空串）。"""
        return self._model

    async def get_models(self) -> list[str]:
        raise UnsupportedCapabilityError(
            "provider.get_models",
            "N.E.K.O. 只暴露一个对话模型槽位，不提供模型列表",
        )

    def set_model(self, model_name: str) -> None:
        raise UnsupportedCapabilityError(
            "provider.set_model",
            f"模型由 N.E.K.O. 宿主绑定，插件无法切换（请求切换 {model_name!r}）",
        )

    # -- 对话 -------------------------------------------------------------- #

    async def text_chat(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: Any = None,
        contexts: list[Any] | None = None,
        system_prompt: str | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        extra_user_content_parts: list[Any] | None = None,
        tool_choice: str = "auto",
        request_max_retries: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """请求一次文本对话。

        Raises:
            UnsupportedCapabilityError: 用到了 N.E.K.O. 没有的能力。
            ProviderError: N.E.K.O. 模型网关返回失败。
        """
        if audio_urls:
            raise UnsupportedCapabilityError(
                "provider.audio_input",
                "N.E.K.O. llm_generate 不接受音频输入",
            )
        if func_tool is not None:
            raise UnsupportedCapabilityError(
                "provider.func_tool",
                "AstrBot 的 ToolSet 没有映射；N.E.K.O. 的工具链由桥接层注入",
            )
        if tool_calls_result is not None:
            raise UnsupportedCapabilityError(
                "provider.tool_calls_result",
                "N.E.K.O. llm_generate 不接受工具调用结果回传",
            )
        if extra_user_content_parts:
            raise UnsupportedCapabilityError(
                "provider.extra_user_content_parts",
                "N.E.K.O. llm_generate 不支持额外内容块",
            )
        if tool_choice and tool_choice != "auto":
            raise UnsupportedCapabilityError(
                "provider.tool_choice",
                f"N.E.K.O. llm_generate 不支持 tool_choice={tool_choice!r}",
            )
        ignored = {
            key: value
            for key, value in kwargs.items()
            if key not in ("temperature", "max_tokens")
        }
        temperature = kwargs.get("temperature")
        max_tokens = kwargs.get("max_tokens")
        messages = _convert_contexts(contexts)
        images = [str(url) for url in (image_urls or [])]
        request_prompt: str | list[dict[str, Any]]
        if messages:
            if prompt or images:
                messages.append(_user_message(prompt or "", images))
            request_prompt = messages
        elif images:
            request_prompt = [_user_message(prompt or "", images)]
        else:
            request_prompt = prompt or ""
        if ignored or session_id or request_max_retries is not None:
            self._neko_note(
                "provider.text_chat ignored unsupported arguments: "
                f"kwargs={sorted(ignored)} session_id={session_id!r} "
                f"request_max_retries={request_max_retries!r}",
            )
        result = await self._neko_host.llm_generate(
            self._neko_plugin_id,
            prompt=request_prompt,
            system_prompt=system_prompt or "",
            model=model or self._model or "",
            image_urls=tuple(images),
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if not result.get("success", False):
            raise ProviderError(
                str(result.get("error", "")) or "N.E.K.O. model gateway returned a failure",
                error_code=str(result.get("code", "")),
            )
        text = str(result.get("response", ""))
        reasoning = result.get("reasoning") or None
        if model:
            self._model = model
        elif result.get("model"):
            self._model = str(result["model"])
        return LLMResponse(
            role="assistant",
            completion_text=text,
            result_chain=MessageChain().message(text) if text else MessageChain(),
            reasoning_content=str(reasoning) if reasoning else None,
            raw_completion=result,
        )

    async def text_chat_stream(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: Any = None,
        contexts: list[Any] | None = None,
        system_prompt: str | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        tool_choice: str = "auto",
        request_max_retries: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[LLMResponse, None]:
        """流式对话（未映射）。"""
        del (
            prompt,
            session_id,
            image_urls,
            audio_urls,
            func_tool,
            contexts,
            system_prompt,
            tool_calls_result,
            model,
            tool_choice,
            request_max_retries,
            kwargs,
        )
        raise UnsupportedCapabilityError(
            "provider.text_chat_stream",
            "N.E.K.O. llm_generate 是整段返回，没有流式通道",
        )
        yield  # pragma: no cover - 使本方法成为异步生成器

    def _neko_note(self, message: str) -> None:
        logger = getattr(self._neko_host, "log", None)
        if callable(logger):
            logger(
                "debug",
                message,
                plugin_id=self._neko_plugin_id,
                source="astrbot.provider",
            )


def _convert_contexts(contexts: Sequence[Any] | None) -> list[dict[str, Any]]:
    """把 AstrBot 的 contexts 统一成 OpenAI 格式的消息字典列表。"""
    messages: list[dict[str, Any]] = []
    for item in contexts or []:
        if isinstance(item, Mapping):
            messages.append(dict(item))
        elif hasattr(item, "model_dump"):
            messages.append(dict(item.model_dump()))
        else:
            raise UnsupportedCapabilityError(
                "provider.contexts",
                f"不支持的上下文元素类型：{type(item).__name__}",
            )
    return messages


def _user_message(text: str, images: Sequence[str]) -> dict[str, Any]:
    """构造一条用户消息；带图片时使用 OpenAI 多模态 content parts。"""
    if not images:
        return {"role": "user", "content": text}
    parts: list[dict[str, Any]] = []
    if text:
        parts.append({"type": "text", "text": text})
    parts.extend({"type": "image_url", "image_url": {"url": url}} for url in images)
    return {"role": "user", "content": parts}


__all__ = [
    "LLMResponse",
    "Provider",
    "ProviderError",
    "ProviderMeta",
    "ProviderMetaData",
    "ProviderRequest",
    "ProviderType",
]
