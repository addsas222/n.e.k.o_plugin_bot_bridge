"""机器人插件兼容桥（N.E.K.O. Adapter）。

把 MaiBot / NoneBot2 / AstrBot / Koishi 生态的机器人插件装进 N.E.K.O.：

* 外来插件的**命令 / 工具** → N.E.K.O. 的插件入口与对话期工具（猫娘可以直接调用）；
* 外来插件的**发送 / 大模型 / 存储 / 配置 / 插件互调**能力 → N.E.K.O. 自己的实现；
* 外来插件的**事件处理器** → 桥接层的事件分发（入站消息、消息回流）。

设计边界（重要）：
宿主不会替适配器调用 ``adapter_startup`` / ``handle_adapter_message``（仓库里没有
任何调用点），所以本插件在自己的 ``@lifecycle`` 里驱动适配器生命周期，并自带入站
通道：显式入口 ``bridge_run_command`` 与可选的总线轮询（``[bridge].poll_conversations``）。
映射不了的外来能力一律返回结构化失败，绝不假装成功。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from plugin.sdk.adapter import NekoAdapterPlugin
from plugin.sdk.plugin import Err, Ok, SdkError, lifecycle, neko_plugin, plugin_entry, tr, ui

from .bridge import NEKOHostServices
from .bridge.contracts import CommandContext
from .bridge.errors import BridgeError
from .bridge.registry import BridgeRegistry
from .bridge.router import BridgeRouter

#: 外来插件目录里没有配置时使用的默认值。
DEFAULT_SETTINGS: dict[str, Any] = {
    "plugins_root": "",
    # 各框架单独的插件源目录（[bridge.framework_dirs]）；空 = 全部走 plugins_root。
    # 目录不存在只告警跳过，不会代建；同一插件同时出现在框架源目录与公共根目录时，
    # 框架源目录优先。
    "framework_dirs": {},
    # 加载前的静态安全扫描（bridge/security.py）：report = 亮灯照常加载；enforce = 高风险命中拒绝加载。
    "security_scan": {
        "enabled": True,
        "mode": "report",
    },
    "enabled_frameworks": [],
    # NoneBot2 的 OneBot 适配器版本：auto（按插件源码扫描）/ v11 / v12（强制）。
    # 只在「双协议插件要强制走 v12」这类场景才需要改；见 config.example.toml。
    "nonebot_adapter": "auto",
    "disabled_plugins": [],
    "max_plugins_per_framework": 32,
    "continue_on_error": True,
    "expose_commands_as_tools": True,
    "expose_plugins_tools": True,
    "max_exposed_tools": 40,
    "default_visibility": ["chat"],
    "default_ai_behavior": "respond",
    "max_push_chars": 4000,
    # 单次外来命令/工具调用的上限。必须**小于宿主给入口的预算**（本插件实测：market 三条 300s，
    # 其余 30s —— 35s 的夹具命令在 30.3s 被宿主砍掉），这样超时是我们自己返回结构化错误，
    # 而不是宿主给一个「跑失败」。
    "call_timeout": 25.0,
    "forward_plugin_logs": True,
    "poll_conversations": False,
    "poll_interval_seconds": 10,
    "poll_user_only": True,
    "llm_usage_id": "chat",
    "embedding_usage_id": "embedding",
    "market": {
        "enabled": True,
        "allow_install": True,
        "sources": [],
        "mirror_prefixes": ["https://ghproxy.net/"],
        "allow_hosts": [],
        "cache_ttl_seconds": 600,
        # 单次索引/下载请求上限：astrbot 索引实测 ~25s，30s 会贴边超时。
        "timeout": 60.0,
        "max_archive_bytes": 33554432,
    },
    "capabilities": {
        "send": True,
        "llm": True,
        "storage": True,
        "config": True,
        "plugin_call": True,
        "chat_query": True,
    },
}


@neko_plugin
class BotBridgePlugin(NekoAdapterPlugin):
    """把其它机器人框架的插件加载进 N.E.K.O. 的适配器。"""

    def __init__(self, ctx: Any):
        super().__init__(ctx)
        self._settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        #: 后台的配置镜像任务（配置写回可能很慢；启停入口不等它）
        self._config_mirror_tasks: set[asyncio.Task[None]] = set()
        self._host: NEKOHostServices | None = None
        self._registry: BridgeRegistry | None = None
        self._router: BridgeRouter | None = None
        self._inbound_task: asyncio.Task[None] | None = None
        self._last_turn_ts: float = 0.0
        self._seen_turns: dict[str, float] = {}
        self._market_obj: Any = None
        self._market_error: str = ""
        self._market_sources_errors: dict[str, str] = {}

    # ------------------------------------------------------------------ 工具

    def _log(self, level: str, message: str) -> None:
        logger = getattr(self, "logger", None) or getattr(self.ctx, "logger", None)
        if logger is None:
            return
        try:
            getattr(logger, level, logger.info)(message)
        except Exception:  # noqa: BLE001 — 日志永远不该影响主流程
            pass

    async def _settings_from_config(self) -> dict[str, Any]:
        """读 ``[bridge]`` 配置段；读不到就用默认值。"""
        settings = dict(DEFAULT_SETTINGS)
        raw: object = None
        try:
            raw = await self.config.dump()
        except Exception as exc:  # noqa: BLE001 — 配置读失败不能让桥接起不来
            self._log("warning", f"[bot-bridge] 读取插件配置失败，使用默认值：{exc!r}")
        if isinstance(raw, Mapping):
            section = raw.get("bridge")
            if isinstance(section, Mapping):
                for key, value in section.items():
                    settings[str(key)] = value
            legacy = raw.get("bot_bridge")
            if isinstance(legacy, Mapping):
                for key, value in legacy.items():
                    settings.setdefault(str(key), value)
        caps = settings.get("capabilities")
        if not isinstance(caps, Mapping):
            settings["capabilities"] = dict(DEFAULT_SETTINGS["capabilities"])
        else:
            merged = dict(DEFAULT_SETTINGS["capabilities"])
            for key, value in caps.items():
                merged[str(key)] = value
            settings["capabilities"] = merged
        return settings

    # -------------------------------------------------------------- 生命周期

    @lifecycle(id="startup")
    async def on_startup(self, **_kwargs: Any) -> Any:
        """启动适配器：加载配置、拉起外来插件、注册桥接面。"""
        try:
            startup = await self.adapter_startup()
        except Exception as exc:  # noqa: BLE001 — 适配器自身生命周期失败要能看见
            return Err(SdkError(f"adapter_startup failed: {type(exc).__name__}: {exc}"))
        if isinstance(startup, Err):
            return startup

        self._settings = await self._settings_from_config()
        try:
            self._host = NEKOHostServices(self, self._settings)
            self._registry = BridgeRegistry(self._host)
            self._router = BridgeRouter(self, self._host, self._registry)
        except Exception as exc:  # noqa: BLE001 — 宿主能力层起不来就别往下走
            self._log("error", f"[bot-bridge] 初始化宿主能力层失败：{type(exc).__name__}: {exc}")
            self._host = None
            self._registry = None
            self._router = None
            return Err(SdkError(f"host services init failed: {type(exc).__name__}: {exc}"))

        # 启停状态落两份（注意：必须在 host 就绪之后做——状态文件路径来自 host.data_root，
        # 读和写必须是同一个根，否则「停用 → 重启」会读不到）：插件数据目录里的状态文件（**权威**）与宿主配置（镜像，供人看/手改）。
        # 真机上宿主配置写回会超时（Config persistence response timed out），所以：
        #   * 状态文件存在 → 只信状态文件（配置只是镜像，改它删不掉禁用项）；
        #   * 状态文件不存在 → 首次运行/从配置迁移，采纳配置里的值并把它写成状态文件。
        configured = {
            str(item).strip() for item in (self._settings.get("disabled_plugins") or []) if str(item).strip()
        }
        if self._disabled_store_exists():
            merged_disabled = sorted(set(self._load_disabled_store()))
            shadowed = sorted(configured - set(merged_disabled))
            if shadowed:
                self._log(
                    "info",
                    f"[bot-bridge] [bridge].disabled_plugins 里的 {shadowed} 与状态文件不一致，"
                    "按状态文件为准（配置是镜像，要改请用面板或 bridge_set_plugin）",
                )
        else:
            merged_disabled = sorted(configured | set(self._load_disabled_store()))
            if merged_disabled:
                self._write_disabled_store(merged_disabled)
        self._settings["disabled_plugins"] = merged_disabled
        # 防御性重同步：注册表通过 host.settings 读这份数据，万一将来 host 又改成
        # 构造时快照，这里显式再同步一次（当前 host 持有同一份 dict，这行是幂等的）。
        resync = getattr(self._host, "sync_settings", None)
        if callable(resync):
            try:
                resync(self._settings)
            except Exception as exc:  # noqa: BLE001 — 同步失败不能挡住启动
                self._log("warning", f"[bot-bridge] 同步 host settings 失败：{type(exc).__name__}: {exc}")

        try:
            await self._registry.start()
        except Exception as exc:  # noqa: BLE001 — 加载失败要如实上报
            self._log("error", f"[bot-bridge] 加载机器人插件失败：{type(exc).__name__}: {exc}")
            return Err(SdkError(f"bot plugin loading failed: {type(exc).__name__}: {exc}"))

        stats = self._router.register_surfaces()
        self._register_market_tool()
        self._register_admin_tool()
        return Ok(
            {
                "status": "ready",
                "plugins": len(self._registry.plugins),
                "tools": stats["tools"],
                "entries": stats["entries"],
                "plugins_root": str(self._host.plugins_root),
            }
        )

    async def _on_command_loop_start(self) -> None:
        """宿主在常驻命令循环上调用（入口也跑在这个循环上）：后台任务只能挂在这里。

        startup 跑在宿主一次性的 ``asyncio.run`` 里，返回即关循环、取消挂在上面的任务；
        Koishi 子进程在 startup 里绑的也是那个循环，这里换成常驻循环。
        """
        loop = asyncio.get_running_loop()
        registry = self._registry
        for loaded in (registry.plugins.values() if registry is not None else ()):
            attach = getattr(loaded.native, "attach_loop", None)
            if callable(attach):
                attach(loop)
        if self._host is not None and bool(self._settings.get("poll_conversations", False)):
            self._inbound_task = asyncio.create_task(self._poll_loop(), name="bot_bridge_poll")

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_kwargs: Any) -> Any:
        """停止：先停入站轮询，再卸桥接面，最后停外来插件。"""
        task = self._inbound_task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 — 收尾阶段的异常只记录
                self._log("warning", f"[bot-bridge] 入站轮询停止异常：{exc!r}")
        self._inbound_task = None
        self._unregister_market_tool()
        self._unregister_admin_tool()
        router = self._router
        if router is not None:
            router.unregister_surfaces()
        registry = self._registry
        if registry is not None:
            try:
                await registry.stop()
            except Exception as exc:  # noqa: BLE001 — 停止失败要记录，但不能拦住关停
                self._log("warning", f"[bot-bridge] 停止机器人插件异常：{exc!r}")
        try:
            await self.adapter_shutdown()
        except Exception as exc:  # noqa: BLE001
            self._log("warning", f"[bot-bridge] adapter_shutdown 异常：{exc!r}")
        return Ok({"status": "stopped"})

    # ------------------------------------------------------------------ 入口

    @plugin_entry(
        id="bridge_status",
        name="兼容桥状态",
        description="查看已加载的机器人插件、各框架兼容层状态与最近一次错误。",
        input_schema={"type": "object", "properties": {}},
    )
    async def bridge_status(self, **_kwargs: Any) -> Any:
        registry = self._registry
        if registry is None:
            return Err(SdkError("桥接层尚未启动"))
        router = self._router
        payload = registry.status()
        payload["bridge"] = router.status() if router is not None else {}
        payload["recent_pushes"] = await self._recent_pushes()
        return Ok(payload)

    async def _recent_pushes(self, limit: int = 10) -> list[dict[str, Any]]:
        """最近的对话推送记录 —— 用来核对「外来插件的消息到底有没有进 N.E.K.O.」。

        数据来自宿主总线 ``messages`` 存储（``push_message`` 的真正落点），
        而不是桥接层自己的记账，所以能作为端到端的旁证。
        """
        host = self._host
        if host is None:
            return []
        try:
            records = await host.recent_messages(limit=limit)
        except Exception as exc:  # noqa: BLE001 — 读不到就返回空，别把状态查询弄崩
            self._log("warning", f"[bot-bridge] 读取最近推送失败：{type(exc).__name__}: {exc}")
            return []
        return [_compact_push(record) for record in records]

    @plugin_entry(
        id="bridge_list_plugins",
        name="列出机器人插件",
        description="列出所有已加载的机器人插件（MaiBot / NoneBot2 / AstrBot / Koishi 等）及其命令、工具、事件。",
        input_schema={"type": "object", "properties": {}},
        llm_result_fields=["plugins", "count"],
    )
    async def bridge_list_plugins(self, **_kwargs: Any) -> Any:
        router = self._router
        if router is None:
            return Err(SdkError("桥接层尚未启动"))
        plugins = router.list_payload()
        return Ok({"count": len(plugins), "plugins": plugins})

    @plugin_entry(
        id="bridge_run_command",
        name="运行机器人插件命令",
        timeout=90.0,
        description="把一条用户消息交给外来机器人插件匹配并执行。",
        input_schema={
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "用户消息原文"},
                "plugin": {
                    "type": "string",
                    "description": "只交给这个插件处理（插件键，形如 maibot:hello_maibot；留空 = 按加载顺序找第一个命中的）",
                },
                "user_id": {"type": "string", "description": "用户标识（可选）"},
                "group_id": {"type": "string", "description": "群标识（可选）"},
            },
            "required": ["text"],
        },
        llm_result_fields=["summary", "handled", "replies"],
    )
    async def bridge_run_command(
        self,
        text: str = "",
        plugin: str = "",
        user_id: str = "",
        group_id: str = "",
        **_kwargs: Any,
    ) -> Any:
        router = self._router
        if router is None:
            return Err(SdkError("桥接层尚未启动"))
        context = CommandContext(
            text=str(text or ""),
            user_id=str(user_id or ""),
            group_id=str(group_id or ""),
            is_group=bool(group_id),
            source="plugin_entry",
            channel="neko",
        )
        try:
            result = await router.handle_text(context, only=str(plugin or ""))
        except BridgeError as exc:
            return Err(SdkError(str(exc)))
        payload = result.to_payload()
        payload["summary"] = "\n".join(result.replies) or (
            f"已由 {result.framework}:{result.plugin_id} 的 {result.command} 处理" if result.handled else "没有插件处理这条消息"
        )
        return Ok(payload)

    @plugin_entry(
        id="bridge_call_api",
        name="调用机器人插件接口",
        timeout=90.0,
        description="调用外来机器人插件对外暴露的 API 或工具。",
        input_schema={
            "type": "object",
            "properties": {
                "plugin": {"type": "string", "description": "插件键，形如 maibot:hello_maibot"},
                "name": {"type": "string", "description": "API 或工具名"},
                "arguments": {"type": "object", "description": "参数对象", "additionalProperties": True},
                "kind": {"type": "string", "enum": ["tool", "api"], "description": "调用类型，默认 tool"},
            },
            "required": ["plugin", "name"],
        },
        llm_result_fields=["summary", "success"],
    )
    async def bridge_call_api(
        self,
        plugin: str = "",
        name: str = "",
        arguments: dict[str, Any] | None = None,
        kind: str = "tool",
        **_kwargs: Any,
    ) -> Any:
        router = self._router
        if router is None:
            return Err(SdkError("桥接层尚未启动"))
        arguments = dict(arguments or {})
        try:
            if str(kind) == "api":
                payload = await router.call_plugin_api(str(plugin), str(name), arguments)
            else:
                payload = await router.call_plugin_tool(str(plugin), str(name), arguments)
        except BridgeError as exc:
            return Err(SdkError(str(exc)))
        payload.setdefault("summary", str(payload.get("result") or payload.get("error") or "已完成"))
        return Ok(payload)

    @ui.action(
        id="bridge_set_plugin",
        label=tr("ui.action.togglePlugin", default="启用/停用插件"),
        refresh_context=True,
    )
    @plugin_entry(
        id="bridge_set_plugin",
        name="启停机器人插件",
        # 只动一个插件（加载/卸载 + 重投影），不重启整座桥。
        timeout=90.0,
        description="启动或停止单个机器人插件（不重启整座桥；停用会记进 [bridge].disabled_plugins，下次启动不再拉起）。",
        input_schema={
            "type": "object",
            "properties": {
                "plugin_id": {"type": "string", "description": "插件名：框架:插件 / 原生 id / 唯一后缀"},
                "enabled": {"type": "boolean", "description": "true = 启动，false = 停止"},
            },
            "required": ["plugin_id"],
        },
        llm_result_fields=["plugin_id", "enabled", "changed"],
    )
    async def bridge_set_plugin(self, plugin_id: str = "", enabled: bool = True, **_kwargs: Any) -> Any:
        registry = self._registry
        router = self._router
        if registry is None or router is None:
            return Err(SdkError("桥接层尚未启动"))
        try:
            payload = await registry.set_enabled(str(plugin_id), bool(enabled))
        except BridgeError as exc:
            return Err(SdkError(str(exc)))
        await self._persist_disabled(payload.get("disabled_plugins") or [])
        await self._reproject_surfaces()
        payload["ok"] = True
        payload["summary"] = (
            f"已{'启动' if payload.get('enabled') else '停止'} {payload.get('key')}"
            + ("" if payload.get("changed") else "（状态未变）")
        )
        return Ok(payload)

    @ui.action(
        id="bridge_delete_plugin",
        label=tr("ui.action.deletePlugin", default="删除插件"),
        tone="danger",
        refresh_context=True,
        confirm=tr("ui.action.deleteConfirm", default="确认删除这个插件？插件文件会被真实删除（数据目录保留）。"),
    )
    @plugin_entry(
        id="bridge_delete_plugin",
        name="删除机器人插件",
        # rmtree + 状态清理
        timeout=120.0,
        description="删除一个机器人插件：先停用，再删掉插件文件（只允许删插件目录里的东西）；purge_state=true 时连带清理它的数据/缓存目录。",
        input_schema={
            "type": "object",
            "properties": {
                "plugin_id": {"type": "string", "description": "插件名：框架:插件 / 原生 id / 唯一后缀"},
                "purge_state": {"type": "boolean", "description": "是否连带删除该插件的数据/缓存目录（默认否）"},
            },
            "required": ["plugin_id"],
        },
        llm_result_fields=["plugin_id", "removed"],
    )
    async def bridge_delete_plugin(
        self, plugin_id: str = "", purge_state: bool = False, **_kwargs: Any
    ) -> Any:
        registry = self._registry
        router = self._router
        if registry is None or router is None:
            return Err(SdkError("桥接层尚未启动"))
        try:
            payload = await registry.delete(str(plugin_id), purge_state=bool(purge_state))
        except BridgeError as exc:
            return Err(SdkError(str(exc)))
        await self._persist_disabled(payload.get("disabled_plugins") or [])
        await self._reproject_surfaces()
        payload["ok"] = True
        payload["summary"] = f"已删除 {payload.get('key')}（{payload.get('removed')}）"
        return Ok(payload)

    def _disabled_store_path(self) -> Path | None:
        """插件数据目录里的启停状态文件（宿主的配置写回超时时的兜底）。"""
        host = getattr(self, "_host", None)
        try:
            if host is not None:
                # host.data_root 在离线测试里被重定向到临时目录；SDK 的 data_path()
                # 永远指向真机目录，会让测试读写真机状态。
                return Path(host.data_root) / "disabled_plugins.json"
            return Path(self.data_path("disabled_plugins.json"))
        except Exception:  # noqa: BLE001 — 拿不到数据目录就只写配置
            return None

    def _load_disabled_store(self) -> list[str]:
        path = self._disabled_store_path()
        if path is None or not path.exists():
            return []
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._log("warning", f"[bot-bridge] 读取启停状态文件失败（{path}）：{exc}")
            return []
        if not isinstance(payload, list):
            return []
        return [str(item).strip() for item in payload if str(item).strip()]

    def _disabled_store_exists(self) -> bool:
        path = self._disabled_store_path()
        return bool(path is not None and path.exists())

    def _write_disabled_store(self, disabled: list[str]) -> list[str]:
        """同步写状态文件（原子替换）；这是启停状态**唯一权威**来源。"""
        items = sorted({str(item).strip() for item in disabled if str(item).strip()})
        path = self._disabled_store_path()
        if path is None:
            return items
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(path.name + ".tmp")
            temp.write_text(json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temp.replace(path)
        except OSError as exc:
            self._log("warning", f"[bot-bridge] 写启停状态文件失败（{path}）：{type(exc).__name__}: {exc}")
        return items

    async def _persist_disabled(self, disabled: list[str]) -> None:
        """保存启停状态：状态文件**同步写**，宿主配置**后台镜像**。

        真机实测：``config.update`` 会卡到客户端超时（宿主侧连
        ``Updated config for plugin bot_bridge`` 都没打出来），所以：
        * 状态文件先落稳 —— 停用/启用立刻生效、跨重启有效；
        * 配置写回放进后台任务（短超时），**绝不**让启停入口等它（否则宿主卡住时
          每次启停都要等满超时）。
        """
        items = self._write_disabled_store(disabled)
        try:
            task = asyncio.create_task(self._mirror_disabled_to_config(items))
        except RuntimeError:  # 没有事件循环（同步上下文）：下次启动合并时再补
            return
        self._config_mirror_tasks.add(task)
        task.add_done_callback(self._config_mirror_tasks.discard)

    async def flush_config_mirror(self, timeout: float = 6.0) -> None:
        """等后台的配置镜像写完（测试与关闭时用；失败不影响启停本身）。"""
        pending = list(self._config_mirror_tasks)
        if pending:
            await asyncio.wait(pending, timeout=timeout)

    async def _mirror_disabled_to_config(self, items: list[str]) -> None:
        """把禁用列表镜像进 ``[bridge].disabled_plugins``（失败只告警，状态文件已经写了）。"""
        try:
            await self.config.update({"bridge": {"disabled_plugins": list(items)}}, timeout=5.0)
        except Exception as exc:  # noqa: BLE001 — 配置写不进去不影响启停
            self._log(
                "warning",
                f"[bot-bridge] 镜像 disabled_plugins 到配置失败（状态文件已生效）：{type(exc).__name__}: {exc}",
            )

    async def _reproject_surfaces(self) -> dict[str, int]:
        """重新投影桥接面（工具/入口）——启停/删除单个插件后调用。"""
        router = self._router
        if router is None:
            return {}
        router.unregister_surfaces()
        return router.register_surfaces()

    @ui.action(id="bridge_reload", label=tr("ui.action.reload", default="重新扫描插件"), tone="primary", refresh_context=True)
    @plugin_entry(
        id="bridge_reload",
        name="重载机器人插件",
        timeout=180.0,
        description="重新扫描并加载机器人插件目录。",
        input_schema={"type": "object", "properties": {}},
    )
    async def bridge_reload(self, **_kwargs: Any) -> Any:
        registry = self._registry
        router = self._router
        if registry is None or router is None:
            return Err(SdkError("桥接层尚未启动"))
        router.unregister_surfaces()
        try:
            status = await registry.reload()
        except Exception as exc:  # noqa: BLE001 — 重载失败要如实返回
            return Err(SdkError(f"reload failed: {type(exc).__name__}: {exc}"))
        stats = router.register_surfaces()
        return Ok({"status": status, "surfaces": stats})

    # ------------------------------------------------------------ 托管界面

    @ui.context(id="dashboard")
    async def dashboard_state(self) -> dict[str, Any]:
        """面板状态（``props.state``）。刻意不联网：只报缓存与当前注册表。"""
        registry = self._registry
        router = self._router
        if registry is None or router is None:
            return {"ok": False, "error": self._market_error or "桥接层尚未启动"}
        status = registry.status()
        market = self._market()
        market_payload: dict[str, Any] = {
            "enabled": market is not None,
            "error": self._market_error,
            "sources": [],
            "source_errors": dict(self._market_sources_errors),
        }
        if market is not None:
            try:
                market_payload["sources"] = await market.sources_status(refresh=False)
            except Exception as exc:  # noqa: BLE001 — 面板不许因为市场读不到而空白
                market_payload["error"] = f"{type(exc).__name__}: {exc}"
        return {
            "ok": True,
            "plugins_root": status.get("plugins_root", ""),
            "plugin_count": status.get("plugin_count", 0),
            "plugins": status.get("plugins", []),
            # 停用的插件不在加载列表里，面板要单独显示（否则用户以为插件消失了）
            "disabled_plugins": registry.disabled_plugins(),
            "stopped_plugins": registry.stopped_plugins(),
            "backends": status.get("backends", []),
            "backend_errors": status.get("backend_errors", {}),
            "errors": status.get("errors", {}),
            "tools": router.status().get("tools", []),
            "entries": router.status().get("entries", []),
            "market": market_payload,
            "recent_pushes": await self._recent_pushes(limit=10),
        }

    # ------------------------------------------------------------ 插件市场

    def _market(self) -> Any:
        """惰性构建市场对象（缺模块/未启用时返回 None，并把原因记在 ``_market_error``）。"""
        if self._market_obj is not None or self._market_error:
            return self._market_obj
        settings = self._settings.get("market")
        market_settings: dict[str, Any] = dict(settings) if isinstance(settings, Mapping) else {}
        if not bool(market_settings.get("enabled", True)):
            self._market_error = "[bridge.market].enabled = false，插件市场已停用"
            return None
        try:
            from .bridge.market import Market, build_sources
        except Exception as exc:  # noqa: BLE001 — 市场模块不可用不该拖垮桥接层
            self._market_error = f"市场模块不可用：{type(exc).__name__}: {exc}"
            return None
        sources, errors = build_sources(market_settings.get("sources") or None)
        host = self._host
        if host is None:
            self._market_error = "桥接层尚未启动"
            return None
        self._market_obj = Market(host, sources)
        self._market_sources_errors = dict(errors)
        return self._market_obj

    #: 市场搜索的对话期工具名（安装**不**暴露给模型：装代码必须是显式动作）。
    MARKET_TOOL_NAME = "bridge_market_search"

    def _register_market_tool(self) -> bool:
        market = self._market()
        if market is None:
            return False
        register = getattr(self, "register_llm_tool", None)
        if not callable(register):
            return False
        try:
            return bool(
                register(
                    name=self.MARKET_TOOL_NAME,
                    description=(
                        "在官方机器人插件索引里搜索 MaiBot / NoneBot2 / AstrBot / Koishi 插件（只读）。"
                        "返回 source + plugin_id，用户确认后可用 bridge_market_install 入口安装。"
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "关键词"},
                            "source": {"type": "string", "description": "maibot / nonebot / astrbot，留空为全部"},
                            "limit": {"type": "integer", "description": "返回条数上限，默认 10"},
                        },
                    },
                    handler=self._market_tool_handler,
                    # 对话期工具走 __llm_tool__ 入口（宿主默认 30s 档），必须自己先超时。
                    timeout=25.0,
                )
            )
        except Exception as exc:  # noqa: BLE001 — 注册失败只记录，不影响桥接层
            self._log("warning", f"[bot-bridge] 注册市场搜索工具失败：{type(exc).__name__}: {exc}")
            return False

    ADMIN_TOOL_NAME = "bridge_set_plugin"

    def _register_admin_tool(self) -> bool:
        """把「启停单个插件」暴露给对话期模型（删除**不**暴露：破坏性操作只留人工入口）。"""
        if self._registry is None or self._router is None:
            return False
        register = getattr(self, "register_llm_tool", None)
        if not callable(register):
            return False
        try:
            return bool(
                register(
                    name=self.ADMIN_TOOL_NAME,
                    description=(
                        "启动或停止单个机器人插件（MaiBot / NoneBot2 / AstrBot / Koishi 等）。"
                        "停用会记进配置，重启后不会自动拉起；用 bridge_list_plugins 拿插件名。"
                    ),
                    parameters={
                        "type": "object",
                        "properties": {
                            "plugin_id": {"type": "string", "description": "插件名：框架:插件 / 原生 id / 唯一后缀"},
                            "enabled": {"type": "boolean", "description": "true = 启动，false = 停止"},
                        },
                        "required": ["plugin_id"],
                    },
                    handler=self._admin_tool_handler,
                    timeout=25.0,
                )
            )
        except Exception as exc:  # noqa: BLE001 — 注册失败只记录，不影响桥接层
            self._log("warning", f"[bot-bridge] 注册插件启停工具失败：{type(exc).__name__}: {exc}")
            return False

    def _unregister_admin_tool(self) -> None:
        unregister = getattr(self, "unregister_llm_tool", None)
        if callable(unregister):
            try:
                unregister(self.ADMIN_TOOL_NAME)
            except Exception as exc:  # noqa: BLE001
                self._log("debug", f"[bot-bridge] 卸载插件启停工具失败：{exc!r}")

    async def _admin_tool_handler(self, plugin_id: str = "", enabled: bool = True, **_kwargs: Any) -> dict[str, Any]:
        """工具版启停：走与入口同一条路径，失败也返回结构化 dict（工具不能抛给模型）。"""
        result = await self.bridge_set_plugin(plugin_id=plugin_id, enabled=enabled)
        if hasattr(result, "is_ok") and result.is_ok():
            return dict(result.value)
        reason = str(getattr(result, "error", "") or "未知原因")
        return {"ok": False, "plugin_id": plugin_id, "enabled": bool(enabled), "error": reason}

    def _unregister_market_tool(self) -> None:
        unregister = getattr(self, "unregister_llm_tool", None)
        if callable(unregister):
            try:
                unregister(self.MARKET_TOOL_NAME)
            except Exception as exc:  # noqa: BLE001
                self._log("debug", f"[bot-bridge] 卸载市场搜索工具失败：{exc!r}")

    async def _market_tool_handler(
        self,
        query: str = "",
        source: str = "",
        limit: int = 10,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        return await self._market_search_payload(query, source, int(limit or 10))

    def _market_install_allowed(self) -> bool:
        settings = self._settings.get("market")
        if isinstance(settings, Mapping):
            return bool(settings.get("allow_install", True))
        return True

    async def _market_search_payload(
        self,
        query: str = "",
        source: str = "",
        limit: int = 20,
        refresh: bool = False,
    ) -> dict[str, Any]:
        market = self._market()
        if market is None:
            return {"ok": False, "error": self._market_error or "插件市场不可用", "count": 0, "results": []}
        counts: dict[str, int] = {}
        truncated: dict[str, int] = {}
        failures: dict[str, str] = {}
        try:
            reporter = getattr(market, "search_report", None)
            if callable(reporter):
                report = await reporter(query, source=source, limit=limit, refresh=refresh)
                entries = list(report.entries)
                counts = dict(report.counts)
                truncated = dict(report.truncated)
                failures = dict(report.failures)
            else:  # 兼容自定义市场实现
                entries = await market.search(query, source=source, limit=limit, refresh=refresh)
        except Exception as exc:  # noqa: BLE001 — 索引拉取失败要如实返回
            return {
                "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "count": 0,
                "results": [],
                "source_errors": dict(self._market_sources_errors),
            }
        results = [
            {
                "source": entry.source,
                "plugin_id": entry.plugin_id,
                "name": entry.name,
                "version": entry.version,
                "author": entry.author,
                "description": entry.description[:200],
                "repository": entry.repository,
                "install_kind": entry.install_kind,
            }
            for entry in entries
        ]
        return {
            "ok": True,
            "count": len(results),
            "results": results,
            # 每源命中数（截断前）：跨源结果按源轮流取，这组数字解释「为什么只有这几条」。
            "source_counts": counts,
            "source_truncated": truncated,
            "source_failures": failures,
            "source_errors": dict(self._market_sources_errors),
        }

    @ui.action(id="bridge_market_sources", label=tr("ui.action.probe", default="探活/预热市场源"), refresh_context=True)
    @plugin_entry(
        id="bridge_market_sources",
        name="机器人插件市场源",
        # 探活要拉三个索引（含镜像回退），必须显式放宽宿主看门狗。
        timeout=300.0,
        description="列出可用的机器人插件市场源（MaiBot / NoneBot2 / AstrBot / Koishi）及其可用状态。",
        input_schema={
            "type": "object",
            "properties": {
                "refresh": {
                    "type": "boolean",
                    "description": "true = 真的去拉一遍索引探活/预热（可能几十秒）；默认只看缓存，不联网",
                },
                "source": {
                    "type": "string",
                    "description": "只探这一个源（maibot / nonebot / astrbot）；留空 = 全部（可能超出宿主单次调用超时）",
                }
            },
        },
        llm_result_fields=["sources"],
    )
    async def bridge_market_sources(
        self,
        refresh: bool = False,
        source: str = "",
        **_kwargs: Any,
    ) -> Any:
        market = self._market()
        if market is None:
            return Err(SdkError(self._market_error or "插件市场不可用"))
        try:
            sources = await market.sources_status(refresh=bool(refresh), only=str(source or ""))
        except Exception as exc:  # noqa: BLE001
            return Err(SdkError(f"{type(exc).__name__}: {exc}"))
        return Ok({"sources": sources, "source_errors": dict(self._market_sources_errors)})

    @ui.action(id="bridge_market_search", label=tr("ui.action.search", default="搜索插件"), refresh_context=False)
    @plugin_entry(
        id="bridge_market_search",
        name="搜索机器人插件",
        timeout=300.0,
        description="在官方插件索引里搜索 MaiBot / NoneBot2 / AstrBot / Koishi 插件（只读，不安装；不限源时按源轮流返回，避免单个大商店吃光结果）。",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "关键词（命中 id / 名称 / 描述 / 作者）"},
                "source": {"type": "string", "description": "限定来源：maibot / nonebot / astrbot；留空 = 全部"},
                "limit": {"type": "integer", "description": "返回条数上限，默认 20"},
                "refresh": {"type": "boolean", "description": "忽略缓存，重新拉取索引"},
            },
        },
        llm_result_fields=["count", "results"],
    )
    async def bridge_market_search(
        self,
        query: str = "",
        source: str = "",
        limit: int = 20,
        refresh: bool = False,
        **_kwargs: Any,
    ) -> Any:
        payload = await self._market_search_payload(query, source, limit, bool(refresh))
        if not payload.get("ok", True):
            return Err(SdkError(f"市场搜索失败：{payload.get('error') or '未知原因'}"))
        counts = payload.get("source_counts") or {}
        spread = " / ".join(f"{name} {total}" for name, total in sorted(counts.items()) if total)
        payload["summary"] = f"命中 {payload.get('count', 0)} 个插件" + (f"（各源命中：{spread}）" if spread else "")
        return Ok(payload)

    @ui.action(
        id="bridge_market_install",
        label=tr("ui.action.install", default="安装选中插件"),
        tone="danger",
        refresh_context=True,
        confirm=tr("ui.action.installConfirm", default="确认把该插件装进 N.E.K.O.？"),
    )
    @plugin_entry(
        id="bridge_market_install",
        name="安装机器人插件",
        # 下载 + 解压 + 热重载，慢一点也要跑完。
        timeout=300.0,
        description="从市场源安装一个机器人插件到隔离的插件目录，并热重载。只接受官方索引里的条目。",
        input_schema={
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "来源：maibot / nonebot / astrbot"},
                "plugin_id": {"type": "string", "description": "索引里的插件 id（先用 bridge_market_search 查）"},
                "overwrite": {"type": "boolean", "description": "已存在时是否覆盖，默认 false"},
            },
            "required": ["source", "plugin_id"],
        },
        llm_result_fields=["summary", "ok", "target_dir"],
    )
    async def bridge_market_install(
        self,
        source: str = "",
        plugin_id: str = "",
        overwrite: bool = False,
        **_kwargs: Any,
    ) -> Any:
        market = self._market()
        if market is None:
            return Err(SdkError(self._market_error or "插件市场不可用"))
        if not self._market_install_allowed():
            return Err(
                SdkError(
                    "安装已被配置关闭：把 [bridge.market].allow_install 设为 true 后重试"
                    "（安装会把第三方代码写进插件目录）"
                )
            )
        if not str(source or "").strip() or not str(plugin_id or "").strip():
            return Err(SdkError("source 与 plugin_id 都是必填"))
        try:
            result = await market.install(source=str(source), plugin_id=str(plugin_id), overwrite=bool(overwrite))
        except Exception as exc:  # noqa: BLE001 — 安装失败要如实返回
            return Err(SdkError(f"安装失败：{type(exc).__name__}: {exc}"))
        if not result.ok:
            return Err(SdkError(f"安装失败（{source}:{plugin_id}）：{result.error}"))
        payload = result.to_payload()
        target_dir = str(payload.get("target_dir") or getattr(result, "target_dir", ""))
        reloaded = await self._reload_after_market(
            framework=str(source),
            plugin_id=str(plugin_id),
            target_dir=target_dir,
        )
        payload["reload"] = reloaded
        load_errors = reloaded.get("load_errors") if isinstance(reloaded, Mapping) else None
        if load_errors:
            # 装是装上了，但插件自身 import 失败（多半缺第三方依赖）——如实告诉用户。
            reason = next(iter(dict(load_errors).values()))
            payload["loaded"] = False
            payload["load_error"] = str(reason)
            payload["summary"] = (
                f"已安装 {source}:{plugin_id} → {target_dir}，但加载失败：{str(reason)[:200]}"
                "（插件自带依赖需要在 N.E.K.O. 运行时里可用，或把该插件加进 disabled_plugins）"
            )
        else:
            payload["loaded"] = bool(reloaded.get("loaded", True))
            payload["summary"] = f"已安装 {source}:{plugin_id} → {target_dir}"
        return Ok(payload)

    async def _reload_after_market(self, *, framework: str = "", plugin_id: str = "", target_dir: str = "") -> dict[str, Any]:
        """装完插件后重新扫描并重投影桥接面，并把这个插件是否真的加载起来了说清楚。"""
        registry = self._registry
        router = self._router
        if registry is None or router is None:
            return {"ok": False, "error": "桥接层尚未启动"}
        try:
            router.unregister_surfaces()
            status = await registry.reload()
            stats = router.register_surfaces()
        except Exception as exc:  # noqa: BLE001 — 重载失败要说清楚，但插件已经装到磁盘上了
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        name = Path(str(target_dir)).name if target_dir else ""
        loaded_keys = sorted(
            key for key in registry.plugins if (framework and key.startswith(f"{framework}:"))
        )
        errors = registry.plugin_errors
        related = {
            key: reason
            for key, reason in errors.items()
            if (name and (key.endswith(f":{name}") or name.endswith(key.split(":", 1)[-1])))
            or (plugin_id and key.endswith(f":{plugin_id}"))
        }
        return {
            "ok": True,
            "plugin_count": status.get("plugin_count"),
            "surfaces": stats,
            "loaded": bool([key for key in loaded_keys if name and key.endswith(f":{name}")]),
            "load_errors": related,
        }

    # -------------------------------------------------------------- 入站轮询

    async def _poll_loop(self) -> None:
        """可选的总线轮询：把 N.E.K.O. 的对话轮次喂给外来插件。

        这是 best-effort 通道（宿主不一定写 conversations 存储），所以默认关闭；
        真正稳定的入站是 ``bridge_run_command`` 入口与对话期工具。
        """
        host = self._host
        router = self._router
        if host is None or router is None:
            return
        interval = _positive_float(self._settings.get("poll_interval_seconds"), 10.0)
        while True:
            try:
                await asyncio.sleep(interval)
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — 轮询失败不能让任务死掉
                self._log("warning", f"[bot-bridge] 对话轮询失败：{type(exc).__name__}: {exc}")

    async def _poll_once(self) -> None:
        host = self._host
        router = self._router
        if host is None or router is None:
            return
        records = await host.recent_conversations(limit=20, since_ts=self._last_turn_ts or None)
        user_only = bool(self._settings.get("poll_user_only", True))
        for record in records:
            stamp = _record_timestamp(record)
            if stamp <= self._last_turn_ts:
                continue
            self._last_turn_ts = max(self._last_turn_ts, stamp)
            text = _record_user_text(record)
            if not text:
                continue
            if user_only and not _looks_like_user_turn(record):
                continue
            fingerprint = hashlib.sha1(f"{stamp}:{text}".encode("utf-8")).hexdigest()
            if fingerprint in self._seen_turns:
                continue
            self._seen_turns[fingerprint] = stamp
            if len(self._seen_turns) > 512:
                cutoff = sorted(self._seen_turns.values())[len(self._seen_turns) // 2]
                self._seen_turns = {key: value for key, value in self._seen_turns.items() if value >= cutoff}
            await router.handle_text(CommandContext(text=text, source="conversation_poll", channel="neko"))


def _compact_push(record: Mapping[str, Any]) -> dict[str, Any]:
    """把总线记录压成状态查询里够用的几个字段。"""
    preview = _record_user_text(record) or _text_of(record.get("parts"))
    return {
        "source": str(record.get("source") or record.get("plugin_id") or ""),
        "kind": str(record.get("kind") or record.get("type") or ""),
        "timestamp": record.get("timestamp") or record.get("ts"),
        "preview": preview[:200],
    }


def _positive_float(value: object, default: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return number if number > 0 else default


def _record_timestamp(record: Mapping[str, Any]) -> float:
    for key in ("timestamp", "ts", "time"):
        value = record.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                text = value.replace("Z", "+00:00")
                try:
                    import datetime as _dt

                    return _dt.datetime.fromisoformat(text).timestamp()
                except ValueError:
                    continue
    return time.time()


def _record_user_text(record: Mapping[str, Any]) -> str:
    for key in ("content", "text", "plain_text", "message"):
        value = record.get(key)
        text = _text_of(value)
        if text:
            return text
    metadata = record.get("metadata")
    if isinstance(metadata, Mapping):
        for key in ("content", "text", "plain_text", "message"):
            text = _text_of(metadata.get(key))
            if text:
                return text
    return ""


def _text_of(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        for key in ("text", "content", "plain_text", "message"):
            nested = value.get(key)
            if isinstance(nested, str) and nested.strip():
                return nested.strip()
    return ""


def _looks_like_user_turn(record: Mapping[str, Any]) -> bool:
    markers: list[str] = []
    for key in ("type", "kind", "source", "role", "speaker"):
        value = record.get(key)
        if isinstance(value, str):
            markers.append(value.lower())
    metadata = record.get("metadata")
    if isinstance(metadata, Mapping):
        for key in ("turn_type", "role", "speaker", "source", "type"):
            value = metadata.get(key)
            if isinstance(value, str):
                markers.append(value.lower())
    if not markers:
        return False
    return any("user" in marker or "human" in marker or marker in {"chat", "user_message", "user_turn"} for marker in markers)


__all__ = ["BotBridgePlugin", "DEFAULT_SETTINGS"]
