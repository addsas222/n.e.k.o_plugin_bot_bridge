"""真机样例：一个标准写法的 HoshinoBot 插件（``hoshino/modules/hello_hoshino/`` 那种形态）。

安装方式（真机验收）：

    把整个 ``hello_hoshino/`` 目录复制到 N.E.K.O. 的插件目录：
    ``<N.E.K.O. 数据目录>/plugins/hoshino/hello_hoshino/``
    （或在 ``plugin.toml`` 的 ``[bridge] plugins_root`` 指定的目录下建 ``hoshino/``）。
    重启/重载 bot_bridge 之后，``bridge_status`` 里应当能看到：
    framework=hoshino、命令 ``hi``（别名 ``你好``）、工具 ``hello_hoshino_hi``、事件
    ``notice.group_increase``，以及 ``native["jobs"]`` 里登记但不执行的 ``daily_cleanup``。

覆盖的 API：``Service``（``help_`` / ``manage_priv``）、``on_command``（别名）、
``on_prefix``、``on_keyword``、``on_notice``、``bot.send(at_sender=True)``、
``bot.finish``、``hoshino.util.load_config`` / ``save_config`` / ``FreqLimiter``、
``hoshino.logger``、``sv.on_startup`` / ``on_shutdown``、``sv.scheduled_job``。
"""

from __future__ import annotations

from hoshino import Service, priv, util
from hoshino.typing import CQEvent, MessageSegment

sv = Service(
    "hello_hoshino",
    help_="真机样例：hi / 打招呼 / 计数器 / 帮助 / 图",
    manage_priv=priv.ADMIN,
)

#: 插件自己的配置：优先读数据目录里的 JSON，没有就写一份默认值。
config = util.load_config(__file__)
if not config:
    config = {"greeting": "你好", "hits": 0, "limit_seconds": 3.0}
    util.save_config(__file__, config)

limiter = util.FreqLimiter(float(config.get("limit_seconds") or 3.0))


@sv.on_command("hi", aliases=("你好",), only_to_me=False)
async def hi(bot, ev: CQEvent):
    """打个招呼（``ev['args']`` 是命令参数，shim 提供）"""
    name = str(ev.get("args") or "").strip() or ev.sender.get("nickname") or "朋友"
    config["hits"] = int(config.get("hits", 0)) + 1
    util.save_config(__file__, config)
    await bot.send(ev, f"{config['greeting']}，{name}！（第 {config['hits']} 次）", at_sender=True)


@sv.on_prefix("打招呼")
async def greet(bot, ev: CQEvent):
    """前缀触发器：命中的前缀会被摘掉，handler 拿到剩下的文本"""
    rest = ev.message.extract_plain_text().strip() or "世界"
    await bot.send(ev, f"你好，{rest}")


@sv.on_keyword("帮助", only_to_me=False)
async def help_(bot, ev: CQEvent):
    """关键词触发器"""
    await bot.send(ev, "可用：hi / 你好 / 打招呼 <名字> / 帮助 / 图 <关键词>")


@sv.on_command("图")
async def picture(bot, ev: CQEvent):
    """图片回复：N.E.K.O. 的对话只承载文本，图片段会被记录但不会发出去（日志里能看到）"""
    await bot.send(ev, MessageSegment.image("file:///tmp/hello_hoshino-sample.png"))
    await bot.finish(ev, "（图片段无法投递到 N.E.K.O.，这里补一句文字）")


@sv.on_notice("group_increase")
async def welcome(bot, ev):
    """群成员增加通知（上游 ``on_notice`` 的典型用法）"""
    if not limiter.check("welcome"):
        return
    limiter.start_cd("welcome")
    sv.logger.info(f"新成员 {ev.user_id} 加入了群 {ev.group_id}")
    await bot.send(ev, f"欢迎 {ev.user_id} 加入！")


@sv.scheduled_job("cron", hour=4, minute=30)
async def daily_cleanup():
    """每日清理（兼容层只登记不执行：N.E.K.O. 不承载后台调度）"""
    sv.logger.info("daily_cleanup 本应在这里跑")


@sv.on_startup
async def on_load():
    """插件加载完成后的初始化"""
    sv.logger.info(f"hello_hoshino 就绪（已打招呼 {config.get('hits', 0)} 次）")


@sv.on_shutdown
async def on_unload():
    """插件卸载前的收尾"""
    sv.logger.info("hello_hoshino 已卸载")
