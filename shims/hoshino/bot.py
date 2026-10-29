"""``HoshinoBot``：上游 ``hoshino/__init__.py`` 里那个 ``nonebot.NoneBot`` 子类。

上游把 ``finish`` / ``silence`` / ``get_self_ids`` 挂在 bot 实例上，插件因此可以写
``await bot.finish(ev, msg)``。shim 的 :class:`~aiocqhttp.bot.CQHttp` 已经提供 ``finish``
与 ``get_self_ids``，这里只补 Hoshino 自己的那几样，并保留上游"别自己 new 一个"的守卫。
"""

from __future__ import annotations

from typing import Any

from aiocqhttp._runtime import UnsupportedCapabilityError
from aiocqhttp.bot import CQHttp

__all__ = ["HoshinoBot"]


class HoshinoBot(CQHttp):
    """Hoshino 的机器人对象（由 bot_bridge 创建，插件不要自己实例化）。"""

    def __init__(
        self,
        config_object: Any = None,
        *,
        plugin_id: str = "",
        api_clients: dict[str, Any] | None = None,
        self_id: Any = "neko",
        _internal: bool = False,
    ) -> None:
        if not _internal:
            # 上游同一条守卫（插件里出现 ``HoshinoBot()`` 是写错了）。
            raise RuntimeError(
                "You should NOT instantiate a HoshinoBot! "
                "Use `hoshino.get_bot()` instead — the bridge creates it for you.",
            )
        super().__init__(plugin_id, self_id=self_id, api_clients=api_clients)
        self.config_object = config_object

    @staticmethod
    def get_self_ids() -> list[str]:
        """在线账号 id 列表（上游同名静态方法）。"""
        from hoshino.util import get_bot

        try:
            bot = get_bot()
        except ValueError:
            return []
        return list(bot.get_self_ids())

    async def silence(self, ev: Any, ban_time: float, skip_su: bool = True) -> None:
        """禁言（上游 ``HoshinoBot.silence``）—— N.E.K.O. 没有禁言接口。"""
        raise UnsupportedCapabilityError(
            "hoshino.HoshinoBot.silence",
            f"N.E.K.O. 没有禁言接口（请求禁言 {ban_time}s 被拒绝）",
        )
