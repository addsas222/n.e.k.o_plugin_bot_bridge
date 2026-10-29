"""Koishi 兼容层（``FrameworkBackend`` 实现，Node 子进程宿主）。

Koishi 插件是 JS/TS（npm 包，``koishi-plugin-*``），不能像 NoneBot/AstrBot 那样
在 Python 进程内加载。本兼容层把一个 **Node 宿主进程** 当作插件本体：

1. **识别**：目录里有 ``package.json``，且名字以 ``koishi-plugin`` 开头、keywords
   含 ``koishi``、或 peerDependencies/dependencies/devDependencies 声明 ``koishi``。
2. **物化运行时**：把 ``shims/koishi/`` 的宿主脚本与 koishi/schemastery 桩写进
   ``<plugins_root>/koishi/``（插件目录的祖先——Node 的裸导入解析靠祖先链，
   这样插件的 ``import { Context } from 'koishi'`` 与自身的 node_modules 依赖都能解析）。
3. **协议**：与 Node 宿主之间走 stdin/stdout 的 JSON-lines（见
   ``shims/koishi/koishi-host.mjs`` 头部注释）。命令调用 / 事件分发 /
   start/stop 都是往那里发请求，等回包。

能力上限（ponytail 级，如实）：覆盖 command/action/session.send/logger 与
ready/dispose 生命周期；依赖 koishi 内部服务（database/console/model）的插件
会在运行时拿到清晰错误。任务选项默认值等 PI 语义由市场/用户显式传入。
"""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ..contracts import (
    CommandContext,
    CommandResult,
    CommandSpec,
    EventSpec,
    LoadedPlugin,
    PluginManifest,
    ToolSpec,
)
from ..errors import BackendUnavailableError, PluginLoadError, UnsupportedCapabilityError

__all__ = ["BACKEND", "KoishiBackend"]

SHIM_DIR = Path(__file__).resolve().parents[2] / "shims" / "koishi"
_HOST_SCRIPT = "koishi-host.mjs"
_SHIM_NODE_MODULES = SHIM_DIR / "node_modules"

#: koishi 插件的身份标记（满足任一即认为是 koishi 插件）。
_PACKAGE_MARKERS = ("koishi-plugin", "koishi")


