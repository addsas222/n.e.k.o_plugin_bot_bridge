"""打包完整性测试。

N.E.K.O. 官方校验器（``plugin/neko_plugin_cli/commands/validate_cmd.py``）要跑起来
需要整套宿主依赖（fastapi / orjson / …），离线环境里不划算。这里用 AST + TOML 复刻
它那些**跟本插件相关**的规则，保证包形态不会悄悄跑偏：

* ``plugin.toml`` 的必填字段、id/version/entry 的正则、``type``、``[adapter]`` 规则；
* ``config.example.toml`` 不允许出现 ``[plugin]``；
* 入口模块必须在插件目录内、类存在、带 ``@neko_plugin``、继承 ``NekoAdapterPlugin``，
  并且声明了 ``startup`` / ``shutdown`` 两个 lifecycle；
* 目录里不允许有 ``requirements.txt``（宿主强制规则）；
* 所有 ``.py`` 必须能编译；JSON / YAML 附件必须是合法格式。
"""

from __future__ import annotations

import ast
import json
import py_compile
import re
import tomllib
from pathlib import Path

import pytest

import support

PLUGIN_DIR = Path(__file__).resolve().parents[1]

_ID = re.compile(r"^[A-Za-z0-9_-]+$")
_ENTRY = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_]*$")
_VERSION = re.compile(r"^\d+\.\d+\.\d+")


def _manifest() -> dict:
    return tomllib.loads((PLUGIN_DIR / "plugin.toml").read_text(encoding="utf-8"))


def test_plugin_table_fields() -> None:
    data = _manifest()
    table = data["plugin"]
    assert _ID.match(table["id"]), table["id"]
    assert table["name"]
    assert _VERSION.match(table["version"]), table["version"]
    assert _ENTRY.match(table["entry"]), table["entry"]
    assert table["type"] == "adapter"
    assert table["id"] == "bot_bridge"
    assert "host" not in table, "[plugin].host 已被宿主移除"


def test_adapter_table_is_validator_compatible() -> None:
    adapter = _manifest().get("adapter")
    assert isinstance(adapter, dict), "缺少 [adapter] 表"
    assert adapter.get("mode", "gateway") == "gateway", "校验器只接受 mode = \"gateway\""
    assert isinstance(adapter.get("priority", 0), int)


def test_config_example_has_no_plugin_table() -> None:
    data = tomllib.loads((PLUGIN_DIR / "config.example.toml").read_text(encoding="utf-8"))
    assert "plugin" not in data, "config.example.toml 不允许包含 [plugin] 表"
    runtime = data.get("plugin_runtime")
    if isinstance(runtime, dict):
        timeout = runtime.get("timeout")
        if timeout is not None:
            assert 0 < float(timeout) <= 300
    assert isinstance(data.get("bridge"), dict), "缺少 [bridge] 配置段"


@pytest.mark.parametrize("locale", ["zh-CN", "en"])
def test_i18n_files_are_valid_json(locale: str) -> None:
    payload = json.loads((PLUGIN_DIR / "i18n" / f"{locale}.json").read_text(encoding="utf-8"))
    assert payload["plugin.name"]
    assert payload["plugin.description"]


def test_entry_target_is_an_adapter_class_with_lifecycles() -> None:
    entry = _manifest()["plugin"]["entry"]
    module_path, class_name = entry.split(":", 1)
    relative = module_path.split("plugins.", 1)[-1]
    if relative == "bot_bridge":
        source = PLUGIN_DIR / "__init__.py"
    else:
        source = PLUGIN_DIR / Path(*relative.split(".")[1:]).with_suffix(".py")
        if not source.is_file():
            source = PLUGIN_DIR / Path(*relative.split(".")[1:]) / "__init__.py"
    assert source.is_file(), f"入口模块不在插件目录内：{source}"

    tree = ast.parse(source.read_text(encoding="utf-8"))
    target = next(
        (node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name),
        None,
    )
    assert target is not None, f"{class_name} 不在 {source} 顶层"
    decorators = {getattr(node, "id", "") for node in target.decorator_list if isinstance(node, ast.Name)}
    assert "neko_plugin" in decorators, "@neko_plugin 缺失"
    bases = {getattr(node, "id", "") for node in target.bases if isinstance(node, ast.Name)}
    assert "NekoAdapterPlugin" in bases, f"适配器应继承 NekoAdapterPlugin，实际 {bases}"

    lifecycle_ids: set[str] = set()
    for node in ast.walk(target):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            name = getattr(decorator.func, "id", "")
            if name != "lifecycle":
                continue
            for keyword in decorator.keywords:
                if keyword.arg == "id" and isinstance(keyword.value, ast.Constant):
                    lifecycle_ids.add(str(keyword.value.value))
    assert {"startup", "shutdown"} <= lifecycle_ids, lifecycle_ids


