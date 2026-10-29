"""``astrbot.core.provider.entities`` —— 上游的 provider 数据类集合。

不少 AstrBot 插件写的是**深层路径**：

    from astrbot.core.provider.entities import LLMResponse

而 shim 只在 ``astrbot.api.provider`` 里提供了同一批名字。这里补一条别名路径，
让两种写法都能过。已兑现：``LLMResponse`` / ``ProviderType`` / ``ProviderMeta`` /
``ProviderMetaData`` / ``ProviderRequest`` / ``ProviderError``。

上游还有 ``TokenUsage`` / ``ToolCallsResult`` / ``RerankResult`` / ``Decision*`` 系列
（决策问答用的数据类）。它们没有 N.E.K.O. 等价实现，**故意不导出**：缺了就 ImportError，
而不是给个字段对不上的假 dataclass 让插件在运行期拿到错误数值。
"""

from __future__ import annotations

from astrbot.core.provider import (
    LLMResponse,
    ProviderError,
    ProviderMeta,
    ProviderMetaData,
    ProviderRequest,
    ProviderType,
)

__all__ = [
    "LLMResponse",
    "ProviderError",
    "ProviderMeta",
    "ProviderMetaData",
    "ProviderRequest",
    "ProviderType",
]
