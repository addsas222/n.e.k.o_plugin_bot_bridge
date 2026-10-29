"""OneBot v12 Bot（上游 ``onebot.v12.bot`` 的极小子集）。

与 v11 Bot 同样的定位：桥接层没有协议端，所以

* **发消息**（``send_message`` / ``send_private_message`` / ``send_group_message`` /
  ``send_channel_message``）→ 文本进当前分发的回复列表；没有活跃分发时转交宿主
  （``HostServices.send_chat``，走 ``_runtime.emit_reply`` 的回落）。
* **其它 API**（``get_user_info`` / ``get_group_info`` / ``get_message`` 等）→
  抛 :class:`ApiNotAvailable`，不返回伪造数据。
* 返回值按 v12 形状：``{"message_id": <str>}``。
"""

from __future__ import annotations

from typing import Any

from nonebot.adapters import Bot as BaseBot
from nonebot.log import logger

from nonebot import _runtime

from .exception import ApiNotAvailable
from .message import Message, MessageSegment

__all__ = ["Bot"]

_SEND_APIS = frozenset(
    {
        "send_message",
        "send_private_message",
        "send_group_message",
        "send_channel_message",
    }
)


def _extract_text(message: Any) -> str:
    """取要交给 N.E.K.O. 的文本（纯文本优先，富文本回退到段渲染）。"""
    if isinstance(message, Message):
        return message.extract_plain_text() or str(message)
    if isinstance(message, MessageSegment):
        return str(message)
    if isinstance(message, list):
        return str(Message(message))
    return str(message)


class Bot(BaseBot):
    """OneBot v12 Bot（桥接层合成，不连协议端）。"""

    neko_plugin_id: str = ""
    """shim 扩展：这个 Bot 属于哪个外来插件（用于回复来源标注）。"""

    def __init__(self, adapter: Any, self_id: str) -> None:
        super().__init__(adapter, self_id)
        self._message_seq = 0

    async def _call_api(self, api: str, **data: Any) -> Any:
        if api not in _SEND_APIS:
            raise ApiNotAvailable(
                f"onebot v12 api '{api}' is not mapped to N.E.K.O. "
                "(the bridge synthesizes this bot; only message sending is available)"
            )
        text = _extract_text(data.get("message", ""))
        self._message_seq += 1
        delivered = _runtime.emit_reply(text, source=self.neko_plugin_id)
        if not delivered:
            logger.debug(f"onebot v12 send fell back to the host chat channel: {text!r}")
        return {"message_id": f"neko-{self._message_seq}"}

    async def send(self, event: Any, message: Any, **kwargs: Any) -> Any:
        """根据事件的 ``detail_type`` 选择发送 API（与上游一致）。"""
        detail = str(getattr(event, "detail_type", "") or "private")
        params: dict[str, Any] = {"detail_type": detail, "message": message}
        if detail == "group":
            params["group_id"] = getattr(event, "group_id", "")
        elif detail == "channel":
            params["guild_id"] = getattr(event, "guild_id", "")
            params["channel_id"] = getattr(event, "channel_id", "")
        else:
            params["user_id"] = getattr(event, "user_id", "")
        params.update({k: v for k, v in kwargs.items() if k in {"message_id", "timestamp"}})
        return await self.call_api("send_message", **params)
