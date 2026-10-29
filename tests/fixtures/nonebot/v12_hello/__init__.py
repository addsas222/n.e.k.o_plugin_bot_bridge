"""``v12_hello``：只使用 OneBot **v12** 适配器的 NoneBot2 插件 fixture。

它的作用是钉住「按插件分发适配器」这条契约：

* 源码里只 import ``nonebot.adapters.onebot.v12``（没有任何 v11 引用）
  → 桥接层必须在加载期就把这个插件判定为 v12；
* ``__plugin_meta__.supported_adapters = {"~onebot.v12"}`` 与源码判断一致；
* handler 用 ``isinstance`` / ``detail_type`` / ``get_session_id()`` 这些**只有 v12 才有**
  的语义回话 —— 如果桥给的是 v11 事件，这条命令会直接不命中（而不是悄悄回错内容）。

它只用 shim 自身，不依赖任何第三方包。
"""

from __future__ import annotations

from typing import Any

from nonebot import on_command
from nonebot.adapters.onebot.v12 import (
    Bot,
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.plugin import PluginMetadata

__all__ = ["describe"]

__plugin_meta__ = PluginMetadata(
    name="V12 Hello",
    description="bot_bridge 的 OneBot v12 适配器测试插件（命令 / 群事件 / 消息段）。",
    usage="/v12ping",
    type="application",
    homepage="https://example.invalid/v12_hello",
    supported_adapters={"~onebot.v12"},
    extra={"apis": ["describe"]},
)

_SEEN: dict[str, Any] = {"calls": 0, "kinds": []}

v12ping = on_command("v12ping", priority=5)


@v12ping.handle()
async def v12ping_handler(bot: Bot, event: MessageEvent) -> None:
    """回一句话，把 v12 专属语义（类名 / detail_type / session_id）带出去。"""
    _SEEN["calls"] += 1
    kind = type(event).__name__
    _SEEN["kinds"].append(kind)
    detail = str(getattr(event, "detail_type", "") or "")
    text = f"v12:{kind}:{detail}:{event.get_session_id()}"
    # v12 的 mention 段（v11 里叫 at）—— 段名不同，用错就会露馅
    await bot.send(event, MessageSegment.mention(event.get_user_id()) + text)


group_only = on_command("v12group", priority=6)


@group_only.handle()
async def v12group_handler(event: MessageEvent) -> None:
    """只在真的收到 v12 群消息事件时才回（``isinstance`` 是硬判据）。"""
    if not isinstance(event, GroupMessageEvent):
        await group_only.finish("not-a-group-event")
        return
    await group_only.finish(f"group:{event.group_id}:{type(event.message).__name__}")


async def describe() -> dict[str, Any]:
    """把插件看到的适配器信息回报给桥接层（由 ``extra["apis"]`` 暴露）。"""
    return {"seen": dict(_SEEN), "message_class": Message.__name__}
