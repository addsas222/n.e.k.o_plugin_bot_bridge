#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""真机验证：把示例机器人插件装进正在运行的 N.E.K.O.，验证整条转接链路。

前置：N.E.K.O. 在跑，bot_bridge 已装载（``POST /plugins/refresh`` 能被认出来）。

做的事：
  1. 把 ``tests/live/plugins/<框架>/<插件>`` 拷进 bot_bridge 的插件根目录
     （默认 ``<插件目录>/data/plugins/``，与 bridge/host.py::_resolve_plugins_root 一致）；
  2. ``POST /plugins/refresh`` + ``POST /plugin/bot_bridge/reload``；
  3. 通过对话期工具回调调 ``bridge_list_plugins``，断言三个框架的示例插件都加载了；
  4. 对每个插件的命令调 ``bridge_run_command``（**点名** ``plugin=``，避免被别的框架的
     catch-all 抢走），断言 handled=true；
  5. 断言麦麦示例插件 ``send.text`` 的投递回执（插件返回值里的 delivered）；
  6. 走官方运行接口 ``POST /runs`` → ``/runs/{id}/export`` 调 ``bridge_status``；
  7. 宿主侧证据（只认本轮新增的日志行）：
     * 插件日志 ``send_chat <- … submitted=True``；
     * 宿主日志 ``proactive bridge forwarded`` 与 ``proactive_delivery``（**仅供参考**：
       这两行取决于宿主 proactive 管道/角色会话状态，不由桥接层决定）。

