"""会话对象：``CommandSession`` / ``NoticeSession`` / ``RequestSession``。

上游（nonebot v1）的命令处理器收到的是 ``CommandSession``，通知/请求处理器收到的是
``NoticeSession`` / ``RequestSession``。Hoshino 的 ``Service.on_command`` 包了一层，插件里
两种写法都能见到：

    @sv.on_command('rank')                      # 上游 v1 风格：handler(session)
    async def rank(session): ...

    @sv.on_command('rank')                      # 新风格：handler(bot, ev)
    async def rank(bot, ev): ...

shim 按 handler 的参数个数决定传哪一种，因此这里要给出**自洽**的 session：它既像事件
（``session.ctx['user_id']`` / ``session.event.group_id``），又能发消息（``send`` / ``finish``）。

**不支持的部分一律显式报错**：多轮会话（``pause`` / ``switch``）与"缺参数时的提示-等待"
在 N.E.K.O. 里没有承载物，抛 :class:`UnsupportedCapabilityError`；``get()`` 只在"这条消息
里就有参数"时可用（单轮），这覆盖了绝大多数 Hoshino 命令。
"""

from __future__ import annotations

from typing import Any

from aiocqhttp._runtime import UnsupportedCapabilityError

__all__ = ["CommandSession", "NoticeSession", "RequestSession", "Session", "T_State"]


#: nonebot v1 的 ``T_State``（就是一个字典）。
T_State = dict[str, Any]


class Session:
    """会话基类：包住一条事件 + 机器人对象。"""

    def __init__(self, bot: Any, event: Any, *, args_text: str = "") -> None:
        self.bot = bot
        self.event = event
        self.self_id = event.get("self_id")
        self.state: dict[str, Any] = {}
        self._args_text = str(args_text)
        self._argv: list[str] = self._args_text.split()
        self._consumed = 0

    # ---- 事件视角 -------------------------------------------------------- #

    @property
    def ctx(self) -> Any:
        """原始事件（上游 ``session.ctx`` 是个 dict；这里就是可下标访问的事件对象）。"""
        return self.event

    def __getitem__(self, key: str) -> Any:
        return self.event[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.event.get(key, default)

    @property
    def current_arg_text(self) -> str:
        """还没被 ``get()`` 消耗掉的参数文本。"""
        if self._consumed == 0:
            return self._args_text
        return " ".join(self._argv[self._consumed :])

    @property
    def current_arg(self) -> str:
        """同上（上游两者对"带空格的参数"处理不同，shim 里一致）。"""
        return self.current_arg_text

    @property
    def argv(self) -> list[str]:
        """全部参数（按空白切分；上游用 argparse，这里只做切分）。"""
        return list(self._argv)

    @property
    def arg(self) -> str:
        """下一个还没消耗的参数。"""
        if self._consumed < len(self._argv):
            return self._argv[self._consumed]
        return ""

    @property
    def is_first_run(self) -> bool:
        """shim 里命令只会跑一次。"""
        return True

    # ---- 发消息 ---------------------------------------------------------- #

    async def send(self, message: Any, **kwargs: Any) -> dict[str, Any]:
        """发一条消息（收进当前分发的回复）。"""
        return await self.bot.send(self.event, message, **kwargs)

    async def finish(self, message: Any = None, **kwargs: Any) -> None:
        """发一条消息并结束本次分发。"""
        await self.bot.finish(self.event, message, **kwargs)

    # ---- 参数 ------------------------------------------------------------ #

    def get_arg(self, key: str, prompt: Any = None, **kwargs: Any) -> Any:
        """取一个参数（单轮）。

        这条消息里还有剩余参数就返回它；否则抛能力缺失 —— 上游会在这里发提示并**等用户
        下一条消息**，N.E.K.O. 的分发模型里没有"等待"，绝不假装能做到。
        """
        if self._consumed < len(self._argv):
            value = self._argv[self._consumed]
            self._consumed += 1
            return value
        raise UnsupportedCapabilityError(
            "nonebot.CommandSession.get",
            f"命令参数 {key!r} 缺失，而多轮会话（发提示后等待用户下一条消息）在 N.E.K.O. 里没有承载物",
        )

    # ---- 明确不支持的会话动作 -------------------------------------------- #

    def pause(self, *args: Any, **kwargs: Any) -> None:
        raise UnsupportedCapabilityError(
            "nonebot.CommandSession.pause",
            "N.E.K.O. 的分发是一次性的，不支持「暂停等用户下一条消息」",
        )

    def switch(self, *args: Any, **kwargs: Any) -> None:
        raise UnsupportedCapabilityError(
            "nonebot.CommandSession.switch",
            "N.E.K.O. 的分发里命令已经匹配完了，不支持把消息交给另一条命令",
        )


class CommandSession(Session):
    """命令会话（上游 nonebot v1 同名）。"""


class NoticeSession(Session):
    """通知会话（上游 nonebot v1 同名）。"""


class RequestSession(Session):
    """请求会话（上游 nonebot v1 同名）。

    ``approve()`` / ``reject()`` 需要向 QQ 回执，N.E.K.O. 侧没有这个通道 —— 显式不支持。
    """

    async def approve(self, *args: Any, **kwargs: Any) -> None:
        raise UnsupportedCapabilityError(
            "nonebot.RequestSession.approve",
            "N.E.K.O. 没有加好友 / 入群审批通道",
        )

    async def reject(self, *args: Any, **kwargs: Any) -> None:
        raise UnsupportedCapabilityError(
            "nonebot.RequestSession.reject",
            "N.E.K.O. 没有加好友 / 入群审批通道",
        )