def _read_package_json(directory: Path) -> dict[str, Any] | None:
    try:
        raw = (directory / "package.json").read_bytes()
    except OSError:
        return None
    try:
        data = json.loads(raw.decode("utf-8-sig", errors="replace"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _is_koishi_package(package: dict[str, Any]) -> bool:
    name = str(package.get("name") or "")
    if name.startswith(_PACKAGE_MARKERS):
        return True
    keywords = package.get("keywords")
    if isinstance(keywords, list) and "koishi" in [str(k).lower() for k in keywords]:
        return True
    for section in ("peerDependencies", "dependencies", "devDependencies"):
        deps = package.get(section)
        if isinstance(deps, dict) and any(str(k).startswith("koishi") for k in deps):
            return True
    return False


def _resolve_entry(directory: Path, package: dict[str, Any]) -> str:
    exported = package.get("exports")
    if isinstance(exported, dict):
        dot = exported.get(".")
        if isinstance(dot, dict):
            candidate = dot.get("import") or dot.get("require") or dot.get("default")
            if isinstance(candidate, str):
                return candidate
        if isinstance(dot, str):
            return dot
    return str(package.get("main") or package.get("module") or "index.js")


class _NodeProcess:
    """一个 Node 宿主进程：JSON-lines 请求/回包 + 事件流。

    ``_pending`` 存的是**回包回调**：同步路径用 threading.Event 兜底，
    异步路径用 ``loop.call_soon_threadsafe`` 完成 future。读线程只管按
    call_id 调回调，不关心调用方是谁。
    """

    def __init__(self, argv: list[str], cwd: Path, *, ready_timeout: float = 30.0) -> None:
        self.proc = subprocess.Popen(
            argv,
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._lock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, Any] = {}
        self.ready: dict[str, Any] | None = None
        self.ready_error: str = ""
        self._ready_event = threading.Event()
        self._closed = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.host_services: Any = None
        self._deferred_calls: list[dict[str, Any]] = []
        self._reader = threading.Thread(
            target=self._read_loop, daemon=True, name="koishi-node-reader"
        )
        self._reader.start()
        if not self._ready_event.wait(ready_timeout):
            self.terminate()
            raise PluginLoadError(
                f"Koishi 宿主 {ready_timeout:.0f} 秒内没有就绪（看插件日志定位："
                "通常是插件 import 了未安装的依赖，或 Node 版本过老）。"
            )
        if self.ready_error:
            self.terminate()
            raise PluginLoadError(f"Koishi 宿主启动失败：{self.ready_error}")
        if self.proc.poll() is not None:
            raise PluginLoadError("Koishi 宿主进程在就绪前退出")

    # ---------------------------------------------------------------- 线程侧

    def _send_line(self, obj: dict[str, Any]) -> None:
        if self._closed or self.proc.stdin is None:
            return
        try:
            self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
        except (OSError, ValueError):
            pass

    def _read_loop(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if kind == "ready":
                manifest = msg.get("manifest")
                self.ready = manifest if isinstance(manifest, dict) else {}
                self._ready_event.set()
            elif kind == "error":
                self.ready_error = str(msg.get("message") or "未知错误")
                self._ready_event.set()
            elif kind == "result":
                self._resolve(int(msg.get("call_id") or 0), msg)
            elif kind == "call":
                self._handle_host_call(msg)

    def _resolve(self, call_id: int, payload: dict[str, Any]) -> None:
        with self._lock:
            resolve = self._pending.pop(call_id, None)
        if resolve is not None:
            try:
                resolve(payload)
            except Exception:  # noqa: BLE001 - 回调异常不能拖垮读线程
                pass

    def _handle_host_call(self, msg: dict[str, Any]) -> None:
        """插件反向借用宿主能力（send_chat / llm_generate）。"""
        call_id = int(msg.get("call_id") or 0)
        method = str(msg.get("method") or "")
        params = msg.get("params") or {}
        # startup 的一次性循环关掉后仍挂在这里，按「未就绪」处理
        if self.loop is None or self.loop.is_closed():
            self._deferred_calls.append(
                {"call_id": call_id, "method": method, "params": params}
            )
            self._send_line(
                {
                    "type": "call_result",
                    "call_id": call_id,
                    "ok": False,
                    "error": "宿主事件循环未就绪，能力调用被拒绝",
                }
            )
            return
        try:
            self.loop.call_soon_threadsafe(self._schedule_host_call, call_id, method, params)
        except RuntimeError:  # 与循环关闭赛跑：回个失败，别让读线程死掉
            self._send_line(
                {"type": "call_result", "call_id": call_id, "ok": False, "error": "宿主事件循环已关闭"}
            )

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        for item in list(self._deferred_calls):
            self._deferred_calls.remove(item)
            self._schedule_host_call(item["call_id"], item["method"], item["params"])

    def _schedule_host_call(self, call_id: int, method: str, params: dict[str, Any]) -> None:
        if self.loop is None:
            return
        self.loop.create_task(self._run_host_call(call_id, method, params))

    async def _run_host_call(self, call_id: int, method: str, params: dict[str, Any]) -> None:
        host = self.host_services
        ok, value, error = True, None, ""
        try:
            if method == "send_chat":
                text = str(params.get("text") or "")
                value = bool(await host.send_chat(text, source="koishi")) if text else False
            elif method == "llm_generate":
                value = await host.llm_generate(str(params.get("prompt") or ""))
            else:
                raise UnsupportedMethodError(method)
        except Exception as exc:  # noqa: BLE001 - 错误要回给插件，不能留在异步任务里
            ok, value, error = False, None, f"{type(exc).__name__}: {exc}"
        self._send_line(
            {"type": "call_result", "call_id": call_id, "ok": ok, "value": value, "error": error}
        )

    # ---------------------------------------------------------------- 控制侧

    def _next_call_id(self) -> int:
        with self._lock:
            self._next_id += 1
            return self._next_id

    def request(self, obj: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
        """同步请求（加载期的握手用）；call_id 注入请求体供宿主关联回包。"""
        call_id = self._next_call_id()
        done = threading.Event()
        sink: dict[str, Any] = {}

        def _resolve(payload: dict[str, Any]) -> None:
            sink["payload"] = payload
            done.set()

        with self._lock:
            self._pending[call_id] = _resolve
        self._send_line({**obj, "call_id": call_id})
        if not done.wait(timeout):
            with self._lock:
                self._pending.pop(call_id, None)
            raise TimeoutError(f"Koishi 宿主 {timeout:.0f} 秒内没有回应：{obj.get('type')}")
        payload = sink.get("payload") or {}
        if payload.get("ok") is False and payload.get("error"):
            raise PluginLoadError(str(payload["error"]))
        return payload

    async def post(self, obj: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        """异步请求：返回回包载荷（``ok=false`` 时抛 PluginLoadError）。"""
        loop = asyncio.get_running_loop()
        if self.loop is None or self.loop.is_closed():
            self.attach_loop(loop)
        call_id = self._next_call_id()
        future: asyncio.Future = loop.create_future()

        def _resolve(payload: dict[str, Any]) -> None:
            if not future.done():
                loop.call_soon_threadsafe(_settle, future, payload)

        with self._lock:
            self._pending[call_id] = _resolve
        self._send_line({**obj, "call_id": call_id})
        return await asyncio.wait_for(future, timeout)

    def notify(self, obj: dict[str, Any]) -> None:
        self._send_line(obj)

    def attach_services(self, host_services: Any) -> None:
        self.host_services = host_services

    def terminate(self) -> None:
        self._closed = True
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass


class UnsupportedMethodError(RuntimeError):
    pass


def _settle(future: asyncio.Future, payload: dict[str, Any]) -> None:
    if future.done():
        return
    if payload.get("ok") is False and payload.get("error"):
        future.set_exception(PluginLoadError(str(payload["error"])))
    else:
        future.set_result(payload)


class KoishiBackend:
    name = "koishi"
    display_name = "Koishi"
    requires: tuple[str, ...] = ()

    def __init__(self) -> None:
        self._runtime_lock = threading.Lock()

    # ------------------------------------------------------------------ 识别

    def detect(self, path: Path) -> PluginManifest | None:
        """判断 ``path`` 是不是 Koishi 插件目录；不是则返回 ``None``（绝不抛异常）。"""
        try:
            return self._detect(Path(path))
        except Exception:  # noqa: BLE001 — detect 的契约就是「便宜 + 永不抛」
            return None

    def _detect(self, path: Path) -> PluginManifest | None:
        if not path.is_dir():
            return None
        package = _read_package_json(path)
        if not package or not _is_koishi_package(package):
            return None
        name = str(package.get("name") or path.name)
        return PluginManifest(
            path=path,
            plugin_id=name,
            framework="koishi",
            name=name,
            version=str(package.get("version") or ""),
            description=str(package.get("description") or ""),
            entry=_resolve_entry(path, package),
            extra={"package": package},
        )

    # ------------------------------------------------------------------ 加载

    def load(self, manifest: PluginManifest, host: Any) -> LoadedPlugin:
        if shutil.which("node") is None:
            raise BackendUnavailableError(
                self.name, "需要 Node.js 运行时（node 不在 PATH）：Koishi 插件是 JS/TS，经 Node 子进程执行"
            )
        plugin_dir = Path(manifest.path)
        framework_root = plugin_dir.parent  # 桩必须写在插件目录的祖先链上
        owned = self._is_owned_framework_root(framework_root, host)
        self._materialize_runtime(framework_root, manifest, host, overwrite=owned)

        package = manifest.extra.get("package") or _read_package_json(plugin_dir) or {}
        self._install_npm_dependencies_if_needed(plugin_dir, package, host, owned=owned)

        argv = [
            shutil.which("node") or "node",
            str(framework_root / _HOST_SCRIPT),
            "--plugin-dir",
            str(plugin_dir),
            "--config",
            json.dumps({"user_path": str(host.data_dir(manifest.plugin_id))}, ensure_ascii=False),
        ]
        try:
            proc = _NodeProcess(argv, cwd=plugin_dir, ready_timeout=30.0)
        except PluginLoadError:
            raise
        except OSError as exc:
            raise PluginLoadError(f"Node 宿主进程启动失败：{exc}") from exc

        manifest_data = proc.ready or {}
        loaded = LoadedPlugin(manifest=manifest, instance=proc)
        for name, item in (manifest_data.get("commands") or {}).items():
            aliases = tuple(str(a) for a in (item.get("aliases") or []))
            loaded.commands.append(
                CommandSpec(
                    id=name,
                    name=name,
                    description=str(item.get("description") or ""),
                    patterns=(name, *aliases),
                    usage=str(item.get("description") or ""),
                )
            )
            # koishi 没有「LLM 工具」概念：与 NoneBot 同款，把命令也暴露成工具。
            loaded.tools.append(
                ToolSpec(
                    name=name,
                    description=str(item.get("description") or f"执行 koishi 命令 {name}"),
                    parameters={
                        "type": "object",
                        "properties": {
                            "text": {"type": "string", "description": "命令参数（跟在命令名后面）"},
                        },
                        "required": [],
                    },
                    origin=f"koishi:{manifest.plugin_id}",
                )
            )
        for item in manifest_data.get("events") or []:
            loaded.events.append(
                EventSpec(event_type=str(item.get("event_type") or "message"), description="koishi 事件监听")
            )
        proc.attach_services(host)
        loaded.handle_message = self._make_message_handler(proc, loaded)
        loaded.call_tool = self._make_tool_handler(proc, loaded)
        loaded.call_api = self._make_api_handler(loaded)
        loaded.dispatch_event = self._make_event_dispatcher(proc)
        loaded.native = proc
        loaded.started = False
        loaded.loaded_at = time.time()
        return loaded

    # ------------------------------------------------------- 能力回调

    def _make_message_handler(self, proc: _NodeProcess, loaded: LoadedPlugin):
        async def handle_message(context: CommandContext) -> CommandResult:
            result = CommandResult(handled=False, plugin_id=loaded.plugin_id, framework=loaded.framework)
            text = context.text.strip()
            if not text:
                return result
            stripped = text.lstrip("/\\")
            tokens = stripped.split(None, 1)
            if not tokens:
                return result
            head = tokens[0]
            rest = tokens[1] if len(tokens) > 1 else ""
            command = next(
                (
                    spec
                    for spec in loaded.commands
                    if head == spec.id or head in spec.patterns
                ),
                None,
            )
            if command is None:
                return result
            call_id = await proc.post( {
                "type": "invoke_command",
                "name": command.id,
                "argv": {"name": command.id, "options": {}, "args": [rest] if rest else []},
                "session": {
                    "content": text,
                    "username": context.user_name or "neko_user",
                    "user_id": context.user_id or "neko_user",
                    "channel_id": context.group_id or context.channel or "neko",
                    "is_direct": not context.is_group,
                },
            })
            replies = [str(item) for item in (call_id.get("replies") or []) if str(item)]
            return CommandResult(
                handled=True,
                plugin_id=loaded.plugin_id,
                framework=loaded.framework,
                command=command.id,
                replies=replies,
                data={"raw": call_id},
                error=str(call_id.get("error") or ""),
            )

        return handle_message

    def _make_tool_handler(self, proc: _NodeProcess, loaded: LoadedPlugin):
        async def call_tool(name: str, arguments: dict[str, Any]) -> Any:
            text = str(arguments.get("text") or name)
            call_id = await proc.post( {
                "type": "invoke_command",
                "name": name,
                "argv": {"name": name, "options": {}, "args": [text] if text else []},
                "session": {
                    "content": f"{name} {text}".strip(),
                    "username": "neko_llm",
                    "user_id": "neko_llm",
                    "channel_id": "neko",
                    "is_direct": True,
                },
            })
            if call_id.get("error"):
                raise PluginLoadError(str(call_id["error"]))
            return {"replies": call_id.get("replies") or []}

        return call_tool

    def _make_api_handler(self, loaded: LoadedPlugin):
        async def call_api(name: str, arguments: dict[str, Any]) -> Any:
            raise UnsupportedCapabilityError(
                f"koishi 插件 {loaded.plugin_id} 未暴露可跨插件调用的 API（{name}）"
            )

        return call_api

    def _make_event_dispatcher(self, proc: _NodeProcess):
        async def dispatch_event(event_type: str, payload: dict[str, Any]) -> None:
            proc.notify({"type": "event", "event": event_type, "payload": payload or {}})

        return dispatch_event


    # ------------------------------------------------------- 运行时物化

    def _is_owned_framework_root(self, framework_root: Path, host: Any) -> bool:
        """框架根是否归桥管：``plugins_root`` 本身或其子目录。

        覆盖两种物化位置 —— 分类布局 ``<plugins_root>/koishi/<插件>/``（框架根 =
        ``<plugins_root>/koishi``）与扁平布局 ``<plugins_root>/koishi-plugin-x/``
        （框架根 = ``plugins_root`` 本身，README「扁平布局」同样受支持）。
        归桥管 → 物化可覆写、可代跑 npm install；不归（``framework_dirs`` 指到
        外部目录，可能是真实的 Koishi 实例）→ 只补缺、不覆写、不装依赖。
        """
        plugins_root = getattr(host, "plugins_root", "") or "."
        try:
            resolved_root = Path(plugins_root).resolve(strict=False)
            resolved_framework_root = Path(framework_root).resolve(strict=False)
        except OSError:
            return False
        return (
            resolved_framework_root == resolved_root or resolved_root in resolved_framework_root.parents
        )

    def _materialize_runtime(
        self, framework_root: Path, manifest: PluginManifest, host: Any, *, overwrite: bool = True
    ) -> None:
        """把 koishi 宿主脚本与桩 node_modules 写进框架根（幂等，内容不变不重写）。

        ``overwrite=False``（外部插件源目录）：只补缺失的文件；已存在的文件一律保留
        ——外部目录可能是一座真实的 Koishi 实例，覆写它的 ``node_modules/koishi``
        等于拿我们的协议桩换掉人家的完整运行时。宿主脚本/桩与现文件内容不同时，
        记一条 warning 说明桥用的是什么。
        """
        with self._runtime_lock:
            framework_root.mkdir(parents=True, exist_ok=True)
            host_script = framework_root / _HOST_SCRIPT
            source_script = SHIM_DIR / _HOST_SCRIPT
            if not host_script.is_file():
                shutil.copy2(source_script, host_script)
            elif not overwrite and host_script.read_bytes() != source_script.read_bytes():
                host.log(
                    "warning",
                    f"外部插件源目录 {framework_root} 里已有 {_HOST_SCRIPT}（与桥自带版本不同），按原文件启动",
                    source="koishi",
                )
            elif host_script.read_bytes() != source_script.read_bytes():
                shutil.copy2(source_script, host_script)  # 桥自管目录：shim 更新要能传播
            for source in (_SHIM_NODE_MODULES / "koishi", _SHIM_NODE_MODULES / "schemastery"):
                if not source.is_dir():
                    continue
                target_base = framework_root / "node_modules" / source.name
                for file in source.rglob("*"):
                    if file.is_file():
                        target = target_base / file.relative_to(source)
                        if target.is_file():
                            if target.read_bytes() == file.read_bytes():
                                continue  # 内容相同就不重写（幂等）
                            if overwrite:
                                # 桥自管目录：桩更新要能传播（升级 shim 后旧桩必须被替换）
                                shutil.copy2(file, target)
                            else:
                                host.log(
                                    "warning",
                                    f"外部插件源目录里已存在 {target.name}（与桥自带桩不同），保留原文件："
                                    f"{target}",
                                    source="koishi",
                                )
                            continue
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(file, target)

    def _install_npm_dependencies_if_needed(
        self, plugin_dir: Path, package: dict[str, Any], host: Any, *, owned: bool = True
    ) -> None:
        settings = getattr(host, "settings", None) or {}
        if not bool(settings.get("koishi_npm_install", True)):
            return
        if not owned:
            # 外部插件源目录（framework_dirs 指到别处）不代跑 npm install：
            # 那可能是真实的 Koishi 实例，往里写 node_modules / package-lock.json 属于越权写入；
            # 依赖解析会回落到插件目录祖先链上的 node_modules（真实安装就在那里）。
            host.log(
                "info",
                f"koishi 插件 {package.get('name')} 在外部插件源目录，跳过 npm install"
                "（依赖由目录祖先链上的 node_modules 解析；确需安装请在该目录手动执行）",
                source="koishi",
            )
            return
        dependencies = package.get("dependencies")
        if not isinstance(dependencies, dict) or not dependencies:
            return
        node_modules = plugin_dir / "node_modules"
        if node_modules.is_dir() and any(node_modules.iterdir()):
            return
        npm = shutil.which("npm")
        if npm is None:
            host.log(
                "warning",
                f"koishi 插件 {package.get('name')} 声明了依赖但 npm 不在 PATH，"
                "跳过依赖安装（插件可能 import 失败）",
                source="koishi",
            )
            return
        host.log("info", f"koishi 插件 {package.get('name')} 正在安装依赖…", source="koishi")
        try:
            subprocess.run(
                [npm, "install", "--omit=dev", "--omit=peer", "--ignore-scripts", "--no-audit", "--no-fund"],
                cwd=str(plugin_dir),
                capture_output=True,
                timeout=600,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            host.log("warning", f"npm install 失败（继续尝试加载）：{exc}", source="koishi")

    # ------------------------------------------------------------------ 生命周期

    async def start(self, plugin: LoadedPlugin) -> None:
        proc: _NodeProcess = plugin.native
        if proc.loop is None:
            proc.attach_loop(asyncio.get_running_loop())
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, lambda: proc.request({"type": "start"}, timeout=30.0))
        plugin.started = True

    async def stop(self, plugin: LoadedPlugin) -> None:
        proc: _NodeProcess = plugin.native
        if proc.proc.poll() is not None:
            plugin.started = False
            return
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, lambda: proc.request({"type": "stop"}, timeout=10.0))
        except (TimeoutError, OSError, RuntimeError):
            pass
        try:
            proc.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.terminate()
        plugin.started = False


BACKEND = KoishiBackend()
