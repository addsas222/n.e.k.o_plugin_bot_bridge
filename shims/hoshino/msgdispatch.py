"""消息 / 通知 / 请求的分发（上游 ``hoshino/msghandler.py`` + nonebot v1 的流水线）。

上游把触发器链挂在 ``nonebot.message_preprocessor`` 上（``msghandler.handle_message``），
命令与 ``on_message`` 由 nonebot v1 的流水线负责。shim 的流水线是：

1. ``message_preprocessor`` 注册的预处理函数；
2. 触发器链：prefix → suffix → 规范化 → rex → keyword（顺序与上游 ``trigger.chain`` 一致）；
3. ``Service.on_command`` 注册的命令（命令名 / 别名匹配，支持 ``only_to_me``）；
4. ``Service.on_message`` 注册的消息监听。

上游语义「一条消息最多被一个 handler 消费」（``msghandler`` 处理完就 ``raise
CanceledException('Handled by Hoshino')``）在这里保留：命中即停。``SwitchException``
表示「这个 handler 不接」，继续找下一个候选项。

**按插件过滤**：桥接层一次只跑一个插件的处理器，因此所有匹配都带 ``plugin_id``
（上游是全局单例语义）。

handler 的调用方式与上游一致：

* 触发器 —— ``func(bot, event)``；
* ``on_command`` —— 1 个参数给 ``CommandSession``（nonebot v1 风格），2 个及以上给 ``(bot, event)``；
* ``on_message`` —— 1 个参数给事件本身（上游 ``wrapper(ctx)``），否则 ``(bot, event)``；
* ``on_notice`` / ``on_request`` —— 1 个参数给 ``NoticeSession`` / ``RequestSession``，否则 ``(bot, event)``。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

from aiocqhttp import _runtime as output
from aiocqhttp.control import CanceledException, FinishedException, PauseException, SwitchException
from aiocqhttp.event import Event, NoticeEvent, RequestEvent, build_event

from hoshino import _runtime
from hoshino import trigger as trigger_module
from hoshino.session import CommandSession, NoticeSession, RequestSession, Session

__all__ = [
    "dispatch",
    "handle_event",
    "handle_message",
    "match_command",
    "run_command",
    "run_session",
]

#: 候选项被挡掉的原因（记进 ``RunReport.skipped``，方便排查"命令为什么没反应"）。
SKIP_ONLY_TO_ME = "only_to_me"
SKIP_PERMISSION = "permission"
SKIP_SERVICE_DISABLED = "service_disabled"


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #


async def handle_message(bot: Any, event: Any, plugin_id: str = "") -> _runtime.RunReport:
    """处理一条消息事件（上游同名函数）。"""
    return await dispatch(bot, event, plugin_id)


async def handle_event(bot: Any, event: Any, plugin_id: str = "") -> _runtime.RunReport:
    """按 ``post_type`` 分派消息 / 通知 / 请求事件。"""
    return await dispatch(bot, event, plugin_id)


async def dispatch(bot: Any, event: Any, plugin_id: str = "") -> _runtime.RunReport:
    """一次完整分发：收集回复、记录命中与错误。"""
    pid = plugin_id or output.current_plugin_id()
    report = _runtime.begin_run(pid)
    if isinstance(event, dict):
        event = build_event(event)
    with output.RUNTIME.scope(pid), _runtime.capture_replies(report), output.RUNTIME.running_event(event):
        if isinstance(event, NoticeEvent) or event.post_type == "notice":
            await _dispatch_sessions(bot, event, pid, report, "notice")
        elif isinstance(event, RequestEvent) or event.post_type == "request":
            await _dispatch_sessions(bot, event, pid, report, "request")
        elif event.post_type in ("", "message"):
            await _dispatch_message(bot, event, pid, report)
        else:
            # 元事件（心跳 / 生命周期）没有对应的插件处理器（上游也没有）。
            report.skipped.append(f"meta_event:{event.detail_type}")
    return report


async def run_command(
    bot: Any,
    event: Any,
    registration: _runtime.CommandRegistration,
    plugin_id: str = "",
) -> _runtime.RunReport:
    """只跑一条命令（工具调用用）。"""
    pid = plugin_id or registration.plugin_id or output.current_plugin_id()
    report = _runtime.begin_run(pid)
    if isinstance(event, dict):
        event = build_event(event)
    with output.RUNTIME.scope(pid), _runtime.capture_replies(report), output.RUNTIME.running_event(event):
        await _run_command(bot, event, pid, report, registration)
    return report


async def run_session(
    bot: Any,
    event: Any,
    registration: _runtime.SessionRegistration,
    plugin_id: str = "",
) -> _runtime.RunReport:
    """只跑一个 ``on_message`` / ``on_notice`` / ``on_request`` 处理器（工具调用用）。"""
    pid = plugin_id or registration.plugin_id or output.current_plugin_id()
    report = _runtime.begin_run(pid)
    if isinstance(event, dict):
        event = build_event(event)
    with output.RUNTIME.scope(pid), _runtime.capture_replies(report), output.RUNTIME.running_event(event):
        await _run_session(bot, event, pid, report, registration)
    return report


async def run_trigger(
    bot: Any,
    event: Any,
    registration: _runtime.TriggerRegistration,
    plugin_id: str = "",
) -> _runtime.RunReport:
    """只跑一个触发器处理器（工具调用用）。

    走与正常分发同一套触发器机制：prefix / suffix 会先把命中的前后缀摘掉再交给 handler，
    因此工具调用看到的参数与真实消息一致。
    """
    pid = plugin_id or registration.plugin_id or output.current_plugin_id()
    report = _runtime.begin_run(pid)
    if isinstance(event, dict):
        event = build_event(event)
    with output.RUNTIME.scope(pid), _runtime.capture_replies(report), output.RUNTIME.running_event(event):
        target = None
        for item in trigger_module.chain:
            if item.kind != registration.kind:
                continue
            # 与 _run_triggers 一样必须惰性迭代（prefix / suffix 的改写只在这段窗口里生效）。
            for sf in item.find_handler(event, pid):
                if sf.func is not registration.func:
                    continue
                if registration.kind != "rex" and getattr(sf, "_matched_value", None) != registration.value:
                    continue
                if registration.only_to_me and not event.to_me:
                    report.skipped.append(f"{_trigger_key(item.kind, sf)}#{SKIP_ONLY_TO_ME}")
                    break
                if not _allowed(getattr(sf, "sv", None), event):
                    report.skipped.append(f"{_trigger_key(item.kind, sf)}#{SKIP_PERMISSION}")
                    break
                target = sf
                report.matched_text = str(getattr(sf, "_matched_value", "") or "")
                outcome = await _invoke(
                    sf.func,
                    bot,
                    event,
                    Session(bot, event),
                    pid,
                    report,
                    key=_trigger_key(item.kind, sf),
                    call_style="trigger",
                )
                if outcome != "switch":
                    report.handlers.append(_trigger_key(item.kind, sf))
                break
            if target is not None:
                break
        if target is None:
            # 这条消息没命中该触发器（例如前缀不对）：如实报告"没匹配"，不假装跑过。
            report.skipped.append(f"{registration.kind}:{registration.label}#not_matched")
    return report


# --------------------------------------------------------------------------- #
# 消息流水线
# --------------------------------------------------------------------------- #


async def _dispatch_message(bot: Any, event: Event, plugin_id: str, report: _runtime.RunReport) -> None:
    if await _run_preprocessors(bot, event, plugin_id, report):
        return
    if await _run_triggers(bot, event, plugin_id, report):
        return
    if await _run_commands(bot, event, plugin_id, report):
        return
    await _run_message_sessions(bot, event, plugin_id, report)


async def _run_preprocessors(bot: Any, event: Event, plugin_id: str, report: _runtime.RunReport) -> bool:
    """跑 ``message_preprocessor`` 注册的函数；返回 ``True`` 表示分发应当停下。"""
    state: dict[str, Any] = {}
    for func in _runtime.plugin_preprocessors(plugin_id):
        name = f"preprocessor:{getattr(func, '__name__', 'preprocessor')}"
        try:
            await _call(func, bot, event, Session(bot, event), "preprocessor", state)
        except CanceledException:
            report.handlers.append(name)
            report.finished = True
            return True
        except SwitchException:
            continue
        except (FinishedException, PauseException):
            report.handlers.append(name)
            report.finished = True
            return True
        except Exception as exc:  # noqa: BLE001 — 预处理失败要记下来，但不能吞掉后面的处理器
            _record_error(report, plugin_id, name, exc)
    return False


async def _run_triggers(bot: Any, event: Event, plugin_id: str, report: _runtime.RunReport) -> bool:
    for item in trigger_module.chain:
        # 注意：必须惰性迭代 —— prefix / suffix 触发器在 yield 之前把消息改写成"参数"，
        # 恢复原消息放在生成器收尾处；handler 必须在 yield 与收尾之间跑（上游正是这样）。
        for sf in item.find_handler(event, plugin_id):
            service = getattr(sf, "sv", None)
            key = _trigger_key(item.kind, sf)
            if sf.only_to_me and not event.to_me:
                report.skipped.append(f"{key}#{SKIP_ONLY_TO_ME}")
                continue
            if not _allowed(service, event):
                report.skipped.append(f"{key}#{SKIP_PERMISSION}")
                continue
            report.matched_text = str(getattr(sf, "_matched_value", "") or "")
            outcome = await _invoke(sf.func, bot, event, Session(bot, event), plugin_id, report, key=key, call_style="trigger")
            if outcome == "switch":
                continue
            report.handlers.append(key)
            return True
    return False


async def _run_commands(bot: Any, event: Event, plugin_id: str, report: _runtime.RunReport) -> bool:
    for registration in _runtime.plugin_commands(plugin_id):
        remaining = match_command(event.message.extract_plain_text() or str(event.raw_message or ""), registration)
        if remaining is None:
            continue
        key = f"command:{registration.name}"
        if registration.only_to_me and not event.to_me:
            report.skipped.append(f"{key}#{SKIP_ONLY_TO_ME}")
            continue
        service = registration.service
        if service is not None and not service.check_enabled(event.group_id):
            report.skipped.append(f"{key}#{SKIP_SERVICE_DISABLED}")
            if registration.deny_tip:
                await bot.send(event, registration.deny_tip, at_sender=True)
            continue
        if not _allowed(service, event):
            report.skipped.append(f"{key}#{SKIP_PERMISSION}")
            continue
        report.matched_text = registration.name
        outcome = await _run_command(bot, event, plugin_id, report, registration, args_text=remaining, key=key)
        if outcome == "switch":
            continue
        return True
    return False


async def _run_message_sessions(bot: Any, event: Event, plugin_id: str, report: _runtime.RunReport) -> bool:
    for registration in _runtime.plugin_sessions(plugin_id, "message"):
        if registration.events and not event.is_message_of(*registration.events):
            continue
        if not _allowed(registration.service, event):
            report.skipped.append(f"message:{registration.label}#{SKIP_PERMISSION}")
            continue
        key = f"message:{registration.label}"
        outcome = await _invoke(
            registration.func,
            bot,
            event,
            Session(bot, event),
            plugin_id,
            report,
            key=key,
            call_style="event",
        )
        if outcome == "switch":
            continue
        report.handlers.append(key)
        return True
    return False


async def _run_command(
    bot: Any,
    event: Event,
    plugin_id: str,
    report: _runtime.RunReport,
    registration: _runtime.CommandRegistration,
    *,
    args_text: str | None = None,
    key: str = "",
) -> str:
    """跑一条命令；返回 ``"ok"`` / ``"stop"`` / ``"switch"``。"""
    text = args_text
    if text is None:
        text = match_command(event.message.extract_plain_text() or str(event.raw_message or ""), registration) or ""
    # shim 扩展：把命中的命令名与参数放到事件上（对应 session.current_arg_text；
    # 上游要求 (bot, ev) 风格的 handler 自己从 message 里切，这里只做加法）。
    event["command"] = registration.name
    event["args"] = text
    label = key or f"command:{registration.name}"
    outcome = await _invoke(
        registration.func,
        bot,
        event,
        CommandSession(bot, event, args_text=text),
        plugin_id,
        report,
        key=label,
        call_style="command",
    )
    if outcome != "switch":
        report.handlers.append(label)
    return outcome


# --------------------------------------------------------------------------- #
# 通知 / 请求
# --------------------------------------------------------------------------- #


async def _dispatch_sessions(
    bot: Any,
    event: Event,
    plugin_id: str,
    report: _runtime.RunReport,
    kind: str,
) -> None:
    for registration in _runtime.plugin_sessions(plugin_id, kind):
        if registration.events and not _matches_event_filter(event, registration.events):
            continue
        service = registration.service
        if service is not None and not service.check_enabled(event.group_id):
            report.skipped.append(f"{kind}:{registration.label}#{SKIP_SERVICE_DISABLED}")
            continue
        key = f"{kind}:{registration.label}"
        outcome = await _run_session(bot, event, plugin_id, report, registration, key=key)
        if outcome == "switch":
            continue
        return


async def _run_session(
    bot: Any,
    event: Event,
    plugin_id: str,
    report: _runtime.RunReport,
    registration: _runtime.SessionRegistration,
    *,
    key: str = "",
) -> str:
    session: Session
    if registration.kind == "notice":
        session = NoticeSession(bot, event)
    elif registration.kind == "request":
        session = RequestSession(bot, event)
    else:
        session = Session(bot, event)
    label = key or f"{registration.kind}:{registration.label}"
    outcome = await _invoke(
        registration.func,
        bot,
        event,
        session,
        plugin_id,
        report,
        key=label,
        call_style=registration.kind,
    )
    if outcome != "switch":
        report.handlers.append(label)
    return outcome


# --------------------------------------------------------------------------- #
# 调用 handler
# --------------------------------------------------------------------------- #


async def _invoke(
    func: Callable[..., Any],
    bot: Any,
    event: Event,
    session: Session,
    plugin_id: str,
    report: _runtime.RunReport,
    *,
    key: str,
    call_style: str,
) -> str:
    """按 handler 的参数个数决定调用方式，返回 ``"ok"`` / ``"stop"`` / ``"switch"``。"""
    try:
        await _call(func, bot, event, session, call_style)
    except CanceledException:
        report.finished = True
        return "stop"
    except (FinishedException, PauseException):
        report.finished = True
        return "stop"
    except SwitchException:
        return "switch"
    except Exception as exc:  # noqa: BLE001 — 上游把异常记进日志后**仍然算这条消息被消费**
        _record_error(report, plugin_id, _handler_name(func, key), exc)
        return "stop"
    output.emit_log("debug", f"hoshino handler {_handler_name(func, key)} 处理完成", source=plugin_id)
    return "ok"


async def _call(
    func: Callable[..., Any],
    bot: Any,
    event: Event,
    session: Session,
    call_style: str,
    state: dict[str, Any] | None = None,
) -> Any:
    """按调用风格与签名决定传 session / event / ``(bot, event)`` / 无参。"""
    count, varargs = _signature_of(func)
    if count == 0 and not varargs:
        return await _await(func())
    if call_style == "trigger":
        # 触发器在上游永远是 ``func(bot, event)``。
        return await _await(func(bot, event))
    if call_style == "preprocessor":
        # 上游 ``@message_preprocessor`` 的签名是 ``func(bot, event, state)``。
        if count >= 3 or varargs:
            return await _await(func(bot, event, state if state is not None else {}))
        if count == 2:
            return await _await(func(bot, event))
        return await _await(func(event))
    if count == 1 and not varargs:
        if call_style == "command":
            return await _await(func(session))
        if call_style == "event":
            # ``on_message`` 的 1 参数写法在上游拿到的是事件本身（``wrapper(ctx)``）。
            return await _await(func(event))
        return await _await(func(session))
    return await _await(func(bot, event))


async def _await(result: Any) -> Any:
    if inspect.isawaitable(result):
        return await result
    return result


def _record_error(report: _runtime.RunReport, plugin_id: str, label: str, exc: BaseException) -> str:
    detail = f"{label}: {type(exc).__name__}: {exc}"
    report.errors.append(detail)
    output.emit_log("error", f"hoshino handler {detail}", source=plugin_id)
    return detail


def _signature_of(func: Callable[..., Any]) -> tuple[int, bool]:
    """``(位置参数个数, 是否有 *args)``；拿不到签名时按 ``(bot, event)`` 处理。"""
    target = getattr(func, "__wrapped__", None) or func
    try:
        signature = inspect.signature(target)
    except (TypeError, ValueError):
        return 2, False
    count = 0
    varargs = False
    for parameter in signature.parameters.values():
        if parameter.kind in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD):
            count += 1
        elif parameter.kind is parameter.VAR_POSITIONAL:
            varargs = True
    return count, varargs


# --------------------------------------------------------------------------- #
# 匹配
# --------------------------------------------------------------------------- #


def command_start() -> tuple[str, ...]:
    """命令前缀（``hoshino_config`` 的 ``COMMAND_START``；默认 ``('/')``）。"""
    values = _runtime.config_snapshot().get("COMMAND_START") or ("/",)
    if isinstance(values, str):
        values = (values,)
    return tuple(str(item) for item in values if str(item))


def match_command(text: str, registration: _runtime.CommandRegistration) -> str | None:
    """命令名 / 别名匹配；命中返回参数文本，未命中返回 ``None``。

    语义（nonebot v1 的简化）：去掉可选的命令前缀 → 取第一个空白分隔的词 → 必须是命令名或
    别名之一。子命令（``cmd.sub``）与 argparse 参数解析没有实现（见兼容层 docstring 的缺口）。
    """
    stripped = _strip_prefix(str(text or ""))
    token, _, remainder = stripped.partition(" ")
    names = {registration.name, *(str(item) for item in registration.aliases)}
    if token and token in names:
        return remainder.strip()
    return None


def _strip_prefix(text: str) -> str:
    stripped = text.strip()
    for prefix in command_start():
        if stripped.startswith(prefix):
            return stripped[len(prefix) :].strip()
    return stripped


def _matches_event_filter(event: Event, filters: tuple[str, ...]) -> bool:
    """``on_notice('group_increase')`` / ``on_request('group.add')`` 的过滤。"""
    detail = str(event.notice_type or event.request_type or "")
    candidates = {
        detail,
        str(event.detail_type or ""),
        str(event.sub_type or ""),
        f"{detail}.{event.sub_type}" if detail and event.sub_type else "",
    }
    return any(str(item) in candidates for item in filters)


def _allowed(service: Any, event: Event) -> bool:
    """服务开关 + 黑名单 + 权限（上游 ``Service._check_all``）。"""
    if service is None:
        return True
    try:
        return bool(service._check_all(event))
    except Exception as exc:  # noqa: BLE001 — 权限判定失败要记日志，不能掀翻分发
        output.emit_log("error", f"hoshino 权限判定失败：{type(exc).__name__}: {exc}", source=service.plugin_id)
        return False


def _trigger_key(kind: str, sf: Any) -> str:
    value = getattr(sf, "_matched_value", None)
    if value is None or value == "":
        return f"{kind}:{getattr(sf, '__name__', 'handler')}"
    if isinstance(value, str):
        return f"{kind}:{value}"
    pattern = getattr(value, "pattern", None)
    return f"{kind}:{pattern if pattern is not None else value}"


def _handler_name(func: Callable[..., Any], key: str) -> str:
    return f"{key}={getattr(func, '__name__', 'handler')}"
