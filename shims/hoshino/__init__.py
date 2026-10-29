"""N.E.K.O. bot_bridge 的 HoshinoBot / Arcadia（``hoshino``）兼容 shim。

HoshinoBot（Ice9Coffee/HoshinoBot）是 NoneBot **v1** 时代的 QQ 群机器人框架，插件写法是：

    from hoshino import Service, priv, util
    from hoshino.typing import CQEvent, Message, MessageSegment

    sv = Service('pcr-rank', help_='查询排名', manage_priv=priv.ADMIN)

    @sv.on_command('rank', aliases=('排名',))
    async def rank(bot, ev):
        await bot.send(ev, 'hello', at_sender=True)

    @sv.on_prefix('帮助')
    async def help_(bot, ev: CQEvent): ...

这个包提供同一批名字的**忠实子集**（只依赖标准库），让这类插件在 N.E.K.O. 里 import
得动、跑得起来。它由 ``bridge/backends/hoshino.py`` 通过 ``sys.path`` + ``sys.modules``
安装，插件不会被真正安装到解释器里。

## 覆盖范围

* ``Service``：``on_command`` / ``on_message`` / ``on_prefix`` / ``on_suffix`` /
  ``on_keyword`` / ``on_rex`` / ``on_fullmatch`` / ``on_notice`` / ``on_request`` /
  ``scheduled_job`` / ``broadcast`` / ``set_enable`` / ``set_disable`` / ``check_enabled`` /
  ``logger`` / ``bot`` / ``get_loaded_services()`` / ``get_bundles()``；``sucmd``
* ``hoshino.typing``：``CQEvent``、``Message``、``MessageSegment``、``CQHttpError``、
  ``Bot`` / ``HoshinoBot``、``CommandSession`` / ``NoticeSession`` / ``RequestSession``、
  ``T_State``、``from typing import *`` 的那些名字
* ``hoshino.util``：``load_config`` / ``save_config`` / ``pic2b64`` / ``fig2b64`` /
  ``image_to_base64`` / ``normalize_str`` / ``render_list`` / ``FreqLimiter`` /
  ``DailyNumberLimiter`` / ``gfw`` / ``filt_message`` / ``get_bot`` / ``get_self_ids``…
* ``hoshino.trigger``：``prefix`` / ``suffix`` / ``keyword`` / ``rex`` 触发器与 ``chain``
* ``hoshino.config``：只读配置快照；``hoshino.priv``：权限等级与黑名单；
  ``hoshino.log`` / ``hoshino.logger``：日志转发到 ``HostServices.log``；
  ``hoshino.R``：资源句柄
* 分发控制流异常（``CanceledException`` / ``SwitchException`` / …）与
  ``@hoshino.message_preprocessor``

## 已知偏差（相对上游）

1. **没有 nonebot v1**：shim 不提供 ``nonebot`` 包（那个名字归 NoneBot2 兼容层）。插件里
   ``import nonebot`` / ``from nonebot import ...`` 一律失败 —— 兼容层会在加载时给出结构化
   的 ``PluginLoadError`` 说明这一点。请改用 ``from hoshino.typing import ...``。
2. **没有网络**：事件来自 N.E.K.O. 对话；除了发消息以外的 OneBot 动作（群成员查询、撤回、
   禁言、审批、群列表…）抛 :class:`UnsupportedCapabilityError`，并登记进
   ``LoadedPlugin.native["unsupported_calls"]``。
3. **没有后台调度器**：``scheduled_job`` / ``sv.scheduler`` 只登记不执行（登记时写 warning，
   清单在 ``native["jobs"]``）；``scheduler.start()`` 等显式报能力缺失。
4. **没有群列表**：``Service.get_enable_groups`` 显式报能力缺失；``broadcast`` 只投递到当前
   N.E.K.O. 对话，并在 metadata 里标注 ``hoshino_broadcast``。
5. **没有繁体/简体互通**：``normalize_str`` 只做 NFKC + 小写（上游还会用 zhconv 转简体），
   因此按繁体写的关键词在与简体消息匹配时不命中。
6. **没有多轮会话**：``CommandSession.get`` 只在"这条消息里就有参数"时可用；``pause`` /
   ``switch`` 显式报能力缺失。
7. **私聊默认放行**：上游 ``priv.check_priv`` 对非群聊直接返回 False（Hoshino 是群聊机器人），
   照抄会让 N.E.K.O. 里的命令全部失效；桥接默认按 ``priv.PRIVATE`` 处理私聊发送者
   （``[bridge].hoshino_allow_private = false`` 可恢复上游行为）。
8. **B 站式细节**：子命令（``cmd.sub``）、argparse 参数解析、``on_natural_language``、
   ``R.url``（需要静态资源服务）、``filt_message`` 的默认词库没有实现/没有提供。

同一个 ``hoshino`` / ``aiocqhttp`` 模块名只能有一个实现：安装前会检查
``sys.modules`` 里是不是本 shim（模块上的 ``__neko_bridge_shim__`` 标记），
不是就抛 ``BackendUnavailableError``，绝不混用两套实现。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# ``aiocqhttp`` shim 与 ``hoshino`` 分放两个 sys.path 根（shims/ 与 shims/hoshino_support/），
# 兼容层会把两个都装好；这里兜底一次，方便直接 `import hoshino` 的场景（测试 / 调试脚本）。
try:  # pragma: no cover — 正常路径由兼容层铺好
    import aiocqhttp as _aiocqhttp_probe  # noqa: F401
except ImportError:  # pragma: no cover
    _support_root = Path(__file__).resolve().parents[1] / "hoshino_support"
    if _support_root.is_dir() and str(_support_root) not in sys.path:
        sys.path.insert(0, str(_support_root))

from aiocqhttp import Message as Message
from aiocqhttp import MessageSegment as MessageSegment
from aiocqhttp._runtime import RUNTIME as RUNTIME
from aiocqhttp._runtime import UnsupportedCapabilityError as UnsupportedCapabilityError
from aiocqhttp.bot import CQHttp as CQHttp
from aiocqhttp.control import CanceledException as CanceledException
from aiocqhttp.control import FinishedException as FinishedException
from aiocqhttp.control import PauseException as PauseException
from aiocqhttp.control import SwitchException as SwitchException
from aiocqhttp.event import Event as CQEvent
from aiocqhttp.event import build_event as build_event

from hoshino import _runtime
from hoshino import config as config
from hoshino import log as log
from hoshino import priv as priv
from hoshino import R as R
from hoshino import trigger as trigger
from hoshino import typing as typing
from hoshino import util as util
from hoshino.bot import HoshinoBot as HoshinoBot
from hoshino.log import logger as logger
from hoshino.log import new_logger as new_logger
from hoshino.service import Scheduler as Scheduler
from hoshino.service import Service as Service
from hoshino.service import ServiceFunc as ServiceFunc
from hoshino.service import sucmd as sucmd
from hoshino.session import CommandSession as CommandSession
from hoshino.session import NoticeSession as NoticeSession
from hoshino.session import RequestSession as RequestSession
from hoshino.session import T_State as T_State

__neko_bridge_shim__ = True
"""标记：这是 N.E.K.O. bot_bridge 的 Hoshino 兼容 shim，不是上游 hoshino 包。"""

_NEKO_BRIDGE_SHIM = True
"""与其它兼容层（astrbot）一致的旧标记名。"""

__version__ = "0.0.0+neko-bridge"
"""上游版本号占位（shim 不打上游版本号，避免插件误判版本）。"""

_NEKO_SHIM_UPSTREAM = "https://github.com/Ice9Coffee/HoshinoBot"


def get_bot() -> HoshinoBot:
    """当前插件绑定的机器人对象（上游同名）。"""
    bot = _bootstrap_bot()
    if bot is None:
        raise ValueError("HoshinoBot has not been initialized")
    return bot


def get_bots() -> dict[str, HoshinoBot]:
    """所有在线账号（shim 里只有一个逻辑账号）。"""
    bot = get_bot()
    return {str(item): bot for item in bot.get_self_ids()}


def get_self_ids() -> list[str]:
    """在线账号 id 列表（上游同名）。"""
    return list(get_bot().get_self_ids())


def init() -> HoshinoBot:
    """上游会 ``nonebot.init()`` 并加载 ``hoshino/modules/*``。

    shim 里"初始化"与"加载模块"都由兼容层在做（插件是被 bot_bridge 逐个导入的），
    因此这里只返回已经绑定好的机器人对象，并写一条 debug 日志说明差异。
    """
    bot = _bootstrap_bot()
    if bot is None:
        raise ValueError("HoshinoBot has not been initialized; bot_bridge loads plugins for you")
    log.logger.debug("hoshino shim: init() 由 bot_bridge 代劳（模块已由桥接层加载）")
    return bot


def message_preprocessor(func: Any) -> Any:
    """``@message_preprocessor``：注册一个消息预处理函数（``func(bot, event, state)``）。

    上游把它挂在 nonebot v1 的全局消息流水线上；shim 记到当前插件的记录里，由分发器在
    触发器链之前调用（语义一致，作用域按插件隔离）。
    """
    _runtime.record_preprocessor(_runtime.current_plugin_id(), func)
    return func


def _bootstrap_bot() -> Any:
    """按"当前插件 → 任意插件"的顺序找已绑定的机器人。"""
    bot = RUNTIME.bot_or_none()
    if bot is None:
        bot = RUNTIME.any_bot()
    return bot


__all__ = [
    "CQEvent",
    "CQHttp",
    "CanceledException",
    "CommandSession",
    "FinishedException",
    "HoshinoBot",
    "Message",
    "MessageSegment",
    "NoticeSession",
    "PauseException",
    "R",
    "RUNTIME",
    "RequestSession",
    "Scheduler",
    "Service",
    "ServiceFunc",
    "SwitchException",
    "T_State",
    "UnsupportedCapabilityError",
    "build_event",
    "config",
    "get_bot",
    "get_bots",
    "get_self_ids",
    "init",
    "log",
    "logger",
    "message_preprocessor",
    "new_logger",
    "priv",
    "sucmd",
    "trigger",
    "typing",
    "util",
]
