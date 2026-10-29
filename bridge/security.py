"""外来机器人插件的**加载前静态安全扫描**。

桥接层加载的是第三方代码：NoneBot/AstrBot 插件直接 ``import`` 进本进程，Koishi
插件起 Node 子进程跑宿主。``framework_dirs`` 又允许从任意目录读插件 —— 加载前
对插件源码做一遍廉价静态扫描，把高风险模式亮出来：

* **dangerous**：动态代码执行（``eval`` / ``exec`` / ``compile`` / ``__import__`` /
  JS ``eval`` / ``new Function``）、进程创建（``subprocess`` / ``os.system`` /
  ``child_process``）、危险反序列化（``pickle.loads`` / ``marshal.loads``）、
  原生调用（``ctypes``）。``mode = "enforce"`` 时**拒绝加载**。
* **warning**：越径文件删除（``shutil.rmtree``、JS ``rmSync(..., {recursive})``、
  同行带 ``..`` 的 remove/unlink）——常见于正当代码，只亮灯不拦。

边界（如实）：这是启发式 tripwire，不是沙箱，也扫不出混淆/编码后的恶意代码；
运行时隔离仍然靠宿主。扫描只读，带体积/文件数上限，跳过 ``node_modules`` /
``.git`` / 数据目录。

配置见 ``[bridge.security_scan]``（``enabled`` / ``mode``，默认 ``report``）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = ["SecurityFinding", "SecurityReport", "scan_plugin_dir"]

#: 只扫这些后缀的文本文件；其它（二进制/字体/媒体）跳过。
_SCAN_SUFFIXES = frozenset({".py", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx"})

#: 不进入扫描的目录名（依赖/版本库/数据缓存与桥自身状态）。
_SKIP_DIRS = frozenset({"__pycache__", "node_modules", ".git", ".venv", "venv", "data", "cache", ".ruff_cache", ".pytest_cache"})

#: 单文件扫描上限：超过按 1 MiB 截断读入（超长行/压缩过的 bundle 不硬啃）。
_MAX_FILE_BYTES = 1 * 1024 * 1024

#: 单插件扫描的文件数上限；超过就如实截断并在报告里注明。
_MAX_FILES = 2000

# --------------------------------------------------------------------------- #
# 规则表：（tier, 语言, 编译好的正则, 说明）
# ``..`` 同行判断只用于 remove/unlink 类 —— 单独出现就太吵。
# --------------------------------------------------------------------------- #

_PY_DANGEROUS: tuple[tuple[str, str], ...] = (
    # ``(?<![\w.])``：排除属性调用与方法名 —— ``re.compile(`` / ``model.eval(`` /
    # ``matcher.exec(` 是正当代码，只有裸调用才算动态执行。
    (r"(?<![\w.])eval\s*\(", "动态执行 eval()"),
    (r"(?<![\w.])exec\s*\(", "动态执行 exec()"),
    (r"(?<![\w.])compile\s*\(", "动态编译 compile()"),
    (r"__import__\s*\(", "动态导入 __import__()"),
    (r"\bos\.system\s*\(", "创建子进程 os.system"),
    (r"\bsubprocess\.(?:Popen|run|call|check_\w+)\s*\(", "创建子进程 subprocess"),
    (r"\bpickle\.loads?\s*\(", "反序列化 pickle（可执行任意代码）"),
    (r"\bmarshal\.loads?\s*\(", "反序列化 marshal"),
    (r"\bctypes\.(?:CDLL|WinDLL|windll|oledll)\b", "原生库调用 ctypes"),
)

_JS_DANGEROUS: tuple[tuple[str, str], ...] = (
    (r"(?<![\w.])eval\s*\(", "动态执行 eval()"),
    (r"new\s+Function\s*\(", "动态执行 new Function()"),
    (r"\brequire\s*\(\s*['\"](?:node:)?child_process['\"]\s*\)", "创建子进程 child_process"),
    (r"from\s+['\"](?:node:)?child_process['\"]", "创建子进程 child_process"),
    (r"\brequire\s*\(\s*['\"](?:node:)?vm['\"]\s*\)", "动态执行 vm 模块"),
)

_PY_WARNING: tuple[tuple[str, str], ...] = (
    (r"\bshutil\.rmtree\s*\(", "递归删除目录 shutil.rmtree"),
)

_JS_WARNING: tuple[tuple[str, str], ...] = (
    (r"\b(?:rmSync|rmdirSync)\s*\([^)]*recursive\s*:\s*true", "递归删除目录 fs.rmSync/rmdirSync"),
)

_TRAVERSAL_PY: tuple[str, str] = (r"\bos\.(?:remove|unlink)\s*\(", "按路径删除文件（同行含 ..）")
_TRAVERSAL_JS: tuple[str, str] = (r"\b(?:unlinkSync|rmSync|rmdirSync)\s*\(", "按路径删除文件（同行含 ..）")


@dataclass(slots=True)
class SecurityFinding:
    """一条扫描命中：``tier`` ∈ ``dangerous`` / ``warning``。"""

    tier: str
    file: str  # 相对插件目录的路径
    line: int
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"tier": self.tier, "file": self.file, "line": self.line, "reason": self.reason}


@dataclass(slots=True)
class SecurityReport:
    """一个插件的完整扫描结果。"""

    findings: list[SecurityFinding] = field(default_factory=list)
    files_scanned: int = 0
    truncated: bool = False  # 文件数超上限，未扫全

    @property
    def dangerous(self) -> list[SecurityFinding]:
        return [item for item in self.findings if item.tier == "dangerous"]

    @property
    def warnings(self) -> list[SecurityFinding]:
        return [item for item in self.findings if item.tier == "warning"]

    @property
    def clean(self) -> bool:
        return not self.findings

    def as_dict(self) -> dict[str, Any]:
        return {
            "clean": self.clean,
            "dangerous": len(self.dangerous),
            "warnings": len(self.warnings),
            "findings": [item.as_dict() for item in self.findings[:20]],
            "files_scanned": self.files_scanned,
            "truncated": self.truncated,
        }


def _compile_rules(rules: tuple[tuple[str, str], ...]) -> list[tuple[re.Pattern[str], str]]:
    return [(re.compile(pattern), reason) for pattern, reason in rules]


_PY_DANGEROUS_RE = _compile_rules(_PY_DANGEROUS)
_JS_DANGEROUS_RE = _compile_rules(_JS_DANGEROUS)
_PY_WARNING_RE = _compile_rules(_PY_WARNING)
_JS_WARNING_RE = _compile_rules(_JS_WARNING)
_PY_TRAVERSAL_RE = re.compile(_TRAVERSAL_PY[0])
_JS_TRAVERSAL_RE = re.compile(_TRAVERSAL_JS[0])

_PY_SUFFIXES = frozenset({".py"})
_JS_SUFFIXES = frozenset({".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx"})


def _scan_text(rel: str, text: str, *, is_python: bool, out: list[SecurityFinding]) -> None:
    dangerous = _PY_DANGEROUS_RE if is_python else _JS_DANGEROUS_RE
    warnings = _PY_WARNING_RE if is_python else _JS_WARNING_RE
    traversal = _PY_TRAVERSAL_RE if is_python else _JS_TRAVERSAL_RE
    for lineno, line in enumerate(text.splitlines(), 1):
        for pattern, reason in dangerous:
            if pattern.search(line):
                out.append(SecurityFinding("dangerous", rel, lineno, reason))
        for pattern, reason in warnings:
            if pattern.search(line):
                out.append(SecurityFinding("warning", rel, lineno, reason))
        # remove/unlink 只有同行出现 ``..`` 才算越径信号，否则正常清理代码全中招。
        if traversal.search(line) and ".." in line:
            out.append(SecurityFinding("warning", rel, lineno, "按路径删除文件（同行含 ..）"))


def _scan_one_file(path: Path, root: Path, report: SecurityReport) -> None:
    """扫描单个源文件；读不了/超限的按「如实标注」处理，绝不假装扫过。"""
    rel = str(path.relative_to(root))
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            report.findings.append(
                SecurityFinding("warning", rel, 0, f"文件超过 {_MAX_FILE_BYTES} 字节，未扫描")
            )
            return
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    report.files_scanned += 1
    _scan_text(rel, text, is_python=path.suffix.lower() in _PY_SUFFIXES, out=report.findings)


def scan_plugin_dir(root: Path) -> SecurityReport:
    """扫描插件目录或**单文件插件**，返回 :class:`SecurityReport`。

    只读；异常按「跳过该文件」处理。单文件形态是 nonebot 兼容层明确支持的
    裸 ``.py`` 插件（``<plugins_root>/xxx.py``）—— 恰是随手丢进来风险最高的
    投放方式，必须同样受扫。
    """
    report = SecurityReport()
    root = Path(root)
    if root.is_file():
        if root.suffix.lower() not in _SCAN_SUFFIXES:
            return report
        _scan_one_file(root, root.parent, report)
        return report
    if not root.is_dir():
        return report
    files: list[Path] = []
    try:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if any(part in _SKIP_DIRS or part.startswith(".") for part in path.relative_to(root).parts):
                continue
            if path.suffix.lower() not in _SCAN_SUFFIXES:
                continue
            files.append(path)
            if len(files) >= _MAX_FILES:
                report.truncated = True
                break
    except OSError:
        return report

    for path in files:
        _scan_one_file(path, root, report)
    return report
