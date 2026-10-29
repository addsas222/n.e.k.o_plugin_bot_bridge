"""``hoshino.service`` 的兼容实现：Service / ServiceFunc / sucmd / Scheduler。

Service 是 Hoshino 插件的"门面"：一个插件通常 ``sv = Service('名字', help_=...)``，然后
用 ``@sv.on_command`` / ``@sv.on_prefix`` / ``@sv.on_message`` / ``@sv.on_notice``
注册处理器，用 ``sv.broadcast`` 群发、``sv.scheduled_job`` 挂定时任务。

与上游的差异（都记在 ``bridge/backends/hoshino.py`` 的 docstring 里）：

* ``Service.scheduler`` / ``scheduled_job`` **只登记不执行**（N.E.K.O. 不承载 apscheduler），
  登记时会写一条 warning 日志，``native["jobs"]`` 里能看到清单；
* ``broadcast`` 只能投递到当前 N.E.K.O. 对话（没有群列表），并标注 ``hoshino_broadcast``；
* ``get_enable_groups`` 显式抛 ``UnsupportedCapabilityError``（拿不到群列表）；
* Service 名重复不 assert（多插件并存时改为"先到先得 + 一条警告日志"），同一插件重复
  load 时旧注册会被覆盖。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from aiocqhttp import _runtime as output

from hoshino import _runtime
from hoshino import trigger as trigger_module
from hoshino.log import new_logger

__all__ = ["Scheduler", "Service", "ServiceFunc", "sucmd"]

#: Service 名不允许包含的字符（与上游同一条规则）。
_ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\.]')


class Scheduler:
    """``Service.scheduler`` / ``hoshino.util.scheduler`` 的最小替身。

    **只登记不执行**：N.E.K.O. 没有后台调度器，注册的 job 会进
    ``LoadedPlugin.native["jobs"]`` 并写一条 warning 日志；``start()`` / ``shutdown()``
    / ``pause()`` / ``resume()`` 这些真正要调度的动作显式抛 ``UnsupportedCapabilityError``。
    """

    def __init__(self, service: Any = None) -> None:
        self._service = service
        self._jobs: list[_runtime.JobRegistration] = []

    # ---- 登记 ------------------------------------------------------------ #

    def scheduled_job(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """装饰器（上游是 apscheduler 的 ``scheduled_job``）。"""

        def deco(func: Callable[..., Any]) -> Callable[..., Any]:
            self._record(func, args, kwargs)
            return func

        return deco

    def add_job(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> _runtime.JobRegistration:
        """命令式登记（上游是 apscheduler 的 ``add_job``）。"""
        return self._record(func, args, kwargs)

    def _record(self, func: Callable[..., Any], args: tuple[Any, ...], kwargs: dict[str, Any]) -> _runtime.JobRegistration:
        trigger = str(args[0]) if args else str(kwargs.pop("trigger", "date"))
        registration = _runtime.JobRegistration(
            func=func,
            trigger=trigger,
            plugin_id=output.current_plugin_id(),
            service_name=str(getattr(self._service, "name", "") or ""),
            args=tuple(args[1:]),
            kwargs={str(key): value for key, value in kwargs.items()},
        )
        self._jobs.append(registration)
        _runtime.register_job(registration)
        return registration

    # ---- 观察 ------------------------------------------------------------ #

    def get_jobs(self) -> list[_runtime.JobRegistration]:
        """已登记的 job（上游返回 apscheduler 的 Job 列表）。"""
        return list(self._jobs)

    @property
    def running(self) -> bool:
        """shim 里没有调度器在跑。"""
        return False

    # ---- 上游要真调度才用得到的动作：显式不支持 -------------------------- #

    def start(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.UnsupportedCapabilityError(
            "hoshino.scheduler.start",
            "N.E.K.O. 不承载后台调度器：定时任务只登记（见 native['jobs']）不会自动执行",
        )

    def shutdown(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.UnsupportedCapabilityError("hoshino.scheduler.shutdown", "见 start()")

    def pause(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.UnsupportedCapabilityError("hoshino.scheduler.pause", "见 start()")

    def resume(self, *args: Any, **kwargs: Any) -> Any:
        raise _runtime.UnsupportedCapabilityError("hoshino.scheduler.resume", "见 start()")

    def remove_job(self, job_id: str) -> Any:
        """按函数名摘掉一条登记。"""
        for index, job in enumerate(self._jobs):
            if job.name == job_id or str(id(job)) == str(job_id):
                self._jobs.pop(index)
                return True
        return False


class ServiceFunc:
    """触发器 / 命令背后的"服务函数"（上游同名）。

    ``plugin_id`` 是本 shim 加的：桥接层要按插件过滤全局触发器链。
    """

    def __init__(
        self,
        sv: "Service",
        func: Callable[..., Any],
        only_to_me: bool,
        normalize_text: bool = False,
    ) -> None:
        self.sv = sv
        self.func = func
        self.only_to_me = only_to_me
        self.normalize_text = normalize_text
        self.plugin_id = getattr(sv, "_plugin_id", "") or output.current_plugin_id()
        self.__name__ = getattr(func, "__name__", "handler")
        self.__doc__ = getattr(func, "__doc__", None)
        self.__wrapped__ = func

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.func(*args, **kwargs)


class Service:
    """一组功能的包装（上游同名）。

    触发条件：``on_message`` / ``on_prefix`` / ``on_fullmatch`` / ``on_suffix`` /
    ``on_keyword`` / ``on_rex`` / ``on_command``；接口：``scheduled_job`` / ``broadcast``。
    """

    def __init__(
        self,
        name: str,
        use_priv: int | None = None,
        manage_priv: int | None = None,
        enable_on_default: bool | None = None,
        visible: bool | None = None,
        help_: str | None = None,
        bundle: str | None = None,
    ) -> None:
        if _ILLEGAL_CHARS.search(name or ""):
            raise ValueError(f'Service name cannot contain character in `\\/:*?"<>|.`: {name!r}')
        self._plugin_id = output.current_plugin_id()
        stored = _load_service_config(name, self._plugin_id)
        self.name = str(name)
        self.use_priv = _coalesce(stored.get("use_priv"), use_priv, 1)
        self.manage_priv = _coalesce(stored.get("manage_priv"), manage_priv, 21)
        self.enable_on_default = bool(_coalesce(stored.get("enable_on_default"), enable_on_default, True))
        self.visible = bool(_coalesce(stored.get("visible"), visible, True))
        self.help = help_
        self.bundle = str(bundle or "通用")
        self.enable_group: set[Any] = set(stored.get("enable_group") or ())
        self.disable_group: set[Any] = set(stored.get("disable_group") or ())
        self.logger = new_logger(self.name, bool(_runtime.settings().get("debug", True)))
        self.scheduler = Scheduler(self)
        self._startups: list[Callable[..., Any]] = []
        self._shutdowns: list[Callable[..., Any]] = []
        _runtime.register_service(
            _runtime.ServiceRecord(
                name=self.name,
                service=self,
                plugin_id=self._plugin_id,
                help=str(help_ or ""),
                use_priv=int(self.use_priv),
                manage_priv=int(self.manage_priv),
                enable_on_default=self.enable_on_default,
                visible=self.visible,
                bundle=self.bundle,
            ),
        )

    # ------------------------------------------------------------------ 基础

    @property
    def bot(self) -> Any:
        """当前插件绑定的机器人对象。"""
        bot = output.RUNTIME.bot_or_none(self._plugin_id)
        if bot is None:
            bot = output.RUNTIME.any_bot()
        if bot is None:
            raise RuntimeError("no bot is bound to this Hoshino plugin (loaded outside bot_bridge?)")
        return bot

    @property
    def plugin_id(self) -> str:
        return self._plugin_id

    @staticmethod
    def get_loaded_services() -> dict[str, "Service"]:
        """已加载的 Service（上游同名静态方法）。"""
        return _runtime.service_registry()

    @staticmethod
    def get_bundles() -> dict[str, list["Service"]]:
        """按 bundle 分组的 Service（上游同名静态方法）。"""
        return _runtime.bundles()

    def set_enable(self, group_id: Any) -> None:
        """开启某群（写入 ``<data_dir>/service_config/<name>.json``）。"""
        self.enable_group.add(group_id)
        self.disable_group.discard(group_id)
        _save_service_config(self)
        self.logger.info(f"Service {self.name} is enabled at group {group_id}")

    def set_disable(self, group_id: Any) -> None:
        """关闭某群（同上）。"""
        self.enable_group.discard(group_id)
        self.disable_group.add(group_id)
        _save_service_config(self)
        self.logger.info(f"Service {self.name} is disabled at group {group_id}")

    def check_enabled(self, group_id: Any) -> bool:
        """这个群有没有启用本服务（上游语义）。"""
        return bool((group_id in self.enable_group) or (self.enable_on_default and group_id not in self.disable_group))

    def _check_all(self, ev: Any) -> bool:
        """启用状态 + 黑名单 + 权限（上游 ``_check_all``）。"""
        from hoshino import priv

        gid = ev.group_id
        return self.check_enabled(gid) and not priv.check_block_group(gid) and priv.check_priv(ev, self.use_priv)

    async def get_enable_groups(self) -> dict:
        """上游返回"启用了本服务的群 → 在线账号"；N.E.K.O. 拿不到群列表，显式不支持。"""
        raise _runtime.UnsupportedCapabilityError(
            "Service.get_enable_groups",
            "N.E.K.O. 没有群列表接口（一条对话就是一个上下文），无法枚举启用了本服务的群",
        )

    # -------------------------------------------------------------- 处理器注册

    def on_message(self, event: str = "group") -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """注册一个消息监听（上游 ``bot.on_message(event)``）。

        handler 收到事件对象（1 个参数时）或 ``(bot, event)``（2 个参数时）。
        """

        def deco(func: Callable[..., Any]) -> Callable[..., Any]:
            _runtime.register_session(
                _runtime.SessionRegistration(
                    kind="message",
                    events=(str(event),),
                    func=func,
                    service=self,
                    plugin_id=self._plugin_id,
                ),
            )
            return func

        return deco

    def on_command(
        self,
        name: str,
        *,
        aliases: Iterable[str] = (),
        only_to_me: bool = False,
        deny_tip: str | None = None,
        **kwargs: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """注册一条命令（上游签名；``**kwargs`` 收下 nonebot v1 的其余参数后忽略）。

        handler 的调用方式按参数个数决定：1 个参数给 ``CommandSession``（上游 v1 风格），
        2 个及以上给 ``(bot, event)``。
        """
        alias_tuple = tuple(str(item) for item in aliases)

        def deco(func: Callable[..., Any]) -> Callable[..., Any]:
            _runtime.register_command(
                _runtime.CommandRegistration(
                    name=str(name),
                    aliases=alias_tuple,
                    func=func,
                    service=self,
                    plugin_id=self._plugin_id,
                    only_to_me=bool(only_to_me),
                    deny_tip=str(deny_tip or ""),
                ),
            )
            if kwargs:
                self.logger.debug(f"on_command({name!r}) 忽略了 nonebot v1 参数：{sorted(kwargs)}")
            return func

        return deco

    def _trigger_deco(
        self,
        kind: str,
        values: tuple[Any, ...],
        only_to_me: bool,
        *,
        normalize: bool = False,
        register: Callable[[Any, ServiceFunc], None] | None = None,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """``on_prefix`` / ``on_suffix`` / ``on_keyword`` / ``on_rex`` / ``on_fullmatch`` 的公共实现。"""
        if kind != "rex" and len(values) == 1 and not isinstance(values[0], str):
            # 上游允许 ``on_prefix(['a', 'b'])`` 这种写法（单个可迭代参数）。
            values = tuple(values[0])

        def deco(func: Callable[..., Any]) -> Callable[..., Any]:
            sf = ServiceFunc(self, func, only_to_me, normalize)
            for value in values:
                if kind != "rex" and not isinstance(value, str):
                    # 上游对类型不对的触发器只记一条错误日志，插件继续加载。
                    self.logger.error(f"Failed to add {kind} trigger `{value}`: expecting `str`, got `{type(value)}`")
                    continue
                if register is not None:
                    register(value, sf)
                else:
                    getattr(trigger_module, kind).add(value, sf)
                _runtime.register_trigger(
                    _runtime.TriggerRegistration(
                        kind=kind,
                        value=value,
                        func=func,
                        service=self,
                        plugin_id=self._plugin_id,
                        only_to_me=only_to_me,
                        normalize_text=normalize,
                    ),
                )
            return func

        return deco

    def on_prefix(self, *prefix: Any, only_to_me: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """前缀触发器（命中最长前缀，运行时摘掉前缀）。"""
        return self._trigger_deco("prefix", prefix, only_to_me)

    def on_suffix(self, *suffix: Any, only_to_me: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """后缀触发器。"""
        return self._trigger_deco("suffix", suffix, only_to_me)

    def on_keyword(
        self,
        *keywords: Any,
        only_to_me: bool = False,
        normalize: bool = True,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """关键词触发器（``normalize=True`` 时用 NFKC + 小写后的文本匹配）。"""
        return self._trigger_deco("keyword", keywords, only_to_me, normalize=normalize)

    def on_rex(
        self,
        rex: Any,
        only_to_me: bool = False,
        normalize: bool = True,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """正则触发器（命中写进 ``ev['match']``）。"""
        return self._trigger_deco("rex", (rex,), only_to_me, normalize=normalize)

    def on_fullmatch(self, *word: Any, only_to_me: bool = False) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """全匹配触发器（整条消息就是这个词）。"""
        return self._trigger_deco("fullmatch", word, only_to_me, register=trigger_module.fullmatch)

    def on_natural_language(
        self,
        keywords: Any = None,
        only_to_me: bool = False,
        **kwargs: Any,
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """自然语言触发器（上游签名 ``on_natural_language(keywords=None, **kwargs)``）。

        上游把它挂到 nonebot v1 的 NLP 管道：**命令都没命中时才轮到它**，handler 返回
        非空字符串即当作回复。桥接层没有 NLP 引擎，因此按最接近的语义落地：

        * 给了 ``keywords`` → 退化成关键词触发器（``on_keyword``，行为可预期）；
        * 没给 → 注册成兜底消息监听（``kind="message"``）。router 的分发顺序是
          「精确命令优先」，所以它天然排在命令之后，等价于「没命中命令时才轮到我」。

        差异写在返回值语义上：上游返回字符串即回复，这里按普通 ``on_message`` 处理
        （返回值不自动回话）。``sessions`` / ``ctx`` 等 v1 参数收下后忽略。
        """
        if kwargs:
            self.logger.debug(f"on_natural_language 忽略了 nonebot v1 参数：{sorted(kwargs)}")
        if keywords:
            values = keywords if isinstance(keywords, (list, tuple, set, frozenset)) else (keywords,)
            return self._trigger_deco("keyword", tuple(values), only_to_me, normalize=True)
        return self.on_message("group")

    def on_notice(self, *events: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """注册通知处理器（handler 1 个参数给 ``NoticeSession``，2 个给 ``(bot, event)``）。"""

        def deco(func: Callable[..., Any]) -> Callable[..., Any]:
            _runtime.register_session(
                _runtime.SessionRegistration(
                    kind="notice",
                    events=tuple(str(item) for item in events),
                    func=func,
                    service=self,
                    plugin_id=self._plugin_id,
                ),
            )
            return func

        return deco

    def on_request(self, *events: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """注册请求处理器（handler 1 个参数给 ``RequestSession``，2 个给 ``(bot, event)``）。"""

        def deco(func: Callable[..., Any]) -> Callable[..., Any]:
            _runtime.register_session(
                _runtime.SessionRegistration(
                    kind="request",
                    events=tuple(str(item) for item in events),
                    func=func,
                    service=self,
                    plugin_id=self._plugin_id,
                ),
            )
            return func

        return deco

    # ------------------------------------------- 生命周期（shim 扩展，上游没有）

    def on_startup(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """插件启动钩子（``bot_bridge`` 的 ``start()`` 阶段调用）。"""
        _runtime.record_hook(self._plugin_id, "startup", func)
        self._startups.append(func)
        return func

    def on_shutdown(self, func: Callable[..., Any]) -> Callable[..., Any]:
        """插件停止钩子（``bot_bridge`` 的 ``stop()`` 阶段逆序调用）。"""
        _runtime.record_hook(self._plugin_id, "shutdown", func)
        self._shutdowns.append(func)
        return func

    # ------------------------------------------------------------------ 定时

    def scheduled_job(self, *args: Any, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """定时任务装饰器（只登记不执行，见 :class:`Scheduler`）。"""
        return self.scheduler.scheduled_job(*args, **kwargs)

    # ------------------------------------------------------------------ 群发

    async def broadcast(
        self,
        msgs: Any,
        TAG: str = "",
        interval_time: float = 0.5,
        randomizer: Callable[[Any], Any] | None = None,
    ) -> dict[str, Any]:
        """上游是「给所有启用了本服务的群发消息」。

        N.E.K.O. 只有当前对话一个投递目标，因此：分发期间这段文本进
        ``CommandResult.replies``；分发之外回落 ``HostServices.send_chat``。返回值里
        ``delivered`` 是进了当前分发的条数，``handed_off`` 是交给宿主的条数，
        ``scope`` 固定是 ``current_conversation``（结构化记录里也标了
        ``hoshino_broadcast``）—— 不假装真的发到了所有群。
        """
        payload = self._broadcast_messages(msgs, randomizer)
        counts = {output.DELIVERED_CAPTURED: 0, output.DELIVERED_HOST: 0, output.DELIVERED_DROPPED: 0}
        for message in payload:
            metadata = {
                "hoshino_broadcast": True,
                "hoshino_service": self.name,
                "tag": TAG,
                # 上游会遍历所有启用了本服务的群；shim 只能投递到当前对话，如实标注。
                "broadcast_scope": "current_conversation",
            }
            status = output.deliver_message(message, self._plugin_id, metadata)
            counts[status] = counts.get(status, 0) + 1
        if payload:
            self.logger.info(
                f"广播 {TAG or self.name}：{counts[output.DELIVERED_CAPTURED]} 条进当前回复、"
                f"{counts[output.DELIVERED_HOST]} 条交给 N.E.K.O. 对话、{counts[output.DELIVERED_DROPPED]} 条被丢弃",
            )
        return {
            "delivered": counts[output.DELIVERED_CAPTURED],
            "handed_off": counts[output.DELIVERED_HOST],
            "dropped": counts[output.DELIVERED_DROPPED],
            "targets": 1 if payload else 0,
            "scope": "current_conversation",
        }

    def _broadcast_messages(self, msgs: Any, randomizer: Callable[[Any], Any] | None) -> list[Any]:
        """把 ``msgs`` 规范成消息列表（上游语义：单个元素也当一条）。"""
        from aiocqhttp.message import Message, MessageSegment

        if isinstance(msgs, (str, MessageSegment, Message)):
            items: list[Any] = [msgs]
        elif isinstance(msgs, Iterable):
            items = list(msgs)
        else:
            items = [msgs]
        if randomizer is not None:
            items = [randomizer(item) for item in items]
        # ``interval_time`` 在上游是"逐群之间歇一会儿"，shim 只投递一次，因此忽略（已文档化）。
        return items


# --------------------------------------------------------------------------- #
# 服务配置文件（上游放 ``~/.hoshino/service_config/<name>.json``）
# --------------------------------------------------------------------------- #


def _config_path(name: str, plugin_id: str) -> Any:
    host = output.RUNTIME.host_or_none()
    if host is None or not plugin_id:
        return None
    root = host.data_dir(plugin_id) / "service_config"
    return root / f"{name}.json"


def _load_service_config(name: str, plugin_id: str) -> dict[str, Any]:
    """读某个服务的分群开关（文件不存在 → ``{}``，与上游一致）。"""
    import json

    path = _config_path(name, plugin_id)
    if path is None or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — 配置坏了要能看见，但不能拦住插件加载
        output.emit_log("warning", f"读取服务配置 {path} 失败：{type(exc).__name__}: {exc}", source=plugin_id)
        return {}
    return dict(data) if isinstance(data, dict) else {}


def _save_service_config(service: Service) -> None:
    """写回分群开关（落 ``<data_dir>/service_config/<name>.json``）。"""
    import json

    path = _config_path(service.name, service.plugin_id)
    if path is None:
        output.emit_log(
            "warning",
            f"没有宿主 / 插件上下文，{service.name} 的分群开关只改了内存（不会持久化）",
            source=service.plugin_id,
        )
        return
    payload = {
        "name": service.name,
        "use_priv": service.use_priv,
        "manage_priv": service.manage_priv,
        "enable_on_default": service.enable_on_default,
        "visible": service.visible,
        "enable_group": sorted(str(item) for item in service.enable_group),
        "disable_group": sorted(str(item) for item in service.disable_group),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:  # noqa: BLE001 — 写失败要如实记日志
        output.emit_log("error", f"写入服务配置 {path} 失败：{type(exc).__name__}: {exc}", source=service.plugin_id)


def _coalesce(*values: Any) -> Any:
    """取第一个非 ``None`` 的值。"""
    for value in values:
        if value is not None:
            return value
    return None


#: 上游 ``hoshino.service`` 里的超级用户命令装饰器。
def sucmd(name: str, force_private: bool = True, **kwargs: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """只允许 ``SUPERUSERS`` 使用的命令（上游同名）。

    上游把 ``privileged=True`` / ``only_to_me=False`` 传给 nonebot v1 的命令系统；shim 的
    权限判定走 ``hoshino.config.SUPERUSERS`` 快照，``force_private=True`` 时要求私聊。
    """
    from hoshino import config

    def deco(func: Callable[..., Any]) -> Callable[..., Any]:
        plugin_id = output.current_plugin_id()

        async def _wrapper(bot: Any, event: Any, *rest: Any) -> Any:
            superusers = {str(item) for item in (config.snapshot().get("SUPERUSERS") or ())}
            if str(event.user_id) not in superusers:
                return None
            if force_private and event.message_type != "private":
                await bot.send(event, "> This command should only be used in private session.")
                return None
            if rest:
                return await func(bot, event, *rest)
            return await func(bot, event)

        _wrapper.__name__ = getattr(func, "__name__", "sucmd")
        _wrapper.__doc__ = getattr(func, "__doc__", None)
        _runtime.register_command(
            _runtime.CommandRegistration(
                name=str(name),
                aliases=(),
                func=_wrapper,
                service=None,
                plugin_id=plugin_id,
                only_to_me=False,
            ),
        )
        return func

    # ``**kwargs``（上游会传 privileged=True 等）在 shim 里没有对应物，直接忽略。
    return deco
