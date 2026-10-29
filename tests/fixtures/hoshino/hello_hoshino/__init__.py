"""``hello_hoshino``：一个尽量贴近真实形态的 Hoshino 插件 fixture。

覆盖桥接层要映射的东西：

* ``Service``（``help_`` / ``manage_priv`` / ``enable_on_default`` / ``visible``）
* ``on_command``（含别名，``(bot, ev)`` 风格）+ 另一条 session 风格（``(session)``）的命令
* ``on_prefix`` / ``on_suffix`` / ``on_keyword`` / ``on_rex`` / ``on_fullmatch`` 触发器
* ``on_message`` 监听、``on_startup`` / ``on_shutdown`` 钩子
* ``bot.send(at_sender=True)`` / ``bot.finish`` / ``session.send`` / ``session.finish``
* ``hoshino.util`` 的 ``load_config`` / ``save_config`` / ``FreqLimiter``
* ``hoshino.logger`` 日志、``sv.broadcast``、``sv.scheduled_job``（只登记不执行）
* 同目录兄弟模块 ``extra.py``（上游按文件加载目录插件，兼容层要都导入）

这个插件只用于测试，除 shim 之外不依赖任何第三方包。
"""

from __future__ import annotations

import re

from hoshino import Service, priv, util
from hoshino.typing import CQEvent, Message, MessageSegment

sv = Service(
    "hello_hoshino",
    help_="兼容层测试服务：问候 / 计数 / 触发器 / 配置",
    manage_priv=priv.ADMIN,
    enable_on_default=True,
    visible=True,
)

#: 配置落在 ``<data_dir>/config/hello_hoshino.json``（不存在时 load_config 返回 {}）。
config = util.load_config(__file__)
if not config:
    config = {"greeting": "你好", "counter": 0}
    util.save_config(__file__, config)

#: CD 限制器（上游 util.FreqLimiter 的典型用法）。
limiter = util.FreqLimiter(0.0)
state = {"startups": 0, "shutdowns": 0, "messages": 0}


@sv.on_command("hello", aliases=("打招呼",), only_to_me=False)
async def hello(bot, ev: CQEvent):
    """问好（``(bot, ev)`` 风格；``ev['args']`` 是 shim 给出的命令参数）"""
    name = str(ev.get("args") or "").strip() or ev.sender.get("nickname") or "陌生人"
    await bot.send(ev, f"{config['greeting']}，{name}！", at_sender=True)


@sv.on_command("bye")
async def bye(bot, ev: CQEvent):
    """用 ``bot.finish`` 结束分发（后面的 send 不该执行）"""
    await bot.finish(ev, "再见")
    await bot.send(ev, "这行不会执行")


@sv.on_command("counter", aliases=("计数",))
async def counter(session):
    """计数（session 风格，读/写插件配置）"""
    if session.current_arg_text.strip() == "reset":
        config["counter"] = 0
        util.save_config(__file__, config)
    config["counter"] = int(config.get("counter", 0)) + 1
    util.save_config(__file__, config)
    session.state["last"] = config["counter"]
    await session.send(f"计数：{config['counter']}")


@sv.on_command("secret")
async def secret(session):
    """只有超管能用（use_priv 之外再叠一层 priv 判定）"""
    await session.send("secret ok")


@sv.on_prefix("图", "查图")
async def picture(bot, ev: CQEvent):
    """前缀触发器：命中的前缀会被摘掉"""
    rest = ev.message.extract_plain_text().strip()
    await bot.send(ev, f"贴图:{rest}")


@sv.on_suffix("结束")
async def suffix_done(bot, ev: CQEvent):
    """后缀触发器"""
    await bot.send(ev, f"收尾:{ev.message.extract_plain_text().strip()}")


@sv.on_keyword("签到")
async def checkin(bot, ev: CQEvent):
    """关键词触发器"""
    if not limiter.check("checkin"):
        await bot.send(ev, "太频繁了")
        return
    limiter.start_cd("checkin", 60)
    await bot.send(ev, "签到成功")


@sv.on_fullmatch("帮助")
async def full_help(bot, ev: CQEvent):
    """全匹配触发器（多一个字都不触发）"""
    await bot.send(ev, "帮助：hello / counter / 图 / 签到")


@sv.on_rex(re.compile(r"^\.r\s*(\d+)d(\d+)"))
async def dice(bot, ev: CQEvent):
    """正则触发器（``ev['match']``）"""
    match = ev["match"]
    await bot.send(ev, f"掷 {match.group(1)} 个 {match.group(2)} 面骰")


@sv.on_rex(r"^\.echo\s+(.+)$")
async def echo(bot, ev: CQEvent):
    """正则触发器（字符串写法）+ 图片段（N.E.K.O. 只承载文本，图片会被记录但不发送）"""
    match = ev["match"]
    await bot.send(ev, Message([MessageSegment.text(f"复读:{match.group(1)}")]))
    await bot.send(ev, MessageSegment.image("file:///tmp/not-exists.png"))


@sv.on_message("group")
async def watcher(bot, ev: CQEvent):
    """群消息监听（兜底：任何群消息都会到这里）"""
    state["messages"] = int(state.get("messages", 0)) + 1
    sv.logger.info(f"看到群消息：{ev.raw_message}")


@sv.on_notice("group_increase")
async def on_group_increase(session):
    """通知处理器（``NoticeSession`` 风格）"""
    session.state["notice"] = session.event.notice_type
    await session.send(f"欢迎新成员 {session.event.user_id}")


@sv.on_request("group.add")
async def on_group_request(bot, ev):
    """请求处理器（``(bot, ev)`` 风格）；``session.approve()`` 在桥接里是显式不支持的"""
    await bot.send(ev, f"收到入群申请（来自 {ev.user_id}）")


@sv.on_command("diag")
async def diag(session):
    """诊断：暴露 Service 状态（测试用）"""
    loaded = Service.get_loaded_services()
    session.state["services"] = sorted(loaded)
    enabled = sv.check_enabled(session.event.group_id)
    await session.send(f"services={','.join(sorted(loaded))} enabled={enabled}")


@sv.on_command("announce")
async def announce_cmd(session):
    """广播：分发期间进当前回复，分发之外回落 N.E.K.O. 对话"""
    result = await sv.broadcast(f"公告:{session.current_arg_text}", "测试")
    await session.send(f"scope={result['scope']}")


@sv.on_command("unsupported")
async def unsupported_cmd(bot, ev):
    """调用 N.E.K.O. 侧没有等价实现的能力（应当结构化失败，不假装成功）"""
    await util.silence(ev, 60)
    await bot.send(ev, "这行永远不会执行")


@sv.scheduled_job("cron", hour=5)
async def nightly():
    """定时任务（兼容层只登记不执行）"""
    sv.logger.info("nightly tick")


@sv.on_startup
async def startup_hook():
    """插件启动钩子"""
    state["startups"] += 1
    sv.logger.info("hello_hoshino 启动")


@sv.on_shutdown
async def shutdown_hook():
    """插件停止钩子"""
    state["shutdowns"] += 1
    sv.logger.info("hello_hoshino 停止")


async def announce(text: str, TAG: str = "") -> dict:
    """供测试调用的广播入口（``sv.broadcast``）。"""
    return await sv.broadcast(text, TAG)
