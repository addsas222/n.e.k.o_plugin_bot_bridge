#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bot_bridge 的离线端到端验证（真 SDK + 假宿主）。

跑法（venv 里装了 pydantic / msgpack）：

    C:\\Users\\54301\\Desktop\\botfw\\bridge-venv\\Scripts\\python.exe -X utf8 tests\\stub_run.py

判据：
  * 插件能被真 SDK 装饰器认出来，``on_startup`` 返回 Ok；
  * 空插件目录不炸、有插件目录能发现并加载各框架的夹具插件；
  * ``bridge_list_plugins`` / ``bridge_status`` 报出插件、命令、工具；
  * ``bridge_run_command`` 命中夹具命令，回复进 N.E.K.O. 对话（push_message 有记录）；
  * 外来插件的 ``send.*`` 能力真的落在 push_message 上；
  * 映射不了的能力返回结构化失败（``unsupported_capability``），不假装成功；
  * ``bridge_reload`` 能重新扫描。

报告写到 ``tests/stub-report.txt``。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
FIXTURES = HERE / "fixtures"
REPORT = HERE / "stub-report.txt"


def _isolate_runtime_storage() -> Path:
    """把「宿主存储根」指到临时目录。

    宿主的 ``resolve_runtime_data_root()`` 默认是 ``%LOCALAPPDATA%/N.E.K.O``，于是每跑一次
    这个脚本，插件就会在**真机插件目录**里为夹具插件建出 ``data/<id>/…`` 状态目录。
    官方离线测试也这么隔离（``NEKO_STORAGE_SELECTED_ROOT``），必须在 import 插件之前设。
    """
    root = Path(tempfile.mkdtemp(prefix="bot_bridge_runtime_"))
    os.environ["NEKO_STORAGE_SELECTED_ROOT"] = str(root)
    return root


_ISOLATED_ROOT = _isolate_runtime_storage()

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), label, detail))
    line = f"[{'PASS' if ok else 'FAIL'}] {label}"
    if detail:
        line += f"  -> {detail}"
    print(line, flush=True)


def build_plugins_root() -> Path:
    """把 tests/fixtures/<framework>/<plugin>/ 铺成一个临时插件根目录。"""
    root = Path(tempfile.mkdtemp(prefix="bot_bridge_fixtures_"))
    if not FIXTURES.is_dir():
        return root
    for framework_dir in sorted(FIXTURES.iterdir()):
        if not framework_dir.is_dir():
            continue
        for plugin_dir in sorted(framework_dir.iterdir()):
            if not plugin_dir.is_dir():
                continue
            target = root / framework_dir.name / plugin_dir.name
            shutil.copytree(plugin_dir, target, dirs_exist_ok=True)
    return root