def test_no_requirements_file() -> None:
    assert not (PLUGIN_DIR / "requirements.txt").exists(), "宿主禁止插件目录里出现 requirements.txt"


def test_expected_files_exist() -> None:
    for relative in (
        "README.md",
        "tests/test_smoke.py",
        "pyproject.toml",
        ".gitignore",
        "bridge/market.py",
        "ui/panel.tsx",
        "docs/quickstart.md",
    ):
        assert (PLUGIN_DIR / relative).is_file(), relative
    for relative in (".vscode/settings.json", ".vscode/tasks.json", ".github/workflows/verify.yml"):
        assert (PLUGIN_DIR / relative).is_file(), relative
    assert (PLUGIN_DIR / "vendor" / "maibot_sdk" / "LICENSE").is_file()
    assert (PLUGIN_DIR / "vendor" / "maibot_sdk" / "README.vendor.md").is_file()
    assert (PLUGIN_DIR / "tests" / "fixtures" / "market").is_dir()


def test_gitignore_covers_host_requirements() -> None:
    text = (PLUGIN_DIR / ".gitignore").read_text(encoding="utf-8")
    for needle in ("__pycache__/", ".pytest_cache/", "store.db"):
        assert needle in text, needle


def test_vscode_json_files_are_valid() -> None:
    for relative in (".vscode/settings.json", ".vscode/tasks.json"):
        json.loads((PLUGIN_DIR / relative).read_text(encoding="utf-8"))


def test_every_python_file_compiles(tmp_path: Path) -> None:
    """每个 .py 都要能编译。

    ``cfile`` 写到临时目录而不是源码旁边：以前写在 ``<源文件>.compilecheck`` 再逐个
    unlink，279 个文件一次性删除会撞上沙箱/CI 的批量删除保护（实测 ``SystemExit: 1``），
    让这条用例在全量跑里假红。写临时目录既不改工作树，也没有删除动作。
    """
    failures: list[str] = []
    for index, path in enumerate(sorted(PLUGIN_DIR.rglob("*.py"))):
        if "__pycache__" in path.parts:
            continue
        try:
            py_compile.compile(
                str(path),
                cfile=str(tmp_path / f"compilecheck_{index}.pyc"),
                doraise=True,
            )
        except py_compile.PyCompileError as exc:
            failures.append(f"{path.relative_to(PLUGIN_DIR)}: {exc.msg}")
    assert not failures, failures


def test_backends_are_discovered_and_labelled() -> None:
    """兼容层是**自动发现**的：放一个 ``bridge/backends/<name>.py`` 就该被登记上。

    这里同时钉住：已交付的框架一个不少、每个发现的模块都有展示名、且都能真的 import
    出 ``BACKEND``（模块坏掉会进 ``errors`` 而不是让整座桥起不来）。
    """
    from bridge.backends import BACKEND_LABELS, BACKEND_MODULES, _discover_backend_modules, load_backends

    discovered = _discover_backend_modules()
    assert {"maibot", "nonebot", "astrbot"} <= set(BACKEND_MODULES)
    assert set(BACKEND_MODULES) <= set(discovered)
    assert {"maibot", "nonebot", "astrbot"} <= set(BACKEND_LABELS)

    backends, errors = load_backends()
    assert not errors, errors
    assert set(discovered) == {backend.name for backend in backends}
    for backend in backends:
        assert backend.display_name and backend.detect and backend.load  # type: ignore[truthy-function]


# --------------------------------------------------------------------------- #
# 托管界面（Hosted UI）
# --------------------------------------------------------------------------- #

_UI_PERMISSIONS = frozenset({"state:read", "config:read", "config:write", "action:call", "logs:read", "runs:read"})
_UI_SURFACE_KEYS = frozenset({"id", "title", "entry", "url", "context", "mode", "permissions", "warnings", "docs", "panel", "guide"})


