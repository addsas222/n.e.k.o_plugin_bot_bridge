"""平台适配器过滤器（N.E.K.O. shim 子集，与上游同语义）。

注意：桥接层给事件填的平台名是 ``neko``，因此按具体平台类型（如 AIOCQHTTP）
过滤的 Handler 不会命中；``PlatformAdapterType.ALL`` 仍然命中。
"""

from __future__ import annotations

import enum

from ...config import AstrBotConfig
from ...platform.astr_message_event import AstrMessageEvent
from . import HandlerFilter


class PlatformAdapterType(enum.Flag):
    """平台适配器类型。"""

    AIOCQHTTP = enum.auto()
    QQOFFICIAL = enum.auto()
    QQOFFICIAL_WEBHOOK = enum.auto()
    TELEGRAM = enum.auto()
    WECOM = enum.auto()
    WECOM_AI_BOT = enum.auto()
    LARK = enum.auto()
    DINGTALK = enum.auto()
    DISCORD = enum.auto()
    SLACK = enum.auto()
    KOOK = enum.auto()
    VOCECHAT = enum.auto()
    WEIXIN_OFFICIAL_ACCOUNT = enum.auto()
    SATORI = enum.auto()
    MISSKEY = enum.auto()
    LINE = enum.auto()
    MATRIX = enum.auto()
    WEIXIN_OC = enum.auto()
    MATTERMOST = enum.auto()
    WEBCHAT = enum.auto()
    ALL = enum.auto()


ADAPTER_NAME_2_TYPE = {
    "aiocqhttp": PlatformAdapterType.AIOCQHTTP,
    "qq_official": PlatformAdapterType.QQOFFICIAL,
    "qq_official_webhook": PlatformAdapterType.QQOFFICIAL_WEBHOOK,
    "telegram": PlatformAdapterType.TELEGRAM,
    "wecom": PlatformAdapterType.WECOM,
    "wecom_ai_bot": PlatformAdapterType.WECOM_AI_BOT,
    "lark": PlatformAdapterType.LARK,
    "dingtalk": PlatformAdapterType.DINGTALK,
    "discord": PlatformAdapterType.DISCORD,
    "slack": PlatformAdapterType.SLACK,
    "kook": PlatformAdapterType.KOOK,
    "vocechat": PlatformAdapterType.VOCECHAT,
    "weixin_official_account": PlatformAdapterType.WEIXIN_OFFICIAL_ACCOUNT,
    "satori": PlatformAdapterType.SATORI,
    "misskey": PlatformAdapterType.MISSKEY,
    "line": PlatformAdapterType.LINE,
    "matrix": PlatformAdapterType.MATRIX,
    "weixin_oc": PlatformAdapterType.WEIXIN_OC,
    "mattermost": PlatformAdapterType.MATTERMOST,
    "webchat": PlatformAdapterType.WEBCHAT,
}


class PlatformAdapterTypeFilter(HandlerFilter):
    """按平台适配器类型过滤。"""

    def __init__(self, platform_adapter_type_or_str: PlatformAdapterType | str) -> None:
        if isinstance(platform_adapter_type_or_str, str):
            self.platform_type = ADAPTER_NAME_2_TYPE.get(platform_adapter_type_or_str)
        else:
            self.platform_type = platform_adapter_type_or_str

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        del cfg
        if (
            self.platform_type is not None
            and self.platform_type & PlatformAdapterType.ALL
        ):
            return True
        adapter_name = event.get_platform_name()
        if adapter_name in ADAPTER_NAME_2_TYPE and self.platform_type is not None:
            return bool(ADAPTER_NAME_2_TYPE[adapter_name] & self.platform_type)
        return False


__all__ = [
    "ADAPTER_NAME_2_TYPE",
    "PlatformAdapterType",
    "PlatformAdapterTypeFilter",
]
