"""Koishi 市场源（``KoishiSource``）的针对性测试。

全部离线：索引用录好的 npm 镜像搜索 JSON 切片（``tests/fixtures/market/koishi_search.json``）；
安装解压走真实的 ``.tgz``（测试里用 ``tarfile`` 现做），安全边界与 zip 同一套断言。
"""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

import bridge.market as market_mod
from bridge.errors import BridgeError
from bridge.market import (
    KoishiSource,
    MarketEntry,
    MarketError,
    _KoishiShape,
    _install_archive,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "market"
KOISHI_SEARCH = FIXTURES / "koishi_search.json"


def _fixture_entries() -> list:
    payload = json.loads(KOISHI_SEARCH.read_text(encoding="utf-8"))
    return KoishiSource().parse(payload)


def test_parse_maps_all_entries() -> None:
    entries = _fixture_entries()
    assert len(entries) == 3
    first = entries[0]
    assert first.source == "koishi"
    assert first.plugin_id == "koishi-plugin-dataview"
    assert first.version == "2.7.8"
    assert first.author == "hieuzest"
    assert first.repository == "https://github.com/hieuzest/koishi-plugin-dataview"
    assert first.install_kind == "koishi"
    assert first.download_url == (
        "https://registry.npmmirror.com/koishi-plugin-dataview/-/"
        "koishi-plugin-dataview-2.7.8.tgz"
    )


def test_parse_scoped_tarball_url_encodes_scope() -> None:
    entries = _fixture_entries()
    scoped = next(item for item in entries if item.plugin_id.startswith("@scope/"))
    # scoped 包名要把 @scope/name 编码成 @scope%2Fname
    assert "%40scope%2Fkoishi-plugin-scoped" in scoped.download_url
    assert scoped.download_url.endswith("/koishi-plugin-scoped-0.2.1.tgz")


def test_tarball_url_survives_registry_slash_mangling() -> None:
    source = KoishiSource()
    url = source._tarball_url("koishi-plugin-x", "1.0.0")
    assert url.startswith("https://registry.npmmirror.com/koishi-plugin-x/-/")
    assert url.endswith("koishi-plugin-x-1.0.0.tgz")


def test_index_url_reads_the_market_json() -> None:
    """市场来源必须是「读 JSON 文件」，不是探活某个运行中的软件。"""
    source = KoishiSource()
    assert source.index_url.startswith("https://registry.npmmirror.com/-/v1/search")
    assert "koishi-plugin" in source.index_url


# --------------------------------------------------------------------------- #
# tgz 安装解压
# --------------------------------------------------------------------------- #


def _make_tgz(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, payload in members.items():
            data = payload.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _plan(tmp_path: Path, url: str) -> market_mod.InstallPlan:
    entry = MarketEntry(source="koishi", plugin_id="koishi-plugin-smoke", install_kind="koishi")
    return market_mod.InstallPlan(
        entry=entry, target_dir=tmp_path / "koishi" / "smoke", urls=(url,)
    )


def test_tgz_install_extracts_package_root(tmp_path: Path) -> None:
    """npm 风格 tgz（单层 package/ 前缀）解包后要落到 package.json 那一层。"""
    blob = _make_tgz(
        {
            "package/package.json": json.dumps({"name": "koishi-plugin-smoke"}),
            "package/index.js": "module.exports = () => {};",
            "package/README.md": "readme",
        }
    )
    files, warning, requires = _install_archive(
        blob,
        _plan(tmp_path, "https://registry.npmmirror.com/x/-/x-1.0.0.tgz"),
        shape=_KoishiShape(),
        max_extract_bytes=10_000_000,
        max_files=500,
    )
    target = tmp_path / "koishi" / "smoke"
    assert (target / "package.json").is_file()
    assert (target / "index.js").is_file()
    assert not (target / "README.md").exists() or (target / "README.md").is_file()
    assert files >= 2
    assert requires == ()  # npm 包没有 wheel metadata


def test_tgz_install_rejects_path_traversal(tmp_path: Path) -> None:
    blob = _make_tgz(
        {
            "package/package.json": "{}",
            "package/../escape.js": "x",
        }
    )
    with pytest.raises(BridgeError) as excinfo:
        _install_archive(
            blob,
            _plan(tmp_path, "https://registry.npmmirror.com/x/-/x-1.0.0.tgz"),
            shape=_KoishiShape(),
            max_extract_bytes=10_000_000,
            max_files=500,
        )
    assert getattr(excinfo.value, "code", "") == "path_traversal"


def test_tgz_install_rejects_symlink_members(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo("package/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        archive.addfile(info)
    with pytest.raises(BridgeError) as excinfo:
        _install_archive(
            buffer.getvalue(),
            _plan(tmp_path, "https://registry.npmmirror.com/x/-/x-1.0.0.tgz"),
            shape=_KoishiShape(),
            max_extract_bytes=10_000_000,
            max_files=500,
        )
    assert "链接" in str(excinfo.value)


def test_tgz_install_rejects_oversize(tmp_path: Path) -> None:
    blob = _make_tgz({"package/big.bin": "x" * 4096})
    with pytest.raises(BridgeError) as excinfo:
        _install_archive(
            blob,
            _plan(tmp_path, "https://registry.npmmirror.com/x/-/x-1.0.0.tgz"),
            shape=_KoishiShape(),
            max_extract_bytes=64,
            max_files=500,
        )
    assert getattr(excinfo.value, "code", "") == "extract_too_large"


def test_koishi_shape_requires_package_json(tmp_path: Path) -> None:
    shape = _KoishiShape()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert shape.matches(empty) is False
    with_pkg = tmp_path / "with-pkg"
    with_pkg.mkdir()
    (with_pkg / "package.json").write_text("{}", encoding="utf-8")
    assert shape.matches(with_pkg) is True
