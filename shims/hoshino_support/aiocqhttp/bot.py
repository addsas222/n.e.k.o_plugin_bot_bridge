"""``aiocqhttp.CQHttp`` 兼容 shim（OneBot v11 客户端）。

上游的 ``CQHttp`` 是一个「连着 websocket、能对 QQ 发号施令」的客户端；N.E.K.O. 没有
OneBot 连接，所以这里只保留**语义上的那一半**：

* 发消息（``send`` / ``send_msg`` / ``send_group_msg`` / ``send_private_msg``）落地到
  当前分发（收集进 ``CommandResult.replies``）或回落 ``HostServices.send_chat``；
* ``finish``（Hoshino 把 ``HoshinoBot.finish`` 挂在 bot 上，插件用
  ``await bot.finish(ev, msg)``）先收下这条回复，再抛 ``CanceledException`` 结束分发；
* 查询类动作（群成员 / 群列表 / 撤回 / 禁言 / 审批…）**一律**抛
  :class:`UnsupportedCapabilityError`，并在 ``native["unsupported_calls"]`` 里留痕。

未列出的动作走 :meth:`CQHttp.__getattr__` 得到一个占位调用对象：调用它同样抛
:class:`UnsupportedCapabilityError`（比"什么都没发生"诚实）。代价是 ``hasattr(bot, 'x')``
对它之外的名字会返回 True —— 这是文档化的偏差。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import _runtime
from .errors import UnsupportedCapabilityError
from .event import Event
from .message import Message, MessageSegment

__all__ = ["CQHttp"]

#: 明确「N.E.K.O. 侧没有等价实现」的 OneBot v11 动作。
_UNSUPPORTED_ACTIONS: dict[str, str] = {
    "call_action": "N.E.K.O. 不是 OneBot 客户端，没有可调用的动作总线",
    "get_login_info": "N.E.K.O. 没有 OneBot 登录账号",
    "get_friend_list": "N.E.K.O. 没有好友列表",
    "get_group_list": "N.E.K.O. 没有群列表",
    "get_group_info": "N.E.K.O. 没有群资料查询",
    "get_group_member_info": "N.E.K.O. 没有群成员查询（只有当前消息发送者的信息）",
    "get_group_member_list": "N.E.K.O. 没有群成员列表",
    "get_stranger_info": "N.E.K.O. 没有陌生用户资料查询",
    "get_group_honor_info": "N.E.K.O. 没有群荣誉查询",
    "get_msg": "N.E.K.O. 没有单条消息查询",
    "get_forward_msg": "N.E.K.O. 没有合并转发消息",
    "get_record": "N.E.K.O. 没有语音转换",
    "get_image": "N.E.K.O. 没有图片下载接口",
    "get_status": "N.E.K.O. 不是 OneBot 实现，没有运行状态接口",
    "get_version_info": "N.E.K.O. 不是 OneBot 实现，没有版本信息接口",
    "delete_msg": "N.E.K.O. 没有消息撤回接口",
    "set_group_ban": "N.E.K.O. 没有禁言接口",
    "set_group_whole_ban": "N.E.K.O. 没有全员禁言接口",
    "set_group_kick": "N.E.K.O. 没有踢人接口",
    "set_group_leave": "N.E.K.O. 没有退群接口",
    "set_group_card": "N.E.K.O. 没有改群名片接口",
    "set_group_admin": "N.E.K.O. 没有设置管理员的接口",
    "set_group_name": "N.E.K.O. 没有改群名接口",
    "set_friend_add_request": "N.E.K.O. 没有好友请求审批",
    "set_group_add_request": "N.E.K.O. 没有入群请求审批",
    "send_like": "N.E.K.O. 没有点赞接口",
    "set_restart": "N.E.K.O. 没有重启接口",
    "set_group_anonymous": "N.E.K.O. 没有匿名开关",
    "set_group_special_title": "N.E.K.O. 没有群头衔接口",
    "mark_msg_as_read": "N.E.K.O. 没有已读接口",
    "set_essence_msg": "N.E.K.O. 没有精华消息接口",
    "send_group_notice": "N.E.K.O. 没有群公告接口",
    "get_group_notice": "N.E.K.O. 没有群公告接口",
}
"""动作名 → 缺失原因（``__getattr__`` 与测试都用它）。"""


class _UnsupportedApi:
    """未知 OneBot 动作的占位调用对象。"""

    def __init__(self, name: str, owner: "CQHttp") -> None:
        self._name = name
        self._owner = owner

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._owner.unsupported(self._name)

    def __getattr__(self, name: str) -> "_UnsupportedApi":
        return _UnsupportedApi(f"{self._name}.{name}", self._owner)

    def __repr__(self) -> str:
        return f"<unsupported OneBot api {self._name!r}>"


class CQHttp:
    """OneBot v11 事件对应的「机器人」对象（Hoshino 里就是 ``HoshinoBot`` 的实例）。"""

    def __init__(
        self,
        plugin_id: str = "",
        *,
        self_id: Any = "neko",
        api_clients: Mapping[str, Any] | None = None,
        **options: Any,
    ) -> None:
        self.plugin_id = plugin_id
        self.self_id = self_id
        self.config: dict[str, Any] = dict(options)
        self._wsr_api_clients: dict[str, Any] = dict(api_clients or {str(self_id): self})
        # 上游 HoshinoBot.finish 会用 bot.loop；这里在事件循环里创建，取得到就存下来。
        self.loop: Any = None
        try:
            import asyncio

            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            self.loop = None

    # ------------------------------------------------------------------ 元信息

    def get_self_ids(self) -> list[str]:
        """在线账号列表（上游从 websocket 客户端里取；shim 只有一个逻辑账号）。"""
        return list(self._wsr_api_clients) or [str(self.self_id)]

    @property
    def is_online(self) -> bool:
        """shim 没有连接，但机器人始终"可用"（N.E.K.O. 就在进程里）。"""
        return True

    def unsupported(self, action: str, detail: str = "") -> Any:
        """登记并抛出「能力缺失」。"""
        reason = detail or _UNSUPPORTED_ACTIONS.get(action, "shim 未映射这个 OneBot 动作")
        _runtime.RUNTIME.record_unsupported(self.plugin_id, action)
        raise UnsupportedCapabilityError(f"OneBot.{action}", reason)

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return _UnsupportedApi(name, self)

    # ------------------------------------------------------------------ 发消息

    async def send(
        self,
        event: Event | Mapping[str, Any] | None,
        message: Any,
        *,
        at_sender: bool = False,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """把 ``message`` 发给 ``event`` 对应的会话。"""
        target = _as_event(event)
        body = _coerce_message(message)
        if at_sender and target is not None and target.message_type != "private" and target.user_id:
            # 上游会在文本前插一个带空格的 at 段；这里只插 at 段（N.E.K.O. 的回复只带文本，
            # 多出来的空格会变成回复开头的空白），at 本身记进结构化记录。
            body = Message([MessageSegment.at(target.user_id), *list(body)])
        metadata = {
            "hoshino_api": "send",
            "self_id": str(target.self_id) if target is not None else str(self.self_id),
            "group_id": str(target.group_id) if target is not None else "",
            "user_id": str(target.user_id) if target is not None else "",
            "at_sender": bool(at_sender),
        }
        captured = _runtime.RUNTIME.emit_message(body, str(self.plugin_id), metadata)
        return _ok_response(captured, group_id=metadata["group_id"], user_id=metadata["user_id"])

    async def send_msg(
        self,
        *,
        message: Any = None,
        message_type: str = "",
        user_id: Any = None,
        group_id: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """按 ``message_type`` 发送（``group`` / ``private``）。"""
        kind = message_type or ("group" if group_id else "private")
        if kind == "group":
            return await self.send_group_msg(group_id=group_id, message=message, **kwargs)
        return await self.send_private_msg(user_id=user_id, message=message, **kwargs)

    async def send_group_msg(
        self,
        *,
        group_id: Any = None,
        message: Any = None,
        self_id: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """发给某个群。

        N.E.K.O. 只有"当前对话"这一个投递目标，因此这段文本会进入当前对话
        （分发期间进 ``CommandResult.replies``），并在结构化记录里标注目标群 —— 不假装
        真的发到了那个群。
        """
        body = _coerce_message(message)
        metadata = {
            "hoshino_api": "send_group_msg",
            "group_id": str(group_id or ""),
            "self_id": str(self_id or self.self_id),
            "target_missing": True,
        }
        captured = _runtime.RUNTIME.emit_message(body, str(self.plugin_id), metadata)
        return _ok_response(captured, group_id=str(group_id or ""))

    async def send_private_msg(
        self,
        *,
        user_id: Any = None,
        message: Any = None,
        self_id: Any = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """发给某个用户（同样只投递到当前对话）。"""
        body = _coerce_message(message)
        metadata = {
            "hoshino_api": "send_private_msg",
            "user_id": str(user_id or ""),
            "self_id": str(self_id or self.self_id),
            "target_missing": True,
        }
        captured = _runtime.RUNTIME.emit_message(body, str(self.plugin_id), metadata)
        return _ok_response(captured, user_id=str(user_id or ""))

    async def finish(self, event: Event | Mapping[str, Any] | None = None, message: Any = None, **kwargs: Any) -> None:
        """发一条消息然后结束本次分发（Hoshino 的 ``await bot.finish(ev, msg)``）。

        上游通过 ``asyncio.run_coroutine_threadsafe`` 异步投递再抛
        ``CanceledException``；shim 在同一条协程里先收下这段文本再抛，语义一致但没有
        跨线程调度。
        """
        from .control import CanceledException

        if message:
            await self.send(event, message, **kwargs)
        raise CanceledException("HoshinoBot.finish")


def _coerce_message(message: Any) -> Message:
    if isinstance(message, Message):
        return message
    return Message(message)


def _as_event(event: Any) -> Event | None:
    if event is None:
        return None
    if isinstance(event, Event):
        return event
    if isinstance(event, Mapping):
        try:
            from .event import build_event

            return build_event(event)
        except ValueError:
            return None
    return None


def _ok_response(captured: bool, **data: Any) -> dict[str, Any]:
    """OneBot 风格的返回（``captured`` 说明这段文本去了哪里）。"""
    payload = {"status": "ok", "retcode": 0, "captured": bool(captured)}
    payload["data"] = {key: value for key, value in data.items() if value}
    return payload