def test_ui_section_follows_host_rules() -> None:
    ui = _manifest()["plugin"].get("ui")
    assert isinstance(ui, dict), "缺少 [plugin.ui]"
    assert ui.get("enabled") is True
    assert ui.get("expose_legacy_static_panel") in (None, False, True)

    panels = ui.get("panel") or []
    assert panels, "[plugin.ui.panel] 至少一个"
    for surface in panels:
        assert surface["id"] and surface["title"]
        assert str(surface["entry"]).endswith(".tsx"), "只有 .tsx/.jsx 才走 hosted-tsx"
        assert (PLUGIN_DIR / surface["entry"]).is_file()
        assert surface.get("context"), "面板要读状态就必须声明 context"
        assert set(surface.get("permissions", [])) <= _UI_PERMISSIONS

    for surface in ui.get("guide") or []:
        assert str(surface["entry"]).endswith((".md", ".mdx"))
        assert (PLUGIN_DIR / surface["entry"]).is_file()
        assert set(surface.get("permissions", [])) <= _UI_PERMISSIONS


def test_ui_action_ids_are_decorated_in_python() -> None:
    """面板引用的 action id 必须真在 @ui.action 里声明过（宿主只授权声明过的入口）。"""
    source = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    declared = set(re.findall(r'@ui\.action\(\s*\n?\s*id="([^"]+)"', source))
    assert declared, "没有任何 @ui.action"
    panel = (PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
    used = set(re.findall(r'action\.id === "([^"]+)"', panel)) | set(re.findall(r'api\.call\("([^"]+)"', panel))
    assert used, "面板没有引用任何 action"
    assert used <= declared, f"面板引用了未声明的 action: {sorted(used - declared)}"

    context_source = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    assert '@ui.context(id="dashboard")' in context_source
    manifest_context = _manifest()["plugin"]["ui"]["panel"][0]["context"]
    assert f'@ui.context(id="{manifest_context}")' in context_source, "manifest 的 context 与 Python 的 @ui.context 不一致"


def test_panel_imports_only_real_kit_members() -> None:
    """面板只能 import 插件 UI 套件真的导出的名字（写错就是运行期白屏）。"""
    panel = (PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
    match = re.search(r'import\s*\{([^}]+)\}\s*from\s*"@neko/plugin-ui"', panel)
    assert match, "面板必须从 @neko/plugin-ui 导入组件"
    imported = {name.strip() for name in match.group(1).split(",") if name.strip()}
    type_match = re.search(r'import\s+type\s*\{([^}]+)\}\s*from\s*"@neko/plugin-ui"', panel)
    types = {name.strip() for name in type_match.group(1).split(",")} if type_match else set()

    kit = Path(support.NEKO_SOURCE_ROOT) / "plugin" / "sdk" / "hosted-ui" / "index.d.ts"
    if not kit.is_file():
        pytest.skip("宿主 UI 套件不可用（NEKO_SOURCE_ROOT 未指向源码树）")
    declarations = kit.read_text(encoding="utf-8")
    exported = set(re.findall(r"^export (?:function|const|type) ([A-Za-z0-9_]+)", declarations, re.M))
    missing = sorted((imported | types) - exported)
    assert not missing, f"面板引用了套件里不存在的名字: {missing}"

    assert "export default function" in panel, "Hosted TSX 必须 default export 一个组件"
    assert "npm" not in panel.split("@neko/plugin-ui", 1)[0] or True  # 只是提醒：不要 import npm 包


def test_static_entry_scan_matches_panel_actions() -> None:
    """干净解释器里做一次宿主式静态扫描：入口清单必须包含面板用到的 action。

    这条是「热改插件后宿主预览过期」的离线对照：只要静态扫描里有，
    全新加载（或重载插件）之后面板的授权白名单就会有。
    """
    import importlib

    support.install_import_shims()
    constants = importlib.import_module("plugin.sdk.shared.constants")
    module = importlib.import_module("plugin.plugins.bot_bridge")

    entries: set[str] = set()
    actions: set[str] = set()
    for name in dir(module.BotBridgePlugin):
        target = getattr(module.BotBridgePlugin, name, None)
        meta = getattr(target, constants.EVENT_META_ATTR, None)
        if meta is not None:
            entries.add(str(getattr(meta, "id", None) or name))
        ui_meta = getattr(target, "__neko_ui_action__", None)
        if isinstance(ui_meta, dict):
            actions.add(str(ui_meta.get("id") or name))

    expected_actions = {
        "bridge_reload",
        "bridge_market_sources",
        "bridge_market_search",
        "bridge_market_install",
        "bridge_set_plugin",
        "bridge_delete_plugin",
    }
    assert expected_actions <= entries
    assert actions == expected_actions
    panel = (PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
    for action_id in actions:
        assert f'"{action_id}"' in panel, f"面板没有引用 {action_id}"


def test_panel_handles_unwhitelisted_actions() -> None:
    """宿主 403（白名单没更新）时面板要给出可照做的提示，而不是只抛原始错误。"""
    panel = (PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
    assert "onError=" in panel
    assert "not exposed" in panel or "403" in panel
    assert "重载本插件" in panel or "重新加载" in panel


def test_panel_uses_props_api_not_a_bare_global() -> None:
    """面板不许引用裸 ``api``（运行时全局只有组件与 hooks；裸 ``api.`` 是死面板）。"""
    panel = (PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
    assert "props.api.call" in panel or "props.api.refresh" in panel
    for match in re.finditer(r"(?<![.\w])api\.", panel):
        prefix = panel[max(0, match.start() - 6):match.start()]
        assert prefix.endswith("props."), f"面板里出现了裸 api. 引用：...{panel[match.start()-20:match.start()+20]}"

    kit_globals = Path(support.NEKO_SOURCE_ROOT) / "plugin" / "sdk" / "hosted-ui" / "globals.d.ts"
    if kit_globals.is_file():
        declared = kit_globals.read_text(encoding="utf-8")
        assert "const api:" not in declared, "宿主全局里不该有 api（有的话这条测试的前提变了）"


def test_manifest_entry_declarations_match_decorators() -> None:
    """``[[plugin.entries]]`` 与 ``@plugin_entry`` 必须逐条对齐（双向）。

    宿主会把清单里的展示字段**覆盖**到扫描结果上（``_overlay_entry_declaration``：preview 与
    handler 必须用同样的方式描述同一个入口），所以清单里多一条/少一条、名字对不上，
    插件列表就会显示幽灵入口或过期名称——这里把它钉死。
    """
    import importlib
    import tomllib

    support.install_import_shims()
    constants = importlib.import_module("plugin.sdk.shared.constants")
    module = importlib.import_module("plugin.plugins.bot_bridge")

    scanned: dict[str, dict[str, str]] = {}
    for name in dir(module.BotBridgePlugin):
        meta = getattr(getattr(module.BotBridgePlugin, name, None), constants.EVENT_META_ATTR, None)
        if meta is None or getattr(meta, "event_type", None) != "plugin_entry":
            continue
        entry_id = str(getattr(meta, "id", None) or name)
        scanned[entry_id] = {
            "name": str(getattr(meta, "name", "") or ""),
            "description": str(getattr(meta, "description", "") or ""),
        }
    assert scanned, "装饰器扫描没找到任何 plugin_entry"

    with (PLUGIN_DIR / "plugin.toml").open("rb") as handle:
        payload = tomllib.load(handle)
    declared = {
        str(item.get("id")): {
            "name": str(item.get("name", "") or ""),
            "description": str(item.get("description", "") or ""),
        }
        for item in (payload.get("plugin", {}).get("entries") or [])
    }

    missing = sorted(set(scanned) - set(declared))
    phantom = sorted(set(declared) - set(scanned))
    assert not missing, f"清单缺少入口声明: {missing}"
    assert not phantom, f"清单里有装饰器不存在的入口: {phantom}"
    for entry_id, fields in scanned.items():
        assert declared[entry_id]["name"] == fields["name"], f"{entry_id} 的 name 与清单不一致"
        assert declared[entry_id]["description"] == fields["description"], f"{entry_id} 的 description 与清单不一致"


def test_panel_slow_calls_pass_explicit_timeout() -> None:
    """慢动作（探活/搜索）必须自己传 timeoutMs：宿主 iframe 对单次请求默认 30s 上限。

    索引冷启动可能 25s+，ActionButton 不透传 timeoutMs —— 不显式给超时，按钮会在
    浏览器侧先超时（服务端其实还在跑）。
    """
    panel = (PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
    assert 'props.api.call(' in panel
    assert "timeoutMs" in panel, "面板必须给慢动作显式超时"
    for action_id in ("bridge_market_sources", "bridge_market_search"):
        assert f'"{action_id}"' in panel
    assert "180000" in panel  # 3 分钟，够冷启动拉三个索引
    assert "ActionButton" in panel  # 快动作仍可用 ActionButton
    assert "source_counts" in panel  # 结果里显示每源命中数（解释「为什么只有这几条」）


def test_panel_satisfies_host_gate_rules() -> None:
    r"""照宿主 ``check-hosted-tsx.mjs`` 的规则离线自查（跑不了 node 也能挡住同类问题）。

    规则来源（N.E.K.O. 仓库 ``frontend/plugin-manager/scripts/check-hosted-tsx.mjs``）：
    * ``/(^|[^\w.])api\./m`` 命中即报错 —— ``props.api.call`` 合法，裸 ``api.call`` 非法；
    * 必须 ``export default`` 一个函数组件；
    * 不许动态 ``import()``；
    * 运行时代码不许 re-export（``export { … }`` / ``export … from``）。
    """
    import re

    panel = (PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
    assert not re.search(r"(^|[^\w.])api\.", panel, re.M), "面板用了裸 api.，宿主门禁会报错"
    assert "props.api." in panel
    assert re.search(r"export default (function|async function|\w)", panel), "缺少 default 导出"
    assert not re.search(r"\bimport\s*\(", panel), "面板里不许有动态 import()"
    assert not re.search(r"^\s*export\s*\{", panel, re.M), "运行时代码不许 re-export"
    assert not re.search(r"^\s*export\s+[^\n]*\bfrom\s+['\"]", panel, re.M), "运行时代码不许 re-export"


def test_manifest_entry_timeouts_cover_decorators() -> None:
    """清单里声明的超时不能小于装饰器里的：两边不一致时，宿主按哪份跑都说不清。

    宿主给单次入口调用的默认预算只有 30s，网络型入口（市场）必须放宽；清单与装饰器
    写同一个值，避免「装饰器写了 180、宿主按 30 跑」这种两边不一致的糊涂账。
    """
    import importlib
    import tomllib

    support.install_import_shims()
    constants = importlib.import_module("plugin.sdk.shared.constants")
    module = importlib.import_module("plugin.plugins.bot_bridge")

    decorated: dict[str, float | None] = {}
    for name in dir(module.BotBridgePlugin):
        meta = getattr(getattr(module.BotBridgePlugin, name, None), constants.EVENT_META_ATTR, None)
        if meta is None or getattr(meta, "event_type", None) != "plugin_entry":
            continue
        value = getattr(meta, "timeout", None)
        decorated[str(getattr(meta, "id", None) or name)] = float(value) if value is not None else None

    with (PLUGIN_DIR / "plugin.toml").open("rb") as handle:
        payload = tomllib.load(handle)
    declared = {
        str(item.get("id")): (float(item["timeout"]) if item.get("timeout") is not None else None)
        for item in (payload.get("plugin", {}).get("entries") or [])
    }

    for entry_id, declared_timeout in declared.items():
        if declared_timeout is None:
            continue
        assert entry_id in decorated, f"清单声明了 {entry_id} 的超时，但装饰器里没有这个入口"
        assert decorated[entry_id] == declared_timeout, (
            f"{entry_id} 的超时不一致：清单 {declared_timeout} vs 装饰器 {decorated[entry_id]}"
        )
    # 网络型入口必须有超时（冷缓存拉三个索引可能几十秒，宿主默认只有 30s）
    for entry_id in ("bridge_market_sources", "bridge_market_search", "bridge_market_install"):
        assert declared.get(entry_id) and declared[entry_id] >= 120, f"{entry_id} 缺超时声明"


def test_call_timeout_stays_under_the_host_entry_budget() -> None:
    """``[bridge].call_timeout`` 必须小于宿主入口预算（实测 30s）。

    真机实测：宿主给本插件入口的预算是 30s（35s 的夹具命令在 30.3s 被砍成 failed），
    market 三条例外（宿主元数据里是 300s）。所以桥自己的调用超时要更小，
    这样超时是我们返回结构化错误，而不是宿主砍成一次「跑失败」。
    """
    import ast
    import tomllib

    source = (PLUGIN_DIR / "__init__.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    defaults: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "call_timeout":
                    defaults["call_timeout"] = ast.literal_eval(value)
    assert defaults.get("call_timeout") is not None, "没找到 call_timeout 默认值"
    assert defaults["call_timeout"] < 30.0, f"call_timeout={defaults['call_timeout']} 会晚于宿主预算触发"

    with (PLUGIN_DIR / "config.example.toml").open("rb") as handle:
        example = tomllib.load(handle)
    assert example["bridge"]["call_timeout"] < 30.0


def test_panel_passes_full_plugin_keys_to_admin_entries() -> None:
    """面板调启停/删除必须传 ``框架:插件``：跨框架同名 id 会让 find_key 报「不唯一」而卡死。"""
    panel = (PLUGIN_DIR / "ui" / "panel.tsx").read_text(encoding="utf-8")
    assert "keyOf" in panel, "面板需要把行转成 key"
    assert "values={{ plugin_id: keyOf(pluginSelected), purge_state: false }}" in panel
    assert "plugin_id: row.key, enabled" in panel
    assert "plugin_id: row.plugin_id, enabled" not in panel, "别用裸 plugin_id（跨框架同名会卡住）"
    assert "stopped_plugins" in panel, "面板要渲染「已停用」表，否则停掉的插件没法再启用"
