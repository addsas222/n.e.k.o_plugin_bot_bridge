"""``hello_nonebot``：一个尽量贴近真实形态的 NoneBot2 插件 fixture。

它刻意覆盖了桥接层需要映射的全部东西：

* ``__plugin_meta__``（``PluginMetadata``，含 ``config`` 模型与 ``extra["apis"]``）
* ``on_command``（含别名）/ ``on_regex`` / ``on_message(rule=to_me())`` / ``on_notice``
* ``Matcher.got`` + ``ArgStr`` 的多轮会话、``Matcher.finish`` / ``Matcher.send``
* 依赖注入 ``CommandArg`` / ``RegexGroup`` / ``EventMessage`` 与 ``T_State``
* ``SUPERUSER`` 权限
* ``get_driver().on_startup`` / ``on_shutdown`` 生命周期钩子
* ``Bot.send`` / ``Bot.call_api`` 以及 ``get_bot()``
* 包内相对导入（``from .side import GREETING_SUFFIX``）

这个插件只用于测试，不依赖任何第三方包（除了 ``nonebot`` 本身，也就是被桥接层替换掉的 shim）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import nonebot
from nonebot import get_driver, on_command, on_message, on_notice, on_regex
from nonebot.adapters.onebot.v11 import (
    Bot,
    Message,
    MessageEvent,
    MessageSegment,
    NoticeEvent,
)
from nonebot.log import logger
from nonebot.params import ArgStr, CommandArg, EventMessage, RegexGroup
from nonebot.permission import SUPERUSER
from nonebot.plugin import PluginMetadata, get_plugin_config
from nonebot.rule import to_me

from .side import GREETING_SUFFIX

__all__ = ["plugin_state", "announce", "resolve_dependency"]


@dataclass
class Config:
    """插件配置模型（``get_plugin_config`` 用）。"""

    greeting: str = "你好"
    pong: str = "pong"
    features: list[str] = field(default_factory=lambda: ["greet", "calc", "notice"])


__plugin_meta__ = PluginMetadata(
    name="Hello NoneBot",
    description="bot_bridge 的 NoneBot2 兼容层测试插件：命令 / 正则 / 会话 / 通知 / API。",
    usage="/hello [名字]、/calc 1 + 2、/ask、ping、poke 通知",
    type="application",
    homepage="https://example.invalid/hello_nonebot",
    config=Config,
    supported_adapters={"~onebot.v11"},
    extra={"apis": ["plugin_state", "announce", "resolve_dependency"]},
)

_CONFIG = get_plugin_config(Config)
"""配置（桥接层能读到用户配置时用它，否则是模型默认值）。"""

_STATE: dict[str, Any] = {
    "startups": 0,
    "shutdowns": 0,
    "greetings": 0,
    "calcs": 0,
    "notices": 0,
    "pongs": 0,
    "answers": [],
}

_driver = get_driver()


@_driver.on_startup
async def _on_startup() -> None:
    """插件启动钩子（等价于 on_load）。"""
    _STATE["startups"] += 1
    logger.info("hello_nonebot 已加载")


@_driver.on_shutdown
async def _on_shutdown() -> None:
    """插件停止钩子（等价于 on_unload）。"""
    _STATE["shutdowns"] += 1
    logger.info("hello_nonebot 已卸载")


hello = on_command("hello", aliases={"hi"}, priority=5)


@hello.handle()
async def hello_handler(bot: Bot, event: MessageEvent, arg: Message = CommandArg()) -> None:
    """向用户问好（命令 + 命令参数）。"""
    _STATE["greetings"] += 1
    name = arg.extract_plain_text().strip() or event.get_user_id()
    text = f"{_CONFIG.greeting}，{name}！{GREETING_SUFFIX}"
    if event.message_type == "group":
        await bot.send(event, MessageSegment.at(event.get_user_id()) + text)
    else:
        await bot.send(event, text)


calc = on_regex(r"^calc\s+(-?\d+)\s*([+\-*/])\s*(-?\d+)$", priority=6)


@calc.handle()
async def calc_handler(groups: tuple[Any, ...] = RegexGroup()) -> None:
    """做一次四则运算（正则 group 注入）。"""
    left, operator, right = int(groups[0]), str(groups[1]), int(groups[2])
    table = {
        "+": lambda a, b: a + b,
        "-": lambda a, b: a - b,
        "*": lambda a, b: a * b,
        "/": lambda a, b: a / b if b else float("nan"),
    }
    result = table[operator](left, right)
    _STATE["calcs"] += 1
    await calc.finish(f"{left} {operator} {right} = {result}")


async def _is_free_chat(event: MessageEvent) -> bool:
    """只在「不是在用命令或算式」时才把消息当成闲聊（避免抢其它响应器的事件）。"""
    text = event.get_plaintext().strip()
    return bool(text) and not text.startswith("/") and not text.startswith("calc")


ping = on_message(rule=to_me() & _is_free_chat, priority=7)
"""没有文本触发的响应器：桥接层把它暴露成 N.E.K.O. 的 LLM 工具（而不是文本命令）。"""


@ping.handle()
async def ping_handler(event: MessageEvent, message: Message = EventMessage()) -> None:
    """被 @ 或私聊时回一句话（无文本触发，只能由工具调用或直接对话命中）。"""
    _STATE["pongs"] += 1
    text = message.extract_plain_text().strip()
    suffix = f"（收到：{text}）" if text else ""
    await ping.finish(f"{_CONFIG.pong}{suffix}")


notice = on_notice(priority=8)


@notice.handle()
async def notice_handler(event: NoticeEvent) -> None:
    """记录通知事件（notice 类型事件响应器）。"""
    _STATE["notices"] += 1
    await notice.send(f"已收到通知：{event.get_event_name()}")


ask = on_command("ask", priority=9)


@ask.got("name", prompt="你的名字是？")
async def ask_handler(name: str = ArgStr("name")) -> None:
    """多轮会话：先问名字，再回答（``got`` + ``ArgStr``）。"""
    _STATE["answers"].append(name)
    await ask.finish(f"{name}，{GREETING_SUFFIX}！")


admin = on_command("admin", permission=SUPERUSER, priority=10)


@admin.handle()
async def admin_handler() -> None:
    """只有超级用户才能触发的命令。"""
    await admin.finish("管理员你好")


def plugin_state() -> dict[str, Any]:
    """返回插件运行时状态（由 ``__plugin_meta__.extra["apis"]`` 暴露给桥接层）。"""
    return {
        "state": dict(_STATE),
        "config": {
            "greeting": _CONFIG.greeting,
            "pong": _CONFIG.pong,
            "features": list(_CONFIG.features),
        },
    }


async def announce(text: str, group_id: str = "") -> dict[str, Any]:
    """在事件分发之外发言：走 ``get_bot().call_api``，会回落到宿主聊天通道。"""
    bot = nonebot.get_bot()
    await bot.call_api("send_group_msg", group_id=int(group_id or 0), message=text)
    return {"sent": True, "text": text, "bots": sorted(nonebot.get_bots())}


async def resolve_dependency(name: str) -> dict[str, Any]:
    """通过 ``nonebot.require`` 解析依赖插件（桥接层只能看到同一个桥里的插件）。"""
    module = nonebot.require(name)
    return {"module": module.__name__}
