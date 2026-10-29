"""把本插件打成可分发 zip —— 按排除清单来，不整目录硬压。

沿用 ``plugins/package_plugin_template.py`` 的规则（手工压缩会把 ``__pycache__`` /
``.pyc`` / ``.pytest_cache`` 一起塞进去，本机实测能占到四成体积），再排除本插件特有
的运行期状态：

* ``config/`` —— 机器本地配置（首次运行由 ``config.example.toml`` 生成）
* ``data/``   —— 外来插件目录、市场缓存、启停状态（**绝不能**打进包里）
* ``tests/``  —— 开发用夹具与真机脚本，运行时不需要
* ``.git/`` ``.github/`` ``.vscode/`` —— 版本库与编辑器元数据

用法（CI 与本地同一份）：
    python tools/package_release.py [--out DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import re
import zipfile
from pathlib import Path

#: 本文件在 <插件根>/tools/ 下，插件根就是它的上上级
SRC_DEFAULT = Path(__file__).resolve().parent.parent

#: **只在仓库根**才跳过的目录（config/ 不能全局匹配：shims/astrbot/core/config/ 是源码）
ROOT_ONLY_SKIP = {
    "config",
    "data",
    "tests",
    "tools",
    "dist",
    ".git",
    ".github",
    ".vscode",
}
#: 任何层级都要跳过的缓存
ANYWHERE_SKIP = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
SKIP_SUFFIX = (".pyc", ".pyo", ".pyd", ".log", ".bak", ".compilecheck")
SKIP_NAME_PARTS = (".bak-", ".bak.", ".DS_Store")

#: 打进 zip 时把 CRLF 归一成 LF。
#: 为什么必须做：CI 用 LF 检出、本机 git 用 CRLF（actions/checkout 在 Windows 上会设
#: core.autocrlf），同一份源码打出来的 zip 字节不同，「本地验过的 sha」和「发出去的
#: sha」就对不上，发布无法自证。
#: 判据用「内容里没有 NUL 字节」而不是后缀白名单 —— 像 .gitignore 这种无扩展名的
#: 文本文件，用白名单会漏掉（实测就漏了它）。
FIXED_ZIP_TIME = (2026, 10, 29, 0, 0, 0)


def _payload(path: Path) -> bytes:
    """读条目内容；可按文本处理的把 CRLF 归一成 LF，二进制原样保留。"""
    raw = path.read_bytes()
    if b"\x00" not in raw:
        return raw.replace(b"\r\n", b"\n")
    return raw


def keep(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = rel.parts
    if parts and parts[0] in ROOT_ONLY_SKIP:
        return False
    if any(part in ANYWHERE_SKIP for part in parts):
        return False
    if path.suffix in SKIP_SUFFIX:
        return False
    return not any(part in path.name for part in SKIP_NAME_PARTS)


def read_version(root: Path) -> str:
    text = (root / "plugin.toml").read_text(encoding="utf-8")
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
    if not match:
        raise SystemExit("plugin.toml 里没有 version 字段")
    return match.group(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(SRC_DEFAULT))
    ap.add_argument("--out", default=str(Path("dist")))
    args = ap.parse_args()

    root = Path(args.src).resolve()
    if not (root / "plugin.toml").is_file():
        raise SystemExit(f"不是插件目录：{root}")
    version = read_version(root)
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"bot_bridge-{version}.zip"

    files = sorted(p for p in root.rglob("*") if p.is_file() and keep(p, root))
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in files:
            info = zipfile.ZipInfo(
                f"bot_bridge/{path.relative_to(root).as_posix()}",
                date_time=FIXED_ZIP_TIME,
            )
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o644 << 16
            zf.writestr(info, _payload(path), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)

    digest = hashlib.sha256(target.read_bytes()).hexdigest()

    print(f"版本      : {version}")
    print(f"产物      : {target}")
    print(f"条目      : {len(files)}")
    print(f"体积      : {target.stat().st_size / 1024 / 1024:.2f} MiB")
    print(f"sha256    : {digest}")

    # 自检：必需项在、禁项不在（装错包是最难查的故障）
    with zipfile.ZipFile(target) as zf:
        names = zf.namelist()
    stripped = {n[len("bot_bridge/") :] if n.startswith("bot_bridge/") else n for n in names}
    required = {
        "plugin.toml",
        "__init__.py",
        "bridge/router.py",
        "shims/nonebot/__init__.py",
        "shims/astrbot/core/config/__init__.py",
        "vendor/maibot_sdk/__init__.py",
        "config.example.toml",
    }
    missing = sorted(required - stripped)
    forbidden = [
        n
        for n in names
        if "/__pycache__/" in n
        or n.endswith(".pyc")
        or n.startswith("bot_bridge/data/")
        or n.startswith("bot_bridge/config/")
        or n.startswith("bot_bridge/tests/")
    ]
    print(f"自检      : 缺 {missing or '无'} / 违禁 {forbidden[:3] or '无'}")
    return 0 if not missing and not forbidden else 1


if __name__ == "__main__":
    raise SystemExit(main())
