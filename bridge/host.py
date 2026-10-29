"""把 N.E.K.O. 的宿主能力包成 :class:`HostServices`。

第一层：**N.E.K.O. 的能力面**（推送消息、大模型、存储、配置、插件互调、消息查询）。
第二层（各框架兼容层）：把外来插件的能力调用翻译成这一层的调用。

这里只做映射，不做业务判断；映射不了的必须抛结构化错误，绝不假装成功。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .contracts import HostServices
from .errors import UnsupportedCapabilityError

_LOG_LEVELS = frozenset({"debug", "info", "warning", "error"})

# push_message 的合法取值（见 plugin/sdk/shared/core/push_message_schema.py）。
VISIBILITY_VALUES = frozenset({"chat", "hud"})
AI_BEHAVIOR_VALUES = frozenset({"respond", "read", "blind"})


def _as_plain(value: object) -> object:
    """把总线记录对象转成普通 dict / list，方便外来插件消费。"""
    dump = getattr(value, "dump", None)
    if callable(dump):
        try:
            return _as_plain(dump())
        except Exception:  # noqa: BLE001 — dump 失败时退回属性遍历
            pass
    if isinstance(value, Mapping):
        return {str(key): _as_plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_as_plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return {
            str(key): _as_plain(item)
            for key, item in vars(value).items()
            if not str(key).startswith("_")
        }
    return str(value)


class NEKOHostServices(HostServices):
    """``HostServices`` 的 N.E.K.O. 实现。

    ``plugin`` 是适配器本身（:class:`NekoAdapterPlugin` 实例），桥接层借它访问
    宿主能力：``push_message`` / ``config`` / ``plugins`` / ``ctx.bus`` / 路径解析。
    """

    def __init__(self, plugin: object, settings: Mapping[str, Any]) -> None:
        self._plugin = plugin
        # 持有**同一份** settings 字典：插件在启动后还会合并/更新它（例如启停状态），
        # 拷贝一份会让注册表读到过期的值（踩过：刚启动的注册表看不到 disabled_plugins）。
        self._settings: dict[str, Any] = settings if isinstance(settings, dict) else dict(settings)
        self._logger = getattr(plugin, "logger", None)
        self._forward_logs = bool(self._settings.get("forward_plugin_logs", True))
        self._max_push_chars = int(self._settings.get("max_push_chars", 4000) or 4000)
        self._default_visibility = tuple(
            str(item)
            for item in (self._settings.get("default_visibility") or ["chat"])
            if str(item) in VISIBILITY_VALUES
        )
        default_behavior = str(self._settings.get("default_ai_behavior") or "respond")
        self._default_ai_behavior = (
            default_behavior if default_behavior in AI_BEHAVIOR_VALUES else "respond"
        )
        # 必须在解析 plugins_root 之前就绪：data_dir() 会用到这两把锁。
        self._dir_locks: dict[str, threading.Lock] = {}
        self._dir_lock_guard = threading.Lock()
        self._plugins_root = self._resolve_plugins_root()
        # 模型网关的 usage id（plugin.toml 的 [plugin.models.<usage_id>] 声明）。
        self._llm_usage_id = str(self._settings.get("llm_usage_id") or "chat")
        self._embedding_usage_id = str(self._settings.get("embedding_usage_id") or "embedding")
        self._llm_lock = threading.Lock()
        self._llm_client: object | None = None

    # ------------------------------------------------------------------ 基础

    @property
    def plugin_id(self) -> str:
        return str(getattr(self._plugin, "plugin_id", "bot_bridge"))

    def sync_settings(self, settings: Mapping[str, Any]) -> None:
        """把最新的 settings 内容同步进宿主能力层（幂等）。

        正常路径下构造时就持有同一份 dict；这个方法留给「插件在构造之后才合并配置」
        的场景（例如启停状态合并），保证注册表一定读到最新值。
        """
        if settings is self._settings:
            return
        self._settings = settings if isinstance(settings, dict) else dict(settings)

    @property
    def settings(self) -> Mapping[str, Any]:
        return self._settings

    def capability_enabled(self, capability: str) -> bool:
        """``[bridge.capabilities]`` 开关；每次现读 settings，sync_settings 推来的改动立即生效。"""
        caps = self._settings.get("capabilities")
        return not (isinstance(caps, Mapping) and caps.get(capability, True) is False)

    def require_capability(self, capability: str, api: str) -> None:
        """开关关闭时抛结构化的 ``unsupported_capability``，不再映射到 N.E.K.O.。"""
        if not self.capability_enabled(capability):
            raise UnsupportedCapabilityError(api, f"已在 [bridge.capabilities].{capability} 关闭")

    def log(self, level: str, message: str, *, source: str = "") -> None:
        if not self._forward_logs:
            return
        logger = self._logger
        if logger is None:
            return
        normalized = level if level in _LOG_LEVELS else "info"
        text = f"[bot-bridge:{source}] {message}" if source else f"[bot-bridge] {message}"
        try:
            getattr(logger, normalized)(text)
        except Exception:  # noqa: BLE001 — 日志失败绝不能影响插件逻辑
            pass

    # ------------------------------------------------------------------ 路径

    def _resolve_plugins_root(self) -> Path:
        """外来插件根目录。

        配置为空时用 ``<桥接层数据目录>/plugins`` —— 注意不能用
        ``data_dir("plugins")``，那会把 "plugins" 当成插件 id，得到
        ``<数据目录>/plugins/data``（曾经踩过这个坑）。
        """
        configured = str(self._settings.get("plugins_root") or "").strip()
        if configured:
            return Path(configured).expanduser()
        return self._data_root() / "plugins"

    @property
    def plugins_root(self) -> Path:
        """外来机器人插件的根目录（``[bridge].plugins_root`` 或数据目录下的 plugins/）。"""
        return self._plugins_root

    def _plugin_lock(self, plugin_id: str) -> threading.Lock:
        with self._dir_lock_guard:
            lock = self._dir_locks.get(plugin_id)
            if lock is None:
                lock = threading.Lock()
                self._dir_locks[plugin_id] = lock
            return lock

    def _ensure_dir(self, base: Path, plugin_id: str, leaf: str = "") -> Path:
        root = base / plugin_id
        target = root / leaf if leaf else root
        with self._plugin_lock(str(target)):
            target.mkdir(parents=True, exist_ok=True)
        return target

    def data_dir(self, plugin_id: str = "") -> Path:
        pid = plugin_id or self.plugin_id
        base = self._data_root()
        if not plugin_id:
            # 桥接层自身的数据目录由宿主提供（<插件数据目录>）。
            return base
        return self._ensure_dir(base, pid, "data")

    def cache_dir(self, plugin_id: str = "") -> Path:
        pid = plugin_id or self.plugin_id
        base = self._data_root()
        if not plugin_id:
            return self._ensure_dir(base, pid, "cache")
        return self._ensure_dir(base, pid, "cache")

    @property
    def data_root(self) -> Path:
        """本插件自己的数据根目录（测试/迁移时可用 ``data_root`` 设置重定向）。

        兼容层内部存状态请用这里，不要用 SDK 的 ``data_path()``：后者永远指向宿主
        真实目录，离线测试会读写真机状态（踩过：测试把 disabled_plugins 写进真机）。
        """
        return self._data_root()

    def _data_root(self) -> Path:
        override = str(self._settings.get("data_root") or "").strip()
        if override:
            # 显式覆盖（离线测试 / 迁移数据目录）：绝不能落到宿主真实目录里。
            root = Path(override).expanduser()
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.log("warning", f"data_root 不可用（{root}）：{exc}", source=self.plugin_id)
            else:
                return root
        # 宿主 SDK 的隔离约定：设了 NEKO_STORAGE_SELECTED_ROOT 就把数据根挪过去。
        # 离线测试靠它避免读写真机目录（踩过：测试把 disabled_plugins 写进真机）。
        selected = str(os.environ.get("NEKO_STORAGE_SELECTED_ROOT") or "").strip()
        if selected:
            root = Path(selected).expanduser() / "plugins" / self.plugin_id / "data"
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                self.log("warning", f"NEKO_STORAGE_SELECTED_ROOT 不可用（{root}）：{exc}", source=self.plugin_id)
            else:
                return root
        resolver = getattr(self._plugin, "data_path", None)
        if callable(resolver):
            try:
                return Path(resolver())
            except Exception as exc:  # noqa: BLE001 — 退回宿主临时目录
                self.log("warning", f"data_path() 解析失败，退回临时目录：{exc!r}", source=self.plugin_id)
        return Path.cwd() / "bot_bridge_data"

    # ------------------------------------------------------- 对话推送（出站）

    async def send_chat(
        self,
        text: str,
        *,
        source: str,
        visibility: Sequence[str] | None = None,
        ai_behavior: str = "",
        priority: int = 5,
        metadata: Mapping[str, Any] | None = None,
    ) -> bool:
        self.require_capability("send", "send.chat")
        body = str(text or "")
        if not body.strip():
            return False
        if len(body) > self._max_push_chars:
            body = body[: self._max_push_chars] + "…（已截断）"
        parts = [{"type": "text", "text": body}]
        resolved_visibility = list(visibility) if visibility is not None else list(self._default_visibility)
        resolved_visibility = [item for item in resolved_visibility if item in VISIBILITY_VALUES]
        behavior = ai_behavior if ai_behavior in AI_BEHAVIOR_VALUES else self._default_ai_behavior
        payload_metadata = dict(metadata or {})
        payload_metadata.setdefault("bridge_source", source)
        try:
            receipt = self._plugin.push_message(  # type: ignore[attr-defined]
                source=source or self.plugin_id,
                visibility=resolved_visibility,
                ai_behavior=behavior,
                parts=parts,
                priority=int(priority),
                metadata=payload_metadata,
            )
        except Exception as exc:  # noqa: BLE001 — 投递失败要如实上报给外来插件
            self.log("error", f"push_message 失败：{type(exc).__name__}: {exc}", source=source)
            return False
        submitted = self._receipt_ok(receipt)
        # 每次都留一条宿主侧证据：外来插件有没有真的把话递进 N.E.K.O. 对话。
        self.log(
            "info" if submitted else "warning",
            f"send_chat <- {source or self.plugin_id}: {len(body)} 字，"
            f"visibility={resolved_visibility or '[]'} ai_behavior={behavior} -> submitted={submitted}",
            source=source,
        )
        return submitted

    @staticmethod
    def _receipt_ok(receipt: object) -> bool:
        if receipt is None:
            return False
        if isinstance(receipt, bool):
            return receipt
        if isinstance(receipt, Mapping):
            for key in ("submitted", "success", "ok", "sent"):
                if key in receipt:
                    return bool(receipt[key])
            reason = receipt.get("reason") or receipt.get("error")
            return not bool(reason)
        return bool(receipt)

    # ------------------------------------------------------------ 大模型

    async def _llm_client_or_raise(self, usage_id: str) -> object:
        self.require_capability("llm", "llm")
        client = self._llm_client
        if client is not None:
            return client
        with self._llm_lock:
            if self._llm_client is not None:
                return self._llm_client
            ctx = getattr(self._plugin, "ctx", None)
            models = getattr(ctx, "models", None)
            getter = getattr(models, "get_client", None)
            if not callable(getter):
                raise UnsupportedCapabilityError(
                    "llm.generate",
                    "当前宿主没有暴露 ctx.models.get_client；请在 plugin.toml 里声明 [plugin.models.<usage_id>] 并绑定模型槽",
                )
            try:
                import asyncio

                client = getter()
                if asyncio.iscoroutine(client):
                    client = await client
            except Exception as exc:  # noqa: BLE001 — 如实抛出，由能力层转成结构化失败
                raise UnsupportedCapabilityError(
                    "llm.generate",
                    f"获取 N.E.K.O. 模型客户端失败：{type(exc).__name__}: {exc}",
                ) from exc
            self._llm_client = client
            self._llm_usage_id = usage_id or self._llm_usage_id
            return client

    @staticmethod
    def _normalize_messages(
        prompt: str | Sequence[Mapping[str, Any]],
        system: str,
    ) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        if isinstance(prompt, str):
            messages.append({"role": "user", "content": prompt})
            return messages
        for item in prompt:
            if not isinstance(item, Mapping):
                continue
            role = str(item.get("role") or "user")
            content = item.get("content")
            messages.append({"role": role, "content": _as_plain(content) if not isinstance(content, str) else content})
        return messages

    async def llm_generate(
        self,
        prompt: str | Sequence[Mapping[str, Any]],
        *,
        model: str = "",
        system: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: Sequence[Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        usage_id = str(model or "").strip() or self._llm_usage_id
        try:
            client = await self._llm_client_or_raise(usage_id)
        except UnsupportedCapabilityError as exc:
            return {"success": False, "response": "", "reasoning": "", "model": "", "error": exc.detail, "code": exc.code}
        payload: dict[str, Any] = {
            "model": usage_id,
            "messages": self._normalize_messages(prompt, system),
        }
        if temperature is not None:
            payload["temperature"] = float(temperature)
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
        if tools:
            payload["tools"] = [dict(item) for item in tools]
        try:
            completion = await client.chat.completions.create(**payload)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 — 模型网关的错误要原样回给外来插件
            detail = f"{type(exc).__name__}: {exc}"
            self.log("warning", f"llm.generate 失败：{detail}", source=self.plugin_id)
            return {
                "success": False,
                "response": "",
                "reasoning": "",
                "model": usage_id,
                "error": detail,
                "code": self._error_code(exc),
            }
        return self._completion_to_dict(completion, usage_id)

    @staticmethod
    def _error_code(exc: BaseException) -> str:
        status = getattr(exc, "status_code", None)
        body = getattr(exc, "body", None)
        if isinstance(body, Mapping):
            error = body.get("error")
            if isinstance(error, Mapping) and error.get("code"):
                return str(error["code"])
            if body.get("code"):
                return str(body["code"])
        return f"http_{status}" if status else "llm_call_failed"

    @staticmethod
    def _completion_to_dict(completion: object, usage_id: str) -> dict[str, Any]:
        choices = getattr(completion, "choices", None) or []
        text = ""
        reasoning = ""
        tool_calls: list[dict[str, Any]] = []
        if choices:
            message = getattr(choices[0], "message", None)
            text = str(getattr(message, "content", "") or "")
            reasoning = str(getattr(message, "reasoning_content", "") or "")
            for call in getattr(message, "tool_calls", None) or []:
                function = getattr(call, "function", None)
                tool_calls.append(
                    {
                        "id": str(getattr(call, "id", "") or ""),
                        "name": str(getattr(function, "name", "") or ""),
                        "arguments": str(getattr(function, "arguments", "") or ""),
                    }
                )
        model_name = str(getattr(completion, "model", "") or usage_id)
        return {
            "success": True,
            "response": text,
            "reasoning": reasoning,
            "model": model_name,
            "tool_calls": tool_calls,
        }

    async def llm_embed(self, texts: Sequence[str]) -> list[list[float]]:
        client = await self._llm_client_or_raise(self._embedding_usage_id)
        try:
            response = await client.embeddings.create(  # type: ignore[attr-defined]
                model=self._embedding_usage_id,
                input=list(texts),
            )
        except Exception as exc:  # noqa: BLE001 — 向量化失败就是能力不可用
            raise UnsupportedCapabilityError(
                "llm.embed",
                f"N.E.K.O. 模型网关拒绝了向量化请求：{type(exc).__name__}: {exc}",
            ) from exc
        return [list(item.embedding) for item in getattr(response, "data", []) or []]

    # ------------------------------------------------------- 插件互调 / 查询

    async def call_plugin_entry(
        self,
        plugin_id: str,
        entry_id: str,
        params: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        self.require_capability("plugin_call", "api.call")
        plugins = getattr(self._plugin, "plugins", None)
        caller = getattr(plugins, "call_entry", None)
        if not callable(caller):
            raise UnsupportedCapabilityError(
                "api.call",
                "当前宿主没有提供 plugins.call_entry，无法调用其它 N.E.K.O. 插件",
            )
        reference = f"{plugin_id}:{entry_id}"
        try:
            result = caller(reference, dict(params or {}), timeout=float(timeout))
            import asyncio

            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                result = await result
        except Exception as exc:  # noqa: BLE001 — 调用失败原样上报
            return {"success": False, "error": f"{type(exc).__name__}: {exc}", "reference": reference}
        # SDK 返回 Ok/Err（slots dataclass，_as_plain 认不出），先拆包再转普通对象
        if callable(getattr(result, "is_err", None)):
            if result.is_err():
                return {"success": False, "error": str(result.error), "reference": reference}
            result = result.value
        plain = _as_plain(result)
        if isinstance(plain, Mapping):
            return dict(plain)
        return {"success": True, "result": plain}

    async def list_plugins(self) -> list[dict[str, Any]]:
        self.require_capability("plugin_call", "component.get_all_plugins")
        plugins = getattr(self._plugin, "plugins", None)
        lister = getattr(plugins, "list", None)
        if not callable(lister):
            return []
        try:
            items = lister()
            import asyncio

            if asyncio.iscoroutine(items) or hasattr(items, "__await__"):
                items = await items
        except Exception as exc:  # noqa: BLE001
            self.log("warning", f"list_plugins 失败：{type(exc).__name__}: {exc}", source=self.plugin_id)
            return []
        if callable(getattr(items, "is_err", None)):
            if items.is_err():
                self.log("warning", f"list_plugins 失败：{items.error}", source=self.plugin_id)
                return []
            items = items.value
        plain = _as_plain(items)
        if isinstance(plain, list):
            return [dict(item) for item in plain if isinstance(item, Mapping)]
        if isinstance(plain, Mapping):
            nested = plain.get("plugins")
            if isinstance(nested, list):
                return [dict(item) for item in nested if isinstance(item, Mapping)]
        return []

    def _bus(self) -> object | None:
        ctx = getattr(self._plugin, "ctx", None)
        return getattr(ctx, "bus", None)

    async def recent_messages(self, limit: int = 20) -> list[dict[str, Any]]:
        return await self._bus_records("messages", limit=limit)

    async def recent_conversations(
        self,
        limit: int = 20,
        since_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        """最近的对话轮次（用户在 N.E.K.O. 里说的话），桥接层的入站来源。"""
        kwargs: dict[str, Any] = {"max_count": int(limit)}
        if since_ts is not None:
            kwargs["since_ts"] = float(since_ts)
        return await self._bus_records("conversations", limit=limit, extra=kwargs)

    async def _bus_records(
        self,
        store: str,
        *,
        limit: int,
        extra: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        self.require_capability("chat_query", f"chat.{store}")
        bus = self._bus()
        namespace = getattr(bus, store, None) if bus is not None else None
        getter = getattr(namespace, "get", None)
        if not callable(getter):
            return []
        kwargs: dict[str, Any] = {"max_count": int(limit)}
        if extra:
            kwargs.update(extra)
        try:
            records = getter(**kwargs)
            import asyncio

            if asyncio.iscoroutine(records) or hasattr(records, "__await__"):
                records = await records
        except Exception as exc:  # noqa: BLE001 — 总线读失败不该拖垮插件
            self.log("warning", f"bus.{store}.get 失败：{type(exc).__name__}: {exc}", source=self.plugin_id)
            return []
        plain = _as_plain(records)
        if isinstance(plain, Mapping):
            nested = plain.get(store) or plain.get("items") or plain.get("records")
            plain = nested if nested is not None else [plain]
        if not isinstance(plain, list):
            return []
        result: list[dict[str, Any]] = []
        for item in plain:
            if isinstance(item, Mapping):
                result.append(dict(item))
        return result

    # -------------------------------------------------------------- 配置

    def _config_path(self, plugin_id: str) -> Path:
        """外来插件配置统一存 JSON（TOML 只读不写，避免手写序列化器）。"""
        return self.data_dir(plugin_id) / "config.json"

    def _legacy_config_path(self, plugin_id: str) -> Path:
        return self.data_dir(plugin_id) / "config.toml"

    async def plugin_config(self, plugin_id: str) -> dict[str, Any]:
        self.require_capability("config", "config.get")
        path = self._config_path(plugin_id)
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 — 配置坏了要能看见
                self.log("warning", f"读取 {path} 失败：{type(exc).__name__}: {exc}", source=plugin_id)
                return {}
            return dict(data) if isinstance(data, dict) else {}
        legacy = self._legacy_config_path(plugin_id)
        if not legacy.is_file():
            return {}
        try:
            import tomllib
        except ImportError:  # pragma: no cover — Python 3.11 起自带 tomllib
            import tomli as tomllib  # type: ignore[no-redef]
        try:
            with legacy.open("rb") as stream:
                data = tomllib.load(stream)
        except Exception as exc:  # noqa: BLE001 — 配置坏了要能看见
            self.log("warning", f"读取 {legacy} 失败：{type(exc).__name__}: {exc}", source=plugin_id)
            return {}
        return dict(data) if isinstance(data, dict) else {}

    async def save_plugin_config(self, plugin_id: str, data: Mapping[str, Any]) -> bool:
        self.require_capability("config", "config.set")
        path = self._config_path(plugin_id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(_as_plain(data), ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 — 写失败要如实返回
            self.log("error", f"写入 {path} 失败：{type(exc).__name__}: {exc}", source=plugin_id)
            return False
        return True


__all__ = ["AI_BEHAVIOR_VALUES", "NEKOHostServices", "VISIBILITY_VALUES"]