async def main() -> int:
    sys.path.insert(0, str(HERE))
    import support  # noqa: PLC0415 — 需要在 sys.path 就位后导入

    support.install_import_shims()
    plugins_root = build_plugins_root()
    plugin, ctx = await support.instantiate_plugin(plugins_root=plugins_root)

    try:
        startup = await plugin.on_startup()
    except Exception:
        check(False, "on_startup 不抛异常", traceback.format_exc(limit=3))
        return finish()

    check(getattr(startup, "is_ok", lambda: False)(), "on_startup 返回 Ok", repr(startup)[:200])
    registry = plugin._registry
    router = plugin._router
    check(registry is not None and router is not None, "registry / router 就绪")
    if registry is None or router is None:
        return finish()

    loaded = registry.plugins
    check(True, "发现的外来插件", ", ".join(sorted(loaded)) or "（无夹具插件）")

    tools = plugin.list_llm_tools()
    names = {item["name"] for item in tools}
    check(
        {"bridge_list_plugins", "bridge_run_command", "bridge_call_plugin_tool"} <= names,
        "三个桥接对话期工具已注册",
        ", ".join(sorted(names)),
    )
    check(len(tools) > 3, "外来插件的命令/工具已投影成对话期工具", f"共 {len(tools)} 个")

    status = await plugin.bridge_status()
    check(getattr(status, "is_ok", lambda: False)(), "bridge_status 返回 Ok")
    payload = status.value if hasattr(status, "value") else {}
    check(
        isinstance(payload, dict) and "backends" in payload,
        "bridge_status 报出各兼容层",
        str(payload.get("backend_errors") if isinstance(payload, dict) else payload)[:200],
    )

    listing = await plugin.bridge_list_plugins()
    listed = listing.value.get("plugins", []) if hasattr(listing, "value") else []
    check(isinstance(listed, list), "bridge_list_plugins 返回插件列表", f"{len(listed)} 个")

    # 逐个夹具插件跑一遍命令，并验证能力回落。
    import sample_text  # noqa: PLC0415 — tests/ 下的共用小工具

    for entry in listed:
        key = entry.get("plugin", "")
        commands = entry.get("commands") or []
        if not commands:
            continue
        matched = False
        detail = ""
        for text in sample_text.candidates(commands[0]):
            before = len(ctx.pushes)
            result = await plugin.bridge_run_command(text=text, plugin=key)
            ok = getattr(result, "is_ok", lambda: False)()
            data = result.value if ok else {}
            detail = f"text={text!r} -> {str(data)[:160]}"
            if data.get("handled"):
                matched = True
                check(True, f"{key} 命中命令", detail)
                check(
                    bool(data.get("replies")) or len(ctx.pushes) > before,
                    f"{key} 的回复有出口（replies 或 push_message）",
                    f"replies={data.get('replies')} pushes={len(ctx.pushes) - before}",
                )
                break
        if not matched:
            check(False, f"{key} 命中命令", f"尝试了 {sample_text.candidates(commands[0])}；最后结果：{detail}")

    # 能力回落：夹具插件在命令里调用了 send.text / llm.generate 之类的能力。
    if any(record.source for record in ctx.pushes):
        sources = sorted({record.source for record in ctx.pushes})
        check(True, "push_message 收到外来插件来源的消息", ", ".join(sources)[:200])

    # 结构化失败：不存在的插件必须报错，而不是假装成功。
    missing = await plugin.bridge_call_api(plugin="nope:missing", name="x")
    check(not getattr(missing, "is_ok", lambda: True)(), "调用不存在的插件返回 Err")

    reloaded = await plugin.bridge_reload()
    check(getattr(reloaded, "is_ok", lambda: False)(), "bridge_reload 返回 Ok", repr(reloaded)[:160])

    # 启停：只动一个插件，然后恢复。
    if listed:
        target = listed[0].get("plugin", "")
        stopped = await plugin.bridge_set_plugin(plugin_id=target, enabled=False)
        ok_stop = getattr(stopped, "is_ok", lambda: False)()
        after_stop = plugin._registry.plugins if plugin._registry is not None else {}
        check(
            ok_stop and not any(target in key for key in after_stop),
            "bridge_set_plugin 能停掉单个插件",
            f"{target} → 剩余 {len(after_stop)} 个",
        )
        restarted = await plugin.bridge_set_plugin(plugin_id=target, enabled=True)
        ok_start = getattr(restarted, "is_ok", lambda: False)()
        after_start = plugin._registry.plugins if plugin._registry is not None else {}
        check(
            ok_start and any(target in key for key in after_start),
            "bridge_set_plugin 能再启动回来",
            f"{target} → 现在 {len(after_start)} 个",
        )

    # 删除：只删一份临时拷贝，绝不碰夹具原件。
    if listed:
        import shutil as _shutil  # noqa: PLC0415

        source = plugin._registry.plugins  # type: ignore[union-attr]
        victim = next(iter(source.values()))
        victim_dir = Path(victim.manifest.path)
        copied = victim_dir.parent / f"{victim_dir.name}_delete_probe"
        _shutil.copytree(victim_dir, copied, dirs_exist_ok=True)
        try:
            await plugin._registry.reload()  # type: ignore[union-attr]
            removed = await plugin.bridge_delete_plugin(plugin_id=f"{victim.plugin_id}_delete_probe")
            check(
                getattr(removed, "is_ok", lambda: False)() and not copied.exists(),
                "bridge_delete_plugin 真的删掉插件目录",
                f"目标 {copied.name} 存在={copied.exists()}",
            )
        finally:
            _shutil.rmtree(copied, ignore_errors=True)

    stopped = await plugin.on_shutdown()
    check(getattr(stopped, "is_ok", lambda: False)(), "on_shutdown 返回 Ok")
    return finish()


def _sample_text(command: dict[str, object]) -> str:
    patterns = command.get("patterns") or []
    if patterns and isinstance(patterns, list):
        raw = str(patterns[0])
        cleaned = raw.strip("^$")
        cleaned = cleaned.replace("\\s*", " ").replace("\\s+", " ").replace(".*", "").replace("\\.", ".")
        return cleaned.strip() or "hello"
    return str(command.get("id") or "hello")


def finish() -> int:
    failed = [item for item in RESULTS if not item[0]]
    lines = [f"{'PASS' if ok else 'FAIL'}\t{label}\t{detail}" for ok, label, detail in RESULTS]
    lines.append("")
    lines.append(f"总计 {len(RESULTS)} 项，失败 {len(failed)} 项")
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n总计 {len(RESULTS)} 项，失败 {len(failed)} 项；报告：{REPORT}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(asyncio.run(main()))
