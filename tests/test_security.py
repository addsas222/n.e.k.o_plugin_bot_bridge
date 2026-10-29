"""``bridge/security.py`` 静态扫描的针对性测试。

覆盖：dangerous / warning 两级命中的识别与行号、干净插件零误报、
node_modules 与非源码后缀跳过、文件数/体积上限的截断语义。
"""

from __future__ import annotations

from pathlib import Path

from bridge.security import scan_plugin_dir


def _make_plugin(tmp_path: Path, files: dict[str, str]) -> Path:
    root = tmp_path / "plugin"
    for name, content in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return root


def test_python_dangerous_hits_with_line_numbers(tmp_path: Path) -> None:
    root = _make_plugin(
        tmp_path,
        {
            "main.py": "echo = 'hi'\n",  # 第 1 行：干净
            "evil.py": "x = eval(payload)\nsubprocess.run(cmd)\n",
        },
    )
    report = scan_plugin_dir(root)
    assert not report.clean
    reasons = {(item.file, item.line, item.tier) for item in report.dangerous}
    assert ("evil.py", 1, "dangerous") in reasons  # eval
    assert ("evil.py", 2, "dangerous") in reasons  # subprocess.run
    assert all(item.file == "evil.py" for item in report.findings)  # main.py 零误报


def test_javascript_dangerous_hits(tmp_path: Path) -> None:
    root = _make_plugin(
        tmp_path,
        {
            "index.js": "const { spawn } = require('child_process')\nconst f = new Function('return 1')\n",
            "sub/more.mjs": "eval(userInput)\n",
        },
    )
    report = scan_plugin_dir(root)
    reasons = {item.reason for item in report.dangerous}
    assert any("child_process" in reason for reason in reasons)
    assert any("new Function" in reason for reason in reasons)
    assert any("eval" in reason for reason in reasons)
    assert any(Path(item.file) == Path("sub") / "more.mjs" for item in report.dangerous)  # 子目录也扫


def test_warning_tier_and_traversal_same_line_rule(tmp_path: Path) -> None:
    root = _make_plugin(
        tmp_path,
        {
            "cleaner.py": (
                "shutil.rmtree(cache_dir)\n"  # 递归删除：恒为 warning
                "os.remove(temp)\n"  # 无 ..：不算
                "os.remove(base + '/../other/file')\n"  # 同行含 ..：算
            ),
        },
    )
    report = scan_plugin_dir(root)
    assert report.dangerous == []
    reasons = [(item.line, item.reason) for item in report.warnings]
    assert (1, "递归删除目录 shutil.rmtree") in reasons
    assert (3, "按路径删除文件（同行含 ..）") in reasons
    assert all(line != 2 for line, _ in reasons), reasons  # 第 2 行不能误报


def test_clean_plugin_reports_clean(tmp_path: Path) -> None:
    root = _make_plugin(
        tmp_path,
        {
            "main.py": "from nonebot import on_command\n\nmatcher = on_command('ping')\n",
            "util.js": "export function greet(name) { return `hi ${name}` }\n",
        },
    )
    report = scan_plugin_dir(root)
    assert report.clean
    assert report.files_scanned == 2
    payload = report.as_dict()
    assert payload["clean"] and payload["dangerous"] == 0 and payload["findings"] == []


def test_skips_node_modules_dotdirs_and_binary_suffixes(tmp_path: Path) -> None:
    root = _make_plugin(
        tmp_path,
        {
            "main.py": "echo = 1\n",
            "node_modules/evil/index.js": "eval(1)\n",
            ".git/hooks/pre-commit.py": "eval(1)\n",
            "logo.png": "eval(1)\n",  # 非源码后缀
            "data/state.json": "eval(1)\n",  # 数据目录 + 非源码后缀
        },
    )
    report = scan_plugin_dir(root)
    assert report.clean, [item.as_dict() for item in report.findings]
    assert report.files_scanned == 1  # 只有 main.py 进扫描


def test_file_count_limit_truncates(tmp_path: Path, monkeypatch: object) -> None:
    import bridge.security as security

    root = _make_plugin(tmp_path, {"main.py": "eval(1)\n"})
    for index in range(5):
        (root / f"filler_{index}.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(security, "_MAX_FILES", 3)  # type: ignore[attr-defined]
    report = security.scan_plugin_dir(root)
    assert report.truncated
    assert report.files_scanned == 3


def test_oversized_file_skipped_with_warning(tmp_path: Path, monkeypatch: object) -> None:
    import bridge.security as security

    root = _make_plugin(tmp_path, {"big.py": "eval(1)\n" * 3})
    monkeypatch.setattr(security, "_MAX_FILE_BYTES", 10)  # type: ignore[attr-defined]
    report = security.scan_plugin_dir(root)
    assert report.dangerous == []  # 没读进去，不能假装扫过
    assert any("未扫描" in item.reason for item in report.warnings)


def test_attribute_calls_are_not_dynamic_execution(tmp_path: Path) -> None:
    """回归：``re.compile`` / ``model.eval`` / ``matcher.exec`` 是正当代码，不能误报。"""
    root = _make_plugin(
        tmp_path,
        {
            "legit.py": (
                "PATTERN = re.compile(r'ping')\n"
                "result = model.eval()\n"
                "matches = matcher.exec(text)\n"
                "handler = self.compile_plan(steps)\n"
            ),
            "evil.py": (
                "PATTERN = compile('x')\n"
                "value = eval(user_input)\n"
                "run(input)\n"  # 非属性前缀的 exec 形态这里不会出现；只验证裸调用命中
            ),
        },
    )
    report = scan_plugin_dir(root)
    legit_hits = [item for item in report.findings if item.file == "legit.py"]
    assert legit_hits == [], [item.as_dict() for item in legit_hits]  # 零误报
    assert {item.line for item in report.dangerous if item.file == "evil.py"} == {1, 2}


def test_single_file_plugin_is_scanned(tmp_path: Path) -> None:
    """单文件形态（nonebot 裸 ``.py``）必须同样受扫 —— 风险最高的随手投放。"""
    dangerous = tmp_path / "evil_plugin.py"
    dangerous.write_text("import nonebot\n\nx = eval('1+1')\n", encoding="utf-8")
    report = scan_plugin_dir(dangerous)
    assert not report.clean
    assert report.files_scanned == 1
    assert report.dangerous[0].reason.startswith("动态执行 eval")

    clean = tmp_path / "clean_plugin.py"
    clean.write_text("from nonebot import on_command\nping = on_command('ping')\n", encoding="utf-8")
    assert scan_plugin_dir(clean).clean

    not_source = tmp_path / "README.md"
    not_source.write_text("eval(1)\n", encoding="utf-8")
    assert scan_plugin_dir(not_source).files_scanned == 0  # 非源码后缀不扫也不误报


def test_all_bundled_fixtures_are_clean(tmp_path: Path) -> None:
    """仓库自带夹具必须在 enforce 规则下**零 dangerous 命中**。

    这条是误报回归闸：任何人往规则表里加正则，都要先过一遍真实生态的夹具。
    """
    fixtures = Path(__file__).resolve().parent / "fixtures"
    report = scan_plugin_dir(fixtures)
    assert report.dangerous == [], [item.as_dict() for item in report.dangerous]
    assert not report.truncated
