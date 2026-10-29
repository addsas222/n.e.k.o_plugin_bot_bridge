"""OneBot v11 Bot（上游 ``onebot.v11.bot`` 的极小子集）。

N.E.K.O. 桥接层里没有真正的 OneBot 协议端，所以：

* **发消息**（``send`` / ``send_msg`` / ``send_group_msg`` / ``send_private_msg``）→
  文本被写进当前事件分发的回复列表（``CommandResult.replies``）；
  若当前没有活跃分发（例如插件在后台任务里发言），则转交 ``HostServices.send_chat``。
* **其它 API**（``get_msg`` / ``get_group_member_info`` / ``set_group_ban`` …）→
  抛 :class:`ApiNotAvailable`，绝不返回伪造数据。

返回值形状与上游一致：发消息类 API 返回 ``{"message_id": <int>}``。
"""

from __future__ import annotations

from typing import Any

from nonebot.adapters import Bot as BaseBot
from nonebot.log import logger

from nonebot import _runtime

from .exception import ApiNotAvailable
from .message import Message, MessageSegment

__all__ = ["Bot"]

_SEND_APIS = frozenset({"send", "send_msg", "send_group_msg", "send_private_msg"})


def _extract_text(message: Any) -> str:
    """取出要交给 N.E.K.O. 的文本（纯文本优先，富文本回退到 CQ 码字符串）。"""
    if isinstance(message, Message):
        return message.extract_plain_text() or str(message)
    if isinstance(message, MessageSegment):
        return message.data.get("text", "") or str(message)
    return str(message)


class Bot(BaseBot):
    """OneBot v11 Bot（桥接层合成，不连协议端）。"""

    neko_plugin_id: str = ""
    """shim 扩展：这个 Bot 属于哪个外来插件（由桥接层设置，用于回复来源标注）。"""

    def __init__(self, adapter: Any, self_id: str) -> None:
        super().__init__(adapter, self_id)
        self._message_seq = 0

    async def _call_api(self, api: str, **data: Any) -> Any:
        if api not in _SEND_APIS:
            raise ApiNotAvailable(
                f"onebot v11 api '{api}' is not mapped to N.E.K.O. "
                "(the bridge synthesizes this bot; only message sending is available)"
            )
        message = data.get("message", "")
        text = _extract_text(message)
        self._message_seq += 1
        delivered = _runtime.emit_reply(text, source=self.neko_plugin_id)
        if not delivered:
            logger.debug(f"onebot v11 send fell back to the host chat channel: {text!r}")
        return {"message_id": self._message_seq}

    async def send_handler(
        self,
        event: Any,
        message: Any,
        **kwargs: Any,
    ) -> Any:
        """默认发送实现（上游由 Adapter 覆盖 ``send_handler``）。"""
        if kwargs.get("at_sender") and getattr(event, "message_type", "") == "group":
            user_id = getattr(event, "user_id", None)
            if user_id is not None:
                message = MessageSegment.at(user_id) + " " + message
        if kwargs.get("reply_message"):
            message_id = getattr(event, "message_id", None)
            if message_id:
                message = MessageSegment.reply(int(message_id)) + message
        return await self.call_api("send_msg", message=message, **kwargs)

    async def send(self, event: Any, message: Any, **kwargs: Any) -> Any:
        """根据 ``event`` 向触发事件的主体回复消息。"""
        return await self.__class__.send_handler(self, event, message, **kwargs)
