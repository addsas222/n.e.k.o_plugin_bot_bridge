"""N.E.K.O. bot_bridge 的 NoneBot2 兼容 shim（``nonebot`` 包名的**忠实子集**）。

这不是上游 NoneBot2，而是一个**只依赖标准库**的实现，用来在 N.E.K.O. 的
frozen Python 3.11 解释器里直接跑 NoneBot2 插件的源码：

    from nonebot import on_command
    from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent

被桥接层 (``bridge/backends/nonebot.py``) 加载的插件会 ``import nonebot`` 到这里，
而不是真的 NoneBot。事件分发、Matcher 运行、依赖注入、规则/权限、CQ 码消息、
OneBot v11 事件模型、``Matcher.send`` 的回复收集都由本 shim 提供。

## 覆盖范围

* 事件响应器：``on`` / ``on_message`` / ``on_notice`` / ``on_request`` / ``on_metaevent`` /
  ``on_startswith`` / ``on_endswith`` / ``on_fullmatch`` / ``on_keyword`` / ``on_command`` /
  ``on_regex`` / ``on_type``、``Matcher``（``handle`` / ``got`` / ``receive`` / ``send`` /
  ``finish`` / ``pause`` / ``reject`` / ``state`` / ``skip`` / ``stop_propagation`` …）、
  ``matchers``、``MatcherGroup`` / ``CommandGroup``
* 规则与权限：``Rule`` / ``Permission`` / ``User`` / ``USER`` / ``SUPERUSER`` /
  ``MESSAGE`` / ``NOTICE`` / ``REQUEST`` / ``METAEVENT`` / ``startswith`` / ``endswith`` /
  ``fullmatch`` / ``keyword`` / ``command`` / ``regex`` / ``to_me`` / ``is_type``
* 依赖注入：``Depends``、``CommandArg`` / ``RegexGroup`` / ``RegexDict`` / ``RegexStr`` /
  ``RegexMatched`` / ``EventMessage`` / ``EventPlainText`` / ``EventToMe`` / ``EventType`` /
  ``Arg`` / ``ArgStr`` / ``ArgPlainText`` / ``Received`` / ``LastReceived`` /
  ``PausePromptResult`` / ``ReceivePromptResult``，以及按注解注入 ``Bot`` / ``Event`` /
  ``Matcher`` / ``T_State`` / 子依赖
* 适配器：``nonebot.adapters.onebot.v11`` 的 ``Event`` / ``MessageEvent`` /
  ``GroupMessageEvent`` / ``PrivateMessageEvent`` / ``NoticeEvent`` 家族 /
  ``RequestEvent`` 家族 / ``MetaEvent`` 家族 / ``Message`` / ``MessageSegment`` / ``Bot`` /
  ``Adapter`` / ``ActionFailed`` 等异常 / 权限常量 / ``escape`` / ``unescape`` / ``log``
* 插件：``PluginMetadata``（``__plugin_meta__``）/ ``Plugin`` / ``require`` / ``get_plugin`` /
  ``get_loaded_plugins`` / ``get_plugin_config`` / ``require``
* 其它：``nonebot.log.logger``（loguru 风格子集）、``nonebot.get_driver`` /
  ``get_bot`` / ``get_bots`` / ``get_adapter``、``nonebot.message.handle_event``、
  ``event_preprocessor`` / ``run_preprocessor`` / ``event_postprocessor`` /
  ``run_postprocessor``、``Driver.on_startup`` / ``on_shutdown`` / ``on_bot_connect``

## 已知偏差（相对上游 NoneBot2）

1. **没有网络驱动**：不发 HTTP / WebSocket，事件来自 N.E.K.O. 对话总线；
   ``Bot`` 是桥接层合成的，除发消息外的 API 一律抛 ``ApiNotAvailable``（不伪造数据）。
2. **没有 pydantic**：``PluginMetadata`` / 事件模型都是普通 dataclass / 普通类，
   字段名一致但不做类型校验；必填字段缺失时取默认值而不是抛 ``ValidationError``。
3. **没有 loguru / anyio / exceptiongroup**：``logger`` 是 loguru 风格的最小替身
   （``opt`` / ``bind`` 支持但颜色标记是纯文本），并发求解与异常组捕获改为顺序执行 +
   原生 ``try/except``；``SkippedException`` / ``TypeMisMatch`` / ``StopPropagation``
   的语义保持一致。
4. **isolation**：``handle_event(bot, event, plugin_id=...)`` 多了一个按插件过滤事件响
   应器的参数（桥接层需要），``event_preprocessor`` 系列也按插件归属；
   上游是全局单例语义。
5. **扩展**：``Rule.__invert__`` / ``Permission.__invert__``（上游没有，``~rule`` 会抛
   ``TypeError``）；``Matcher._trigger``；``RunReport``；``Matcher.new`` 支持
   ``source`` 显式指定插件归属。
6. **ID 保真**：``user_id`` / ``group_id`` 等能转 int 就转 int，转不了就保留原字符串
   （上游严格 int）。
7. **未实现**：``nonebot.plugin.load_plugin`` / ``load_plugins`` / ``load_all_plugins``
   （运行期动态加载，显式抛 ``RuntimeError``）、``CommandGroup`` 的异步插件管理器形态、
   ``nonebot.adapters.console`` 等其它适配器、``scheduler`` 等社区插件。
   （``on_shell_command`` / ``shell_command`` / ``ArgumentParser`` 已实现，见 ``rule.py``；
   OneBot **v12 适配器已实现并与 v11 并存**，桥接层按插件源码里 import 的适配器分流。）

同一个 ``nonebot`` 模块名只能存在一个实现：若解释器里已经有别的 ``nonebot`` 先被导入，
桥接层会拒绝加载并抛 ``BackendUnavailableError``（绝不半真半假地混用两个实现）。
"""

