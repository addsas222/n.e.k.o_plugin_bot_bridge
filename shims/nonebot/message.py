"""事件分发主流程（上游 ``nonebot.message`` 的子集）。

上游 ``handle_event(bot, event)`` 只返回 ``None``；shim 额外返回
:class:`nonebot._runtime.RunReport`（收集本次分发产生的回复与错误），桥接层据此填
``CommandResult``。参数 ``plugin_id`` 也是 shim 扩展：桥接层按插件隔离事件分发，
避免一个插件的响应器抢另一个插件的事件。

与上游的实现差异：

* 顺序执行（上游同优先级并发执行）；
* ``event_preprocessor`` 系列按插件归属注册（上游是全局的），因此也就按插件分发；
* 临时会话响应器的过期清理在分发时进行（上游有独立定时任务）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from nonebot import _runtime
from nonebot.dependencies import Dependent
from nonebot.exception import IgnoredException, NoLogException, StopPropagation
from nonebot.internal.adapter import Bot, Event
from nonebot.internal.params import (
    ArgParam,
    BotParam,
    DefaultParam,
    DependParam,
    EventParam,
    ExceptionParam,
    MatcherParam,
    StateParam,
)
from nonebot.log import logger
from nonebot.matcher import Matcher, matchers
from nonebot.rule import TrieRule
from nonebot.typing import T_DependencyCache, T_State

__all__ = [
    "check_and_run_matcher",
    "event_postprocessor",
    "event_preprocessor",
    "handle_event",
    "run_matcher",
    "run_postprocessor",
    "run_preprocessor",
]

EVENT_PCS_PARAMS = (DependParam, BotParam, EventParam, StateParam, DefaultParam)
RUN_PREPCS_PARAMS = (DependParam, BotParam, EventParam, StateParam, ArgParam, MatcherParam, DefaultParam)
RUN_POSTPCS_PARAMS = (
    DependParam,
    ExceptionParam,
    BotParam,
    EventParam,
    StateParam,
    ArgParam,
    MatcherParam,
    DefaultParam,
)


def event_preprocessor(func: Any) -> Any:
    """事件预处理：每次接收到事件并分发给各响应器之前执行。"""
    _runtime.record_preprocessor("event_pre", Dependent.parse(call=func, allow_types=EVENT_PCS_PARAMS))
    return func


def event_postprocessor(func: Any) -> Any:
    """事件后处理：每次事件分发之后执行。"""
    _runtime.record_preprocessor("event_post", Dependent.parse(call=func, allow_types=EVENT_PCS_PARAMS))
    return func


def run_preprocessor(func: Any) -> Any:
    """运行预处理：每个事件响应器运行前执行。"""
    _runtime.record_preprocessor("run_pre", Dependent.parse(call=func, allow_types=RUN_PREPCS_PARAMS))
    return func


def run_postprocessor(func: Any) -> Any:
    """运行后处理：每个事件响应器运行后执行。"""
    _runtime.record_preprocessor("run_post", Dependent.parse(call=func, allow_types=RUN_POSTPCS_PARAMS))
    return func


def _matcher_label(matcher_cls: type[Matcher]) -> str:
    trigger = getattr(matcher_cls, "_trigger", None)
    label = getattr(trigger, "label", "") if trigger is not None else ""
    return label or (getattr(matcher_cls, "type", "") or "any")


def _candidate_buckets(plugin_id: str) -> dict[int, list[type[Matcher]]]:
    """按优先级分桶的事件响应器（``plugin_id`` 为空时用全局表）。"""
    if plugin_id:
        candidates: list[type[Matcher]] = _runtime.plugin_matchers(plugin_id)
    else:
        candidates = [matcher for bucket in matchers.values() for matcher in bucket]
    buckets: dict[int, list[type[Matcher]]] = {}
    for matcher_cls in candidates:
        buckets.setdefault(int(getattr(matcher_cls, "priority", 1)), []).append(matcher_cls)
    return buckets


def _drop_expired(matcher_cls: type[Matcher]) -> bool:
    """清理过期的临时响应器（``pause`` / ``reject`` 之后等待超时的会话）。"""
    if not getattr(matcher_cls, "temp", False):
        return False
    expire_time = getattr(matcher_cls, "expire_time", None)
    if expire_time is None or datetime.now() <= expire_time:
        return False
    bucket = matchers.get(int(getattr(matcher_cls, "priority", 0)), None)
    if bucket and matcher_cls in bucket:
        bucket.remove(matcher_cls)
    plugin_id = getattr(matcher_cls, "plugin_id", None)
    if plugin_id:
        _runtime.unregister_matcher(plugin_id, matcher_cls)
    return True


async def _apply_preprocessors(kind: str, plugin_id: str, **kwargs: Any) -> None:
    for proc in _runtime.plugin_preprocessors(plugin_id, kind):
        await proc(**kwargs)


async def check_and_run_matcher(
    Matcher: type[Matcher],  # noqa: N803 — 与上游参数名一致
    bot: Bot,
    event: Event,
    state: T_State,
    stack: Any = None,
    dependency_cache: T_DependencyCache | None = None,
    report: _runtime.RunReport | None = None,
    plugin_id: str = "",
) -> None:
    """检查并运行事件响应器。"""
    if not await Matcher.check_perm(bot, event, stack, dependency_cache):
        return
    if not await Matcher.check_rule(bot, event, state, stack, dependency_cache):
        return
    await _run_matcher(Matcher, bot, event, state, stack, dependency_cache, report, plugin_id)


async def _run_matcher(
    Matcher: type[Matcher],  # noqa: N803 — 与上游参数名一致
    bot: Bot,
    event: Event,
    state: T_State,
    stack: Any,
    dependency_cache: T_DependencyCache | None,
    report: _runtime.RunReport | None,
    plugin_id: str,
) -> None:
    matcher = Matcher()
    if report is not None:
        report.matchers.append(_matcher_label(Matcher))
    await _apply_preprocessors(
        "run_pre", plugin_id, matcher=matcher, bot=bot, event=event, state=state, dependency_cache=dependency_cache
    )
    exception: BaseException | None = None
    try:
        await matcher.run(bot, event, state, stack, dependency_cache)
    except Exception as exc:  # noqa: BLE001 — 运行后处理需要拿到异常，随后原样抛出
        exception = exc
    await _apply_preprocessors(
        "run_post",
        plugin_id,
        matcher=matcher,
        exception=exception,
        bot=bot,
        event=event,
        state=state,
        dependency_cache=dependency_cache,
    )
    if exception is not None:
        raise exception
    if matcher.block:
        raise StopPropagation


async def run_matcher(
    Matcher: type[Matcher],  # noqa: N803 — 与上游参数名一致
    bot: Bot,
    event: Event,
    plugin_id: str = "",
    *,
    check_rule: bool = True,
) -> _runtime.RunReport:
    """只运行一个事件响应器（shim 扩展；``call_tool`` 用它跑指定 matcher）。"""
    report = _runtime.begin_run(plugin_id)
    state: T_State = {}
    dependency_cache: T_DependencyCache = {}
    with _runtime.capture_replies(report):
        try:
            TrieRule.get_value(bot, event, state)
        except Exception as exc:  # noqa: BLE001 — 命令解析失败不影响其余规则
            logger.warning(f"Error while parsing command for event: {exc!r}")
        try:
            if check_rule:
                await check_and_run_matcher(Matcher, bot, event, state, None, dependency_cache, report, plugin_id)
            else:
                await _run_matcher(Matcher, bot, event, state, None, dependency_cache, report, plugin_id)
        except IgnoredException as exc:
            reason = getattr(exc, "reason", exc)
            report.errors.append(f"ignored: {reason}")
        except Exception as exc:  # noqa: BLE001 — 结构化上报，绝不吞掉
            report.errors.append(f"{type(exc).__name__}: {exc}")
            logger.opt(exception=exc).error(f"Error when running matcher {Matcher}")
    return report


async def handle_event(bot: Bot, event: Event, plugin_id: str = "") -> _runtime.RunReport:
    """处理一个事件，返回本次分发的 :class:`nonebot._runtime.RunReport`。"""
    resolved_plugin = plugin_id or _runtime.current_plugin_id()
    report = _runtime.begin_run(resolved_plugin)

    log_string = ""
    try:
        log_string = event.get_log_string()
    except (NoLogException, ValueError):
        log_string = ""
    if log_string:
        logger.success(f"{bot.type} {bot.self_id} | {log_string}")

    state: T_State = {}
    dependency_cache: T_DependencyCache = {}

    with _runtime.capture_replies(report):
        try:
            await _apply_preprocessors("event_pre", resolved_plugin, bot=bot, event=event, state=state)
        except IgnoredException as exc:
            reason = getattr(exc, "reason", exc)
            report.errors.append(f"ignored: {reason}")
            return report
        except Exception as exc:  # noqa: BLE001 — 上游同：预处理出错则该事件被忽略
            report.errors.append(f"event_preprocessor: {type(exc).__name__}: {exc}")
            logger.opt(exception=exc).error("Error when running EventPreProcessors")
            return report

        try:
            TrieRule.get_value(bot, event, state)
        except Exception as exc:  # noqa: BLE001 — 命令解析失败只影响命令类规则
            logger.warning(f"Error while parsing command for event: {exc!r}")

        buckets = _candidate_buckets(resolved_plugin)
        break_flag = False
        for priority in sorted(buckets):
            if break_flag:
                break
            for matcher_cls in buckets[priority]:
                if _drop_expired(matcher_cls):
                    continue
                try:
                    await check_and_run_matcher(
                        matcher_cls,
                        bot,
                        event,
                        dict(state),
                        None,
                        dependency_cache,
                        report,
                        resolved_plugin,
                    )
                except StopPropagation:
                    break_flag = True
                    break
                except IgnoredException as exc:
                    reason = getattr(exc, "reason", exc)
                    report.errors.append(f"ignored: {reason}")
                except Exception as exc:  # noqa: BLE001 — 单个响应器失败不影响其它响应器
                    report.errors.append(f"{type(exc).__name__}: {exc}")
                    logger.opt(exception=exc).error(f"Error when running matcher {matcher_cls}")

        try:
            await _apply_preprocessors("event_post", resolved_plugin, bot=bot, event=event, state=state)
        except Exception as exc:  # noqa: BLE001 — 后处理失败只记录
            report.errors.append(f"event_postprocessor: {type(exc).__name__}: {exc}")

    return report