用法：
    python -X utf8 tests\\live_check.py [--base http://127.0.0.1:48916] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLUGIN_DIR = HERE.parent
LIVE_FIXTURES = HERE / "live" / "plugins"
sys.path.insert(0, str(HERE))

import sample_text  # noqa: E402 — tests/ 下的共用小工具

DEFAULT_PLUGIN_ROOT = Path.home() / "AppData" / "Local" / "N.E.K.O" / "plugins" / "bot_bridge"
#: 与 bridge/host.py::_resolve_plugins_root 保持一致：<插件目录>/data/plugins
DEFAULT_PLUGINS_ROOT = DEFAULT_PLUGIN_ROOT / "data" / "plugins"
REPORT = HERE / "live-report.txt"

RESULTS: list[tuple[bool, str, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    RESULTS.append((bool(ok), label, detail))
    line = f"[{'PASS' if ok else 'FAIL'}] {label}"
    if detail:
        line += f"  -> {detail}"
    print(line, flush=True)


def request(base: str, method: str, path: str, payload: dict[str, object] | None = None, timeout: float = 90.0):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        body = response.read().decode("utf-8", "replace")
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"raw": body}


def install_fixtures(plugins_root: Path) -> list[str]:
    installed: list[str] = []
    for framework_dir in sorted(LIVE_FIXTURES.iterdir()):
        if not framework_dir.is_dir():
            continue
        for plugin in sorted(framework_dir.iterdir()):
            destination = plugins_root / framework_dir.name / plugin.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if plugin.is_dir():
                shutil.copytree(plugin, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(plugin, destination)
            installed.append(f"{framework_dir.name}/{plugin.name}")
    return installed


def call_tool(base: str, tool: str, arguments: dict[str, object]) -> dict[str, object]:
    return request(
        base,
        "POST",
        f"/api/llm-tools/callback/bot_bridge/{tool}",
        {"name": tool, "arguments": arguments, "call_id": f"live-{int(time.time() * 1000)}"},
    )


def run_entry(base: str, entry_id: str, args: dict[str, object], *, timeout: float = 30.0) -> dict[str, object]:
    """走官方运行接口调一个入口：``POST /runs`` → 轮询 → ``GET /runs/{id}/export``。

    只把 ``POST /runs`` 当**触发器**：本 build 里 ``result_refs`` 指向的 blob 取不到（404），
    而结构化结果在 export 的 ``label == "trigger_response"`` 条目里 —— 外层是宿主信封
    ``{success, code, data, message, error, time, trace_id}``，插件返回值在 ``data`` 内。
    两条路都不可用时如实记进 ``error``，不算插件失败。
    """
    created = request(base, "POST", "/runs", {"plugin_id": "bot_bridge", "entry_id": entry_id, "args": args})
    run_id = str(created.get("run_id") or "")
    if not run_id:
        return {"status": "", "result": {}, "error": f"no run_id: {created}"}
    record: dict[str, object] = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = request(base, "GET", f"/runs/{run_id}")
        if str(record.get("status") or "") in ("succeeded", "failed", "error", "cancelled"):
            break
        time.sleep(0.5)
    status = str(record.get("status") or "")
    try:
        exported = request(base, "GET", f"/runs/{run_id}/export")
    except OSError as exc:  # noqa: BLE001 — export 不可用不该让整轮验证崩掉
        return {"status": status, "result": {}, "error": f"export failed: {exc}"}
    payload: dict[str, object] = {}
    for item in exported.get("items") or []:
        if not isinstance(item, dict) or item.get("label") != "trigger_response":
            continue
        body = item.get("json")
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            payload = body["data"]
        break
    return {"status": status, "result": payload, "error": record.get("error")}


def _candidate_logs(log_dir: Path) -> list[Path]:
    if not log_dir.is_dir():
        return []
    return sorted(log_dir.glob("**/*.log"), key=lambda path: path.stat().st_mtime, reverse=True)[:12]


def _snapshot_logs(log_dir: Path) -> dict[str, int]:
    """记录候选日志文件当前大小 —— 之后的证据只认这个位置之后写下的字节。"""
    return {str(path): path.stat().st_size for path in _candidate_logs(log_dir)}


def _grep_new_lines(
    log_dir: Path, needle: str, marks: dict[str, int], limit: int = 5, *, since: str = ""
) -> list[str]:
    """只在本轮触发之后新增的日志内容里找 ``needle``。

    ``since``（``YYYY-MM-DD HH:MM:SS``）是**本轮开始时刻**，用于兜住日志轮转：
    轮转后新出现的文件在 ``marks`` 里没有偏移量，直接读全文会把今天早些时候的旧行
    当成"本轮证据"（真机上就是这么被坑过：宿主日志按日期分文件，我只 grep 了旧文件，
    误判成「14:34 之后不再转发」）。给新文件加时间戳门槛即可。

    比时间戳过滤可靠：宿主日志写本地时区（本机为 UTC+8），插件进程时钟可能是 UTC，
    直接比时间戳会把本轮证据一起过滤掉；文件偏移不受时区影响，也不会撞上历史遗留行。
    """
    hits: list[str] = []
    for path in _candidate_logs(log_dir):
        start = marks.get(str(path), 0)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        known = str(path) in marks
        if size < start:  # 被截断 / 轮转
            start = 0
            known = False
        try:
            with path.open("r", encoding="utf-8", errors="replace") as stream:
                stream.seek(start)
                for line in stream:
                    if needle not in line:
                        continue
                    # 没见过的新文件（轮转产物）：只认本轮开始之后写下的行 ——
                    # 否则整份文件都会被当成"本轮证据"（会把旧行算进来）。
                    if not known and since and line[:19] < since:
                        continue
                    hits.append(f"{path.name}: {line.strip()}")
                    if len(hits) >= limit:
                        return hits
        except OSError:
            continue
    return hits


def _find_delivered(payload: object) -> bool | None:
    if isinstance(payload, dict):
        if "delivered" in payload and isinstance(payload["delivered"], bool):
            return payload["delivered"]
        for value in payload.values():
            found = _find_delivered(value)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for item in payload:
            found = _find_delivered(item)
            if found is not None:
                return found
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:48916")
    parser.add_argument("--plugins-root", default=str(DEFAULT_PLUGINS_ROOT))
    parser.add_argument(
        "--log-dir",
        default=str(Path.home() / "AppData" / "Local" / "N.E.K.O" / "logs" / "plugin"),
        help="bot_bridge 的插件日志目录（push 回执在这里）",
    )
    parser.add_argument(
        "--host-log-dir",
        default=str(Path.home() / "AppData" / "Local" / "N.E.K.O" / "logs"),
        help="宿主会话日志目录（proactive bridge 转发证据在这里）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只铺夹具，不调用宿主")
    args = parser.parse_args()
    base = args.base.rstrip("/")

    installed = install_fixtures(Path(args.plugins_root))
    check(bool(installed), "示例插件已铺到插件根目录", ", ".join(installed))

    if args.dry_run:
        return finish()

    try:
        refreshed = request(base, "POST", "/plugins/refresh")
        check(bool(refreshed.get("success", refreshed.get("added") is not None)), "plugins/refresh 成功", str(refreshed)[:160])
        reloaded = request(base, "POST", "/plugin/bot_bridge/reload")
        check(bool(reloaded.get("success")), "bot_bridge 重载成功", str(reloaded)[:160])
    except urllib.error.URLError as exc:
        check(False, "连接 N.E.K.O. 插件服务", f"{exc}")
        return finish()

    time.sleep(2.0)
    listing = call_tool(base, "bridge_list_plugins", {})
    plugins = listing.get("output", {}).get("plugins", []) if isinstance(listing.get("output"), dict) else []
    check(bool(plugins), "bridge_list_plugins 报出插件", f"{len(plugins)} 个")
    frameworks = sorted({str(item.get("framework")) for item in plugins})
    check(bool(frameworks), "已加载的框架", ", ".join(frameworks))

    # 市场搜索（只读；需要网络，降级时如实报出原因，不参与失败判定）。
    market = call_tool(base, "bridge_market_search", {"query": "插件", "limit": 3})
    market_out = market.get("output") if isinstance(market.get("output"), dict) else {}
    if market_out.get("ok") is False:
        check(True, "市场搜索（只读，仅供参考）", f"市场不可用 / 网络受限：{str(market_out.get('error'))[:160]}")
    else:
        check(
            True,
            "市场搜索（只读，仅供参考）",
            f"命中 {market_out.get('count')} 条；源错误={market_out.get('source_errors')}",
        )

    # 触发前给日志拍偏移快照：后面的证据必须是这轮新写的行。
    session_started = time.strftime("%Y-%m-%d %H:%M:%S")
    plugin_log_marks = _snapshot_logs(Path(args.log_dir))
    host_log_marks = _snapshot_logs(Path(args.host_log_dir))

    for item in plugins:
        key = str(item.get("plugin"))
        for command in item.get("commands", [])[:1]:
            matched = False
            detail = ""
            output: dict[str, object] = {}
            for text in sample_text.candidates(command):
                result = call_tool(base, "bridge_run_command", {"text": text, "plugin": key})
                output = result.get("output") if isinstance(result.get("output"), dict) else {}
                detail = f"text={text!r} -> {json.dumps(output, ensure_ascii=False)[:200]}"
                if output.get("handled") or output.get("replies"):
                    matched = True
                    break
            check(matched, f"{key} 命令被处理（点名 plugin={key}）", detail)
            if key.endswith("hello_neko"):
                delivered = _find_delivered(output)
                check(delivered is True, "麦麦插件 send.text 投递成功（插件侧回执）", detail)

    # 官方运行接口：POST /runs 触发，结果取 /runs/{id}/export。
    status_run = run_entry(base, "bridge_status", {})
    payload = status_run.get("result") if isinstance(status_run.get("result"), dict) else {}
    check(
        status_run.get("status") == "succeeded" and payload.get("plugin_count") is not None,
        "POST /runs → /export 调 bridge_status 成功",
        f"status={status_run.get('status')} keys={list(payload)[:8]}",
    )
    pushes = payload.get("recent_pushes") if isinstance(payload.get("recent_pushes"), list) else []
    check(
        True,
        "bridge_status 的 recent_pushes（宿主 messages 存储，仅供参考）",
        json.dumps(pushes[:2], ensure_ascii=False)[:200] if pushes else "宿主 messages 存储里还没有可见记录",
    )

    # 证据一：桥接层为每次外来推送留的 send_chat 回执（只认本轮新增行）。
    hits = _grep_new_lines(Path(args.log_dir), "send_chat <-", plugin_log_marks, since=session_started)
    accepted = [line for line in hits if "submitted=True" in line]
    check(
        bool(accepted),
        "插件日志里出现本轮推送回执（send_chat submitted=True，本轮新增行）",
        accepted[-1][:220] if accepted else f"本轮新增 send_chat 行数={len(hits)}",
    )

    # 证据二：宿主把本轮推送转发给了角色（同样只认本轮新增行，避免撞上历史行）。
    # 宿主日志有刷盘/批处理延迟：给这条证据一个**有界等待**（判定标准不变，只是别抢跑）。
    forwarded: list[str] = []
    deadline = time.time() + 20.0
    while time.time() < deadline:
        forwarded = _grep_new_lines(
            Path(args.host_log_dir), "proactive bridge forwarded: plugin=bot_bridge", host_log_marks, since=session_started
        )
        if forwarded:
            break
        time.sleep(1.0)
    # 只作提示，**不计失败**：这一行由宿主的 proactive 管道决定——本安装上用户把
    # proactive_controller 打开后（日志 14:56 `False -> True`），宿主就不再转发外来推送，
    # 但插件侧 send_chat 仍然拿到 accepted 回执（上面的硬证据）。转发与否是宿主的策略，
    # 桥接层能负责的是「把消息交出去并确认收到」。
    check(
        True,
        "宿主侧转发（仅供参考，不计失败）",
        (forwarded[-1][:200] if forwarded else "本轮宿主 proactive 管道没有转发（可能与 proactive_controller/会话状态有关）"),
    )
    # 投放批次是二阶信号：它取决于角色当前有没有活跃会话、投放是否被合并，
    # 所以给一个有限的等待窗口，仍没有就只作提示。
    # 投放批次：**只作参考，不计失败**。这条受宿主状态影响：用户活跃时
    # `prepare_proactive_delivery: user active before claim` 会一直推迟 claim，release 自然不出现
    # （用户正好在聊天时跑本脚本就会假失败）。所以这里给 45s 窗口，并把「推迟信号」记进结果，
    # 而不是当作判据；真正的硬证据是插件侧的 send_chat 回执。
    released: list[str] = []
    deferred: list[str] = []
    deadline = time.time() + 45.0
    while time.time() < deadline:
        released = _grep_new_lines(
            Path(args.host_log_dir), "main_logic.proactive_delivery", host_log_marks, since=session_started
        )
        if released:
            break
        deferred = _grep_new_lines(
            Path(args.host_log_dir),
            "user active before claim",
            host_log_marks,
            since=session_started,
        )
        if deferred:
            break
        time.sleep(1.0)
    if released:
        detail = released[-1][:200]
    elif deferred:
        detail = f"宿主当前用户活跃，投放被推迟（不是失败）：{deferred[-1][:140]}"
    else:
        detail = "45s 内没有 release batch（角色可能没有活跃会话）；转发回执才是硬证据"
    check(True, "投放批次（仅供参考，不计失败）", detail)
    return finish()


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
    raise SystemExit(main())