from __future__ import annotations

from typing import Any

import nonebot.adapters as adapters
from nonebot._runtime import bind_runtime as _bind_runtime  # noqa: F401 — 桥接层入口
from nonebot.drivers import Config as Config
from nonebot.drivers import Driver as Driver
from nonebot.log import logger as logger

__neko_bridge_shim__ = True
"""标记：这是 N.E.K.O. bot_bridge 的 NoneBot2 兼容 shim，不是上游 NoneBot2。"""

__version__ = "0.0.0+neko-bridge"
"""上游版本号占位（shim 不打上游版本号，避免插件误判版本）。"""

_resolved_driver: Driver | None = None


def init(*, _env_file: Any = None, **kwargs: Any) -> None:
    """初始化 NoneBot 与全局 Driver。

    shim 说明：驱动由桥接层在加载插件前绑定（事件来源是 N.E.K.O. 而不是网络），
    因此 ``init()`` 只做两件事——确保驱动存在、把 ``kwargs`` 合并进配置。
    上游的 .env 读取与网络驱动注册不适用。
    """
    global _resolved_driver
    from nonebot import _runtime

    driver = _runtime.driver_or_none()
    if driver is None:
        driver = Driver()
        _bind_runtime(driver=driver)
    for key, value in kwargs.items():
        setattr(driver.config, key, value)
    _resolved_driver = driver
    logger.info("nonebot shim initialized by the N.E.K.O. bot bridge")


def get_driver() -> Driver:
    """获取全局 Driver 实例。"""
    from nonebot import _runtime

    driver = _runtime.driver_or_none()
    if driver is None:
        raise ValueError("NoneBot has not been initialized.")
    return driver


def get_bots() -> dict[str, Any]:
    """获取所有已连接的 Bot（桥接层合成，最多一个）。"""
    from nonebot import _runtime

    bot = _runtime.get_session_bot()
    if bot is None:
        return {}
    return {str(bot.self_id): bot}


def get_bot(self_id: str | None = None) -> Any:
    """获取一个 Bot 对象。"""
    bots = get_bots()
    if self_id is not None:
        try:
            return bots[self_id]
        except KeyError as exc:
            raise KeyError(self_id) from exc
    for bot in bots.values():
        return bot
    raise ValueError("There are no bots to get.")


def get_adapters() -> dict[str, Any]:
    """获取所有已注册的 Adapter 实例。"""
    return dict(get_driver()._adapters)


def get_adapter(name: str | type[Any]) -> Any:
    """获取已注册的 Adapter 实例。"""
    adapters_map = get_adapters()
    target = name if isinstance(name, str) else name.get_name()
    if target not in adapters_map:
        raise ValueError(f"Adapter {target} not registered.")
    return adapters_map[target]


# --------------------------------------------------------------------------- #
# 便捷导入放在文件末尾：``nonebot.rule`` 等子模块依赖 ``get_driver`` 等函数，
# 在它们之前 import 会形成部分初始化循环（与上游 NoneBot 的组织方式一致）。
# --------------------------------------------------------------------------- #

from nonebot.matcher import Matcher as Matcher
from nonebot.matcher import matchers as matchers
from nonebot.message import handle_event as handle_event
from nonebot.permission import Permission as Permission
from nonebot.plugin import CommandGroup as CommandGroup
from nonebot.plugin import MatcherGroup as MatcherGroup
from nonebot.plugin import Plugin as Plugin
from nonebot.plugin import PluginMetadata as PluginMetadata
from nonebot.plugin import get_loaded_plugins as get_loaded_plugins
from nonebot.plugin import get_plugin as get_plugin
from nonebot.plugin import get_plugin_by_module_name as get_plugin_by_module_name
from nonebot.plugin import get_plugin_config as get_plugin_config
from nonebot.plugin import load_all_plugins as load_all_plugins
from nonebot.plugin import load_plugin as load_plugin
from nonebot.plugin import load_plugins as load_plugins
from nonebot.plugin import on as on
from nonebot.plugin import on_command as on_command
from nonebot.plugin import on_endswith as on_endswith
from nonebot.plugin import on_fullmatch as on_fullmatch
from nonebot.plugin import on_keyword as on_keyword
from nonebot.plugin import on_message as on_message
from nonebot.plugin import on_metaevent as on_metaevent
from nonebot.plugin import on_notice as on_notice
from nonebot.plugin import on_regex as on_regex
from nonebot.plugin import on_request as on_request
from nonebot.plugin import on_shell_command as on_shell_command
from nonebot.plugin import on_startswith as on_startswith
from nonebot.plugin import on_type as on_type
from nonebot.plugin import require as require
from nonebot.rule import Rule as Rule

__all__ = [
    "CommandGroup",
    "Config",
    "Driver",
    "Matcher",
    "MatcherGroup",
    "Permission",
    "Plugin",
    "PluginMetadata",
    "Rule",
    "adapters",
    "get_adapter",
    "get_adapters",
    "get_bot",
    "get_bots",
    "get_driver",
    "get_loaded_plugins",
    "get_plugin",
    "get_plugin_by_module_name",
    "get_plugin_config",
    "init",
    "load_all_plugins",
    "load_plugin",
    "load_plugins",
    "logger",
    "matchers",
    "on",
    "on_command",
    "on_endswith",
    "on_fullmatch",
    "on_keyword",
    "on_message",
    "on_metaevent",
    "on_notice",
    "on_regex",
    "on_request",
    "on_shell_command",
    "on_startswith",
    "on_type",
    "require",
]

