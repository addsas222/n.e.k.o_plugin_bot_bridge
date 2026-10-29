"""插件市场（``bridge/market.py``）的针对性测试。

全部离线、确定性、无网络、无 sleep：

* 索引解析：三个源各自的真实索引切片（``tests/fixtures/market/*.json``）+ 形态不符必须报错；
* 索引缓存：内存 → 磁盘 → 重新拉取，损坏缓存要忽略并告警；
* 安装编排：候选 URL 顺序、镜像前缀重试、主机白名单、体积/文件数上限；
* **解压安全边界**：``..`` / 绝对路径 / 符号链接一律拒绝，且 plugins_root 里不留半个插件；
* 各种失败路径（未知 id、PyPI 条目、已存在目录、越界目标、市场停用）都必须是结构化失败。

需要 ``HostServices`` 的那一小撮能力（``settings`` / ``log`` / ``cache_dir`` / ``plugins_root``），
宿主用下面的 :class:`FakeHost` 顶上；网络用 :class:`FakeFetcher`（或替换
``market._urlopen_bytes``）顶上。
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Mapping, Sequence
import tempfile
from pathlib import Path

import pytest

import bridge.market as market_mod
from bridge.errors import BridgeError
from bridge.market import (
    AstrBotSource,
    InstallPlan,
    KoishiSource,
    MaiBotSource,
    Market,
    MarketEntry,
    MarketError,
    NoneBotSource,
    build_sources,
    default_fetcher,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "market"

MAIBOT_INDEX = MaiBotSource().index_url
NONEBOT_INDEX = NoneBotSource().index_url
ASTRBOT_INDEX = AstrBotSource().index_url
KOISHI_INDEX = KoishiSource().index_url

#: 夹具里几条稳定可用的条目 id。
MAIBOT_RICH = "SengokuCola.Mute-Plugin"
MAIBOT_LEGACY = "A0000Xz.MaiBot-Tarots-Plugin"
MAIBOT_NO_REPO = "ZmasCloud.makeplugin-0.6"
NONEBOT_PACKAGE = "nonebot_plugin_status"
NONEBOT_INVALID = "haruka_bot"
NONEBOT_PYPI = "ayaka_games"
ASTRBOT_COLLECTION_ID = "astrbot-plugin-get-px"
ASTRBOT_MARKET_ID = "Sham1k0/astrbot_plugin_get_px"


# --------------------------------------------------------------------------- #
# 夹具 / 替身
# --------------------------------------------------------------------------- #


def load_fixture(name: str) -> object:
    """读一份真实索引切片。"""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def index_payloads() -> dict[str, object]:
    """四个默认源的索引内容（用真实切片）。"""
    return {
        MAIBOT_INDEX: load_fixture("maibot_plugin_details.json"),
        NONEBOT_INDEX: load_fixture("nonebot_plugins.json"),
        ASTRBOT_INDEX: load_fixture("astrbot_collection.json"),
        KOISHI_INDEX: load_fixture("koishi_search.json"),
    }


def make_zip(entries: Mapping[str, str | bytes], *, symlinks: Sequence[str] = ()) -> bytes:
    """按 ``{成员名: 内容}`` 造一个内存 zip；``symlinks`` 里的是符号链接成员。"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
        for name in symlinks:
            info = zipfile.ZipInfo(name)
            info.external_attr = 0o120777 << 16  # symlink 模式位
            archive.writestr(info, "target")
    return buffer.getvalue()


def maibot_zip(*, wrapper: str = "MutePlugin-main") -> bytes:
    """一个最小但合法的 MaiBot 插件归档（GitHub 布局：顶层 ``<repo>-<分支>/``）。"""
    return make_zip(
        {
            f"{wrapper}/_manifest.json": json.dumps({"manifest_version": 2, "name": "Mute"}),
            f"{wrapper}/plugin.py": "print('mute')\n",
            f"{wrapper}/sub/mod.py": "VALUE = 1\n",
            f"{wrapper}/__pycache__/mod.cpython-311.pyc": b"\x00\x01",
            f"{wrapper}/.git/config": "[core]\n",
        }
    )


class FakeHost:
    """市场用到的那几个宿主入口（不是完整的 ``HostServices`` 实现）。"""

    def __init__(
        self,
        root: Path,
        settings: Mapping[str, object] | None = None,
        *,
        with_plugins_root: bool = True,
        log_raises: bool = False,
    ) -> None:
        self.root = root
        if with_plugins_root:
            self.plugins_root = root / "plugins"
        self.settings: dict[str, object] = {"market": dict(settings or {})}
        self.logs: list[tuple[str, str]] = []
        self.log_raises = log_raises

    def log(self, level: str, message: str, *, source: str = "") -> None:
        if self.log_raises:
            raise RuntimeError("日志实现坏了")
        self.logs.append((level, message))

    def text(self) -> str:
        return "\n".join(message for _level, message in self.logs)

    def data_dir(self, plugin_id: str = "") -> Path:
        path = self.root / "data" / plugin_id if plugin_id else self.root / "data"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def cache_dir(self, plugin_id: str = "") -> Path:
        path = self.root / "cache" / plugin_id
        path.mkdir(parents=True, exist_ok=True)
        return path


class FakeFetcher:
    """内存版抓取器：``json_map`` / ``byte_map`` 里没有的 URL 就当 404。"""

    def __init__(self, *, json_map: Mapping[str, object] | None = None, byte_map: Mapping[str, bytes] | None = None) -> None:
        self.json_map: dict[str, object] = dict(json_map or {})
        self.byte_map: dict[str, bytes] = dict(byte_map or {})
        self.json_calls: list[str] = []
        self.byte_calls: list[str] = []

    async def get_bytes(self, url: str, *, timeout: float, max_bytes: int) -> bytes:
        self.byte_calls.append(url)
        if url not in self.byte_map:
            raise MarketError(f"HTTP 404（假抓取器）：{url}")
        return self.byte_map[url]

    async def get_json(self, url: str, *, timeout: float) -> object:
        self.json_calls.append(url)
        if url not in self.json_map:
            raise MarketError(f"HTTP 404（假抓取器）：{url}")
        return self.json_map[url]


def nonebot_wheel() -> bytes:
    """最小可用的 NoneBot wheel：包目录 + dist-info + requirements.txt。"""
    return make_zip(
        {
            f"{NONEBOT_PYPI}/__init__.py": (
                'from nonebot import on_command\n\nayaka = on_command("ayaka")\n'
            ),
            f"{NONEBOT_PYPI}-1.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: ayaka-games\nVersion: 1.0\n"
            ),
            "README.md": "wheel fixture\n",
        }
    )


def build_market(
    tmp_path: Path,
    *,
    settings: Mapping[str, object] | None = None,
    fetcher: FakeFetcher | None = None,
    sources: Sequence[market_mod.MarketSource] | None = None,
    load_index: bool = True,
    **host_kwargs: object,
) -> tuple[Market, FakeHost, FakeFetcher]:
    """造一个「真 Market + 假宿主 + 假网络」的组合。"""
    host = FakeHost(tmp_path / "host", settings, **host_kwargs)  # type: ignore[arg-type]
    fake = fetcher if fetcher is not None else FakeFetcher()
    if load_index:
        for url, payload in index_payloads().items():
            fake.json_map.setdefault(url, payload)
    market = Market(host, list(sources) if sources is not None else build_sources()[0], fetcher=fake)
    return market, host, fake


def add_zip(fake: FakeFetcher, url: str, entries: Mapping[str, str | bytes], *, symlinks: Sequence[str] = ()) -> None:
    fake.byte_map[url] = make_zip(entries, symlinks=symlinks)


# --------------------------------------------------------------------------- #
# 索引解析
# --------------------------------------------------------------------------- #


def test_maibot_parse_fixture() -> None:
    entries = MaiBotSource().parse(load_fixture("maibot_plugin_details.json"))
    assert [entry.plugin_id for entry in entries] == [MAIBOT_RICH, MAIBOT_LEGACY, MAIBOT_NO_REPO]
    rich = entries[0]
    assert rich.source == "maibot"
    assert rich.repository == "https://github.com/SengokuCola/MutePlugin"
    assert rich.install_kind == "github-zip"
    assert rich.author == "SengokuCola"
    assert rich.version == "4.7.0"
    assert rich.description  # 索引里本来就有描述
    assert rich.raw["id"] == MAIBOT_RICH
    # 旧版布局的 repository_url 也要认
    assert entries[1].repository.startswith("https://github.com/")
    assert entries[1].install_kind == "github-zip"
    # 既没有 urls.repository 也没有 repository_url：老实标成装不了
    assert entries[2].repository == ""
    assert entries[2].install_kind == "unsupported"


def test_nonebot_parse_fixture() -> None:
    entries = NoneBotSource().parse(load_fixture("nonebot_plugins.json"))
    by_id = {entry.plugin_id: entry for entry in entries}
    assert set(by_id) == {NONEBOT_PACKAGE, NONEBOT_INVALID, NONEBOT_PYPI}
    # 仓库名与插件名对得上 → 用仓库压缩包装
    assert by_id[NONEBOT_PACKAGE].install_kind == "github-zip"
    assert by_id[NONEBOT_PACKAGE].repository == "https://github.com/nonebot/plugin-status"
    # homepage 不是 GitHub（只有 PyPI 包）→ 装不了，如实标成 pypi
    assert by_id[NONEBOT_PYPI].install_kind == "pypi-wheel"  # 有 PyPI 包名的条目走 wheel 安装
    assert by_id[NONEBOT_PYPI].repository == ""
    assert by_id[NONEBOT_PYPI].raw["project_link"] == "ayaka-games"
    # 索引里的 valid=false 要带进 raw（安装时作为告警）
    assert by_id[NONEBOT_INVALID].raw["valid"] is False


def test_astrbot_parse_collection_fixture() -> None:
    entries = AstrBotSource().parse(load_fixture("astrbot_collection.json"))
    assert [entry.plugin_id for entry in entries] == [
        ASTRBOT_COLLECTION_ID,
        "astrbot-plugin-codeforces-helper",
        "astrbot_plugin_yunsdf",
    ]
    first = entries[0]
    assert first.source == "astrbot"
    assert first.repository == "https://github.com/shitianyaa/astrbot_plugin_get_px"
    assert first.install_kind == "github-zip"
    assert first.download_url == ""  # 这份索引没有直链
    assert first.author == "shitianyaa"
    assert first.name  # display_name
    # 真实索引里就有一条「repo 写成了 https://github.com/WhiteCloudOL/」的坏数据：
    # 它必须被标成装不了，但**不能**让整个索引解析失败。
    broken = entries[2]
    assert broken.repository == ""
    assert broken.install_kind == "unsupported"
    assert broken.raw["repo"] == "https://github.com/WhiteCloudOL/"


def test_astrbot_parse_market_fixture_skips_meta() -> None:
    entries = AstrBotSource().parse(load_fixture("astrbot_market.json"))
    ids = [entry.plugin_id for entry in entries]
    assert ASTRBOT_MARKET_ID in ids
    assert all(not entry.plugin_id.startswith("$") for entry in entries)
    first = entries[0]
    assert first.plugin_id == ASTRBOT_MARKET_ID
    assert first.download_url.startswith("https://")
    assert first.version


def test_parse_empty_index_is_allowed() -> None:
    """结构合法但空的索引就是 ``[]``，绝不凭空造条目。"""
    assert MaiBotSource().parse([]) == []
    assert NoneBotSource().parse([]) == []
    assert AstrBotSource().parse({}) == []


@pytest.mark.parametrize(
    "source, payload, expected",
    [
        (MaiBotSource(), {"entries": []}, "顶层是 list"),
        (NoneBotSource(), [{"name": "没有 module_name"}], "module_name"),
        (AstrBotSource(), [{"repo": "https://github.com/a/b"}], "期望 object"),
        (AstrBotSource(), {"x": ["不是一个对象"]}, "期望 object"),
    ],
)
def test_parse_shape_mismatch_raises_market_error(source: object, payload: object, expected: str) -> None:
    """形态不符必须抛出「期望什么 / 实际什么」，不能默默变成空列表。"""
    with pytest.raises(MarketError) as excinfo:
        source.parse(payload)  # type: ignore[attr-defined]
    message = str(excinfo.value)
    assert expected in message
    assert isinstance(excinfo.value, BridgeError)
    assert excinfo.value.code == "market_error"


def test_parse_reports_position_and_keys() -> None:
    with pytest.raises(MarketError) as excinfo:
        MaiBotSource().parse([{"id": "a.b", "manifest": {"name": "x"}}, {"manifest": {}}])
    message = str(excinfo.value)
    assert "第 2 项" in message and "manifest" in message


# --------------------------------------------------------------------------- #
# 搜索
# --------------------------------------------------------------------------- #


async def test_search_filters_matching_and_limit(tmp_path: Path) -> None:
    market, _host, fake = build_market(tmp_path)
    everything = await market.search("", limit=0)
    assert sorted(entry.source for entry in everything) == (
        ["astrbot"] * 3 + ["koishi"] * 3 + ["maibot"] * 3 + ["nonebot"] * 3
    )

    assert [entry.plugin_id for entry in await market.search("禁言")] == [MAIBOT_RICH]
    assert [entry.plugin_id for entry in await market.search("MUTE-PLUGIN")] == [MAIBOT_RICH]  # 大小写不敏感
    assert [entry.plugin_id for entry in await market.search("shitianyaa")] == [ASTRBOT_COLLECTION_ID]  # 命中作者
    assert await market.search("不存在的关键词xyz") == []

    only_nonebot = await market.search("", source="nonebot", limit=0)
    assert {entry.source for entry in only_nonebot} == {"nonebot"}

    # 第二次搜索走内存缓存，不再打网络
    calls = len(fake.json_calls)
    await market.search("", limit=0)
    assert len(fake.json_calls) == calls


async def test_search_cross_source_is_round_robin(tmp_path: Path) -> None:
    """跨源搜索按源轮流取：单个源不能吃光 limit（修之前 limit=20 的结果全是排在最前的 maibot）。"""
    market, _host, _fake = build_market(tmp_path)
    everything = await market.search("", limit=0)
    assert [entry.source for entry in everything] == [
        "maibot", "nonebot", "astrbot", "koishi"
    ] * 3

    two = await market.search("", limit=2)
    assert [entry.source for entry in two] == ["maibot", "nonebot"]  # 不再是「前两页全是 maibot」

    three = await market.search("", limit=3)
    assert {entry.source for entry in three} == {"maibot", "nonebot", "astrbot"}
    four = await market.search("", limit=4)
    assert {entry.source for entry in four} == {
        "maibot", "nonebot", "astrbot", "koishi"
    }

    # 只搜一个源时不轮转，顺序保持索引顺序
    one = await market.search("", source="nonebot", limit=0)
    assert [entry.source for entry in one] == ["nonebot"] * 3


async def test_search_report_counts_truncation_and_failures(tmp_path: Path) -> None:
    market, _host, fake = build_market(tmp_path)
    report = await market.search_report("", limit=0)
    assert report.counts == {"maibot": 3, "nonebot": 3, "astrbot": 3, "koishi": 3}
    assert report.truncated == {}
    assert report.failures == {}

    capped, _host2, _fake2 = build_market(tmp_path / "capped", settings={"max_entries_per_source": 1})
    capped_report = await capped.search_report("", limit=0)
    assert capped_report.counts == {
        "maibot": 3,
        "nonebot": 3,
        "astrbot": 3,
        "koishi": 3,
    }  # 计数是截断前的全量
    assert capped_report.truncated == {"maibot": 2, "nonebot": 2, "astrbot": 2, "koishi": 2}
    assert len(capped_report.entries) == 4

    # 一个源挂掉：其余源照常返回，失败原因结构化带出（不是整次搜索失败）
    dead = FakeFetcher()
    dead.json_map[MAIBOT_INDEX] = load_fixture("maibot_plugin_details.json")
    dead_market, _host3, _fake3 = build_market(tmp_path / "dead", fetcher=dead, load_index=False)
    dead_report = await dead_market.search_report("", limit=0)
    assert set(dead_report.counts) == {"maibot"}
    assert set(dead_report.failures) == {"nonebot", "astrbot", "koishi"}
    assert all(reason for reason in dead_report.failures.values())


async def test_search_unknown_source_raises(tmp_path: Path) -> None:
    market, _host, _fake = build_market(tmp_path)
    with pytest.raises(MarketError) as excinfo:
        await market.search("x", source="not-a-source")
    assert "未知的市场源" in str(excinfo.value)
    assert "maibot" in str(excinfo.value)


async def test_search_raises_when_every_selected_source_fails(tmp_path: Path) -> None:
    market, _host, _fake = build_market(tmp_path, sources=[NoneBotSource()], load_index=False)
    with pytest.raises(MarketError) as excinfo:
        await market.search("x")
    assert "所有市场源都拉不到索引" in str(excinfo.value)


async def test_search_skips_dead_source_when_others_work(tmp_path: Path) -> None:
    fetcher = FakeFetcher()
    fetcher.json_map[MAIBOT_INDEX] = load_fixture("maibot_plugin_details.json")
    market, host, _fake = build_market(tmp_path, fetcher=fetcher, load_index=False)
    results = await market.search("", limit=0)
    assert {entry.source for entry in results} == {"maibot"}
    assert "nonebot" in market.status()["source_errors"]
    assert any("索引不可用" in message for _level, message in host.logs)


async def test_search_caps_matches_per_source_without_hiding_the_index(tmp_path: Path) -> None:
    """``max_entries_per_source`` 只截单次返回，索引本身整份保留（不然大商店会凭空少一半）。"""
    market, host, _fake = build_market(tmp_path, settings={"max_entries_per_source": 1})
    results = await market.search("", limit=0)
    assert len(results) == 4  # 每个源只取 1 条
    assert any("每次只取前 1 条" in message for _level, message in host.logs)

    rows = await market.sources_status()
    assert [row["entries"] for row in rows] == [3, 3, 3, 3]  # 索引没有被截断
    assert [entry.plugin_id for entry in await market.search("禁言")] == [MAIBOT_RICH]


async def test_sources_status_shape_and_reasons(tmp_path: Path) -> None:
    fetcher = FakeFetcher(json_map=index_payloads())
    fetcher.json_map.pop(NONEBOT_INDEX)
    market, _host, _fake = build_market(tmp_path, fetcher=fetcher, load_index=False)

    # 默认形态：**不联网**（冷启动时不知道条目数，只报配置），状态查询必须是瞬时的。
    cold = await market.sources_status()
    assert [row["name"] for row in cold] == ["maibot", "nonebot", "astrbot", "koishi"]
    assert all(row["entries"] is None and row["stale"] is True for row in cold), cold
    assert fetcher.json_calls == [], "默认状态查询不该联网"

    rows = await market.sources_status(refresh=True)
    assert [row["name"] for row in rows] == ["maibot", "nonebot", "astrbot", "koishi"]
    for row in rows:
        assert {
            "name", "display_name", "index_url", "ok", "entries", "cached",
            "fetched_at", "error",
        } <= set(row)
        assert set(row) == {
            "name",
            "display_name",
            "index_url",
            "ok",
            "entries",
            "cached",
            "fetched_at",
            "error",
            "stale",
            "note",
            "age_seconds",
        }
        assert row["display_name"] and row["index_url"].startswith("https://")
    by_name = {row["name"]: row for row in rows}
    assert by_name["maibot"]["ok"] is True and by_name["maibot"]["entries"] == 3
    assert by_name["maibot"]["stale"] is False
    assert by_name["nonebot"]["ok"] is False and by_name["nonebot"]["error"]
    # 坏源只是「没有条目」，不去猜数字（不假装成功，也不编造 0 条这种噪声）。
    assert by_name["nonebot"]["entries"] is None


def test_market_status_shape(tmp_path: Path) -> None:
    host = FakeHost(tmp_path / "host", {"max_archive_bytes": 2048})
    market = Market(host, [MaiBotSource()])  # fetcher=None → 默认抓取器
    status = market.status()
    assert status["enabled"] is True and status["allow_install"] is True
    assert status["sources"] == ["maibot"]
    assert Path(str(status["plugins_root"])) == host.plugins_root
    assert "github.com" in status["allow_hosts"]
    assert status["mirror_prefixes"] == ["https://ghproxy.net/"]
    assert status["max_archive_bytes"] == 2048
    assert status["max_extract_bytes"] == 2048 * 4
    assert status["cache_ttl_seconds"] == 600.0
    assert status["max_entries_per_source"] == 500
    assert status["source_errors"] == {}
    assert Path(str(status["cache_dir"])) == host.cache_dir("market")


def test_market_drops_duplicate_sources(tmp_path: Path) -> None:
    host = FakeHost(tmp_path / "host")
    market = Market(host, [MaiBotSource(), MaiBotSource(), NoneBotSource()], fetcher=FakeFetcher())
    assert market.status()["sources"] == ["maibot", "nonebot"]
    assert market.status()["skipped_sources"] == ["maibot"]


def test_build_sources_reports_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    sources, errors = build_sources()
    assert [source.name for source in sources] == [
        "maibot",
        "nonebot",
        "astrbot",
        "koishi",
    ]
    assert errors == {}

    sources, errors = build_sources(["NONEBOT"])
    assert [source.name for source in sources] == ["nonebot"]
    assert errors == {}

    sources, errors = build_sources(["maibot", "不存在"])
    assert [source.name for source in sources] == ["maibot"]
    assert "未知的市场源" in errors["不存在"]

    class BrokenSource:
        name = "broken"
        display_name = "配不出来的源"
        index_url = ""

        def parse(self, payload: object) -> list[MarketEntry]:
            return []

        def plan_install(self, entry: MarketEntry, plugins_root: Path, *, overwrite: bool = False) -> InstallPlan:
            return InstallPlan(entry=entry, target_dir=Path(plugins_root) / "broken" / "x")

    monkeypatch.setitem(market_mod._SOURCE_FACTORIES, "broken", BrokenSource)
    sources, errors = build_sources(["broken"])
    assert sources == []
    assert "索引地址不可用" in errors["broken"]

    def boom() -> None:
        raise RuntimeError("构造就炸")

    monkeypatch.setitem(market_mod._SOURCE_FACTORIES, "boom", boom)
    sources, errors = build_sources(["boom"])
    assert sources == []
    assert "构造就炸" in errors["boom"]

    sources, errors = build_sources("astrbot")  # type: ignore[arg-type]
    assert [source.name for source in sources] == ["astrbot"]


# --------------------------------------------------------------------------- #
# 安装：成功路径
# --------------------------------------------------------------------------- #


async def test_install_happy_path_strips_prefix_and_writes_provenance(tmp_path: Path) -> None:
    market, host, fake = build_market(tmp_path)
    fake.byte_map["https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"] = maibot_zip()

    result = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert result.ok is True, result.error
    assert result.error == ""
    assert result.target_dir == host.plugins_root / "maibot" / "MutePlugin"
    assert result.url.endswith("/archive/refs/heads/main.zip")
    assert len(result.archive_sha256) == 64
    assert result.files == 3  # _manifest.json / plugin.py / sub/mod.py

    # 顶层 <repo>-main/ 被剥掉了：目标里直接就是插件内容
    assert (result.target_dir / "_manifest.json").is_file()
    assert (result.target_dir / "plugin.py").is_file()
    assert (result.target_dir / "sub" / "mod.py").is_file()
    # 垃圾成员不进目标
    assert not (result.target_dir / "__pycache__").exists()
    assert not (result.target_dir / ".git").exists()
    assert not list(result.target_dir.rglob("*.pyc"))

    provenance = json.loads((result.target_dir / ".bot_bridge_market.json").read_text(encoding="utf-8"))
    assert set(provenance) == {
        "source",
        "plugin_id",
        "name",
        "version",
        "url",
        "archive_sha256",
        "installed_at",
        "install_kind",
        "requires",
    }
    assert provenance["source"] == "maibot"
    assert provenance["plugin_id"] == MAIBOT_RICH
    assert provenance["version"] == "4.7.0"
    assert provenance["archive_sha256"] == result.archive_sha256
    assert provenance["installed_at"].startswith("20")

    # 成功日志里有目标目录；市场自己不重载注册表
    assert any(str(result.target_dir) in message for _level, message in host.logs)
    # 暂存目录清干净了
    assert list((host.plugins_root / "maibot").glob(".market-*")) == []


async def test_install_falls_back_to_next_candidate_url(tmp_path: Path) -> None:
    """第一个候选地址 404 → 用第二个；第一个成功就不再试后面的。"""
    market, host, fake = build_market(tmp_path)
    master = f"https://github.com/SengokuCola/MutePlugin/archive/refs/heads/master.zip"
    fake.byte_map[master] = maibot_zip()
    result = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert result.ok is True, result.error
    assert result.url == master
    assert len(fake.byte_calls) == 2  # main 失败后才试 master
    assert fake.byte_calls[-1] == master
    assert host.plugins_root.joinpath("maibot", "MutePlugin").is_dir()


async def test_install_nonebot_descends_into_package_dir(tmp_path: Path) -> None:
    """NoneBot 归档的插件包在仓库根下面一层：要装的是那个包，而不是仓库根。"""
    market, host, fake = build_market(tmp_path)
    url = f"https://github.com/nonebot/plugin-status/archive/refs/heads/master.zip"
    add_zip(
        fake,
        url,
        {
            "plugin-status-master/pyproject.toml": "[project]\n",
            "plugin-status-master/README.md": "hi\n",
            "plugin-status-master/nonebot_plugin_status/__init__.py": "from .config import *\n",
            "plugin-status-master/nonebot_plugin_status/config.py": "KEY = 'x'\n",
        },
    )
    result = await market.install(source="nonebot", plugin_id=NONEBOT_PACKAGE)
    assert result.ok is True, result.error
    assert result.target_dir == host.plugins_root / "nonebot" / "nonebot_plugin_status"
    assert (result.target_dir / "__init__.py").is_file()
    assert (result.target_dir / "config.py").is_file()
    assert not (result.target_dir / "pyproject.toml").exists()  # 仓库根没被搬进来
    assert result.files == 2


async def test_install_astrbot_collection_entry_uses_repo_zip(tmp_path: Path) -> None:
    market, host, fake = build_market(tmp_path)
    url = "https://github.com/shitianyaa/astrbot_plugin_get_px/archive/refs/heads/main.zip"
    add_zip(
        fake,
        url,
        {
            "astrbot_plugin_get_px-main/metadata.yaml": "name: astrbot_plugin_get_px\n",
            "astrbot_plugin_get_px-main/main.py": "print('px')\n",
        },
    )
    result = await market.install(source="astrbot", plugin_id=ASTRBOT_COLLECTION_ID)
    assert result.ok is True, result.error
    assert result.target_dir == host.plugins_root / "astrbot" / "astrbot_plugin_get_px"
    assert (result.target_dir / "metadata.yaml").is_file()
    assert result.files == 2


async def test_install_astrbot_market_entry_prefers_download_url(tmp_path: Path) -> None:
    """带直链的索引：先用直链（布局未知，靠「找插件目录」兜底），失败才回退仓库压缩包。"""
    fetcher = FakeFetcher()
    for url, payload in index_payloads().items():
        fetcher.json_map[url] = payload
    fetcher.json_map[ASTRBOT_INDEX] = load_fixture("astrbot_market.json")
    market, host, fake = build_market(tmp_path, fetcher=fetcher, load_index=False)
    entry = (await market.search("", source="astrbot", limit=0))[0]
    assert entry.download_url.startswith("https://")
    add_zip(
        fake,
        entry.download_url,
        {
            "astrbot_plugin_get_px-3.8.0/metadata.yaml": "name: astrbot_plugin_get_px\n",
            "astrbot_plugin_get_px-3.8.0/main.py": "print('px')\n",
        },
    )
    result = await market.install(source="astrbot", plugin_id=entry.plugin_id)
    assert result.ok is True, result.error
    assert result.url == entry.download_url
    assert fake.byte_calls == [entry.download_url]
    assert (result.target_dir / "metadata.yaml").is_file()
    assert result.files == 2


async def test_install_logs_plan_warnings_for_invalid_entry(tmp_path: Path) -> None:
    """索引说 valid=false 的条目也能装，但计划里的告警必须进日志。"""
    market, host, fake = build_market(tmp_path)
    url = "https://github.com/SK-415/HarukaBot/archive/refs/heads/main.zip"
    add_zip(
        fake,
        url,
        {
            "HarukaBot-main/haruka_bot/__init__.py": "from .bot import *\n",
            "HarukaBot-main/haruka_bot/bot.py": "BOT = 1\n",
            "HarukaBot-main/pyproject.toml": "[project]\n",
        },
    )
    result = await market.install(source="nonebot", plugin_id=NONEBOT_INVALID)
    assert result.ok is True, result.error
    assert (result.target_dir / "__init__.py").is_file()
    assert result.files == 2
    assert any("valid=false" in message for _level, message in host.logs)
    assert any(_level == "info" and "提示" in message for _level, message in host.logs)


async def test_install_nonebot_single_file_plugin_fails_actionably(tmp_path: Path) -> None:
    """单文件 NoneBot 插件装不了（发现层只认包目录），要给出可执行的出路。"""
    market, host, fake = build_market(tmp_path)
    url = "https://github.com/SK-415/HarukaBot/archive/refs/heads/main.zip"
    add_zip(fake, url, {"HarukaBot-main/haruka_bot.py": "print('x')\n"})
    result = await market.install(source="nonebot", plugin_id=NONEBOT_INVALID)
    assert result.ok is False
    assert "单文件插件" in result.error
    assert "haruka_bot.py" in result.error
    assert not result.target_dir.exists()
    assert list(host.plugins_root.rglob("*.py")) == []


# --------------------------------------------------------------------------- #
# 安装：安全边界
# --------------------------------------------------------------------------- #


async def test_install_rejects_malicious_zips_and_leaves_nothing(tmp_path: Path) -> None:
    cases = {
        "parent-escape": make_zip(
            {"MutePlugin-main/_manifest.json": "{}", "MutePlugin-main/../escape.py": "boom"}
        ),
        "absolute-path": make_zip(
            {"MutePlugin-main/_manifest.json": "{}", "/abs.py": "boom"}
        ),
        "symlink": make_zip(
            {"MutePlugin-main/_manifest.json": "{}"}, symlinks=["MutePlugin-main/link"]
        ),
    }
    for label, payload in cases.items():
        market, host, fake = build_market(tmp_path / label)
        url = "https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"
        fake.byte_map[url] = payload
        result = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
        assert result.ok is False, label
        assert result.error, label
        assert not result.target_dir.exists(), label
        framework_dir = host.plugins_root / "maibot"
        assert list(framework_dir.glob(".market-*")) == [], label
        assert not (host.plugins_root / "escape.py").exists(), label
        assert not (host.plugins_root / "abs.py").exists(), label
        assert list(host.plugins_root.rglob("*.py")) == [], label
        assert not (tmp_path / label / "escape.py").exists(), label


async def test_install_rejects_non_zip_payload(tmp_path: Path) -> None:
    market, host, fake = build_market(tmp_path)
    fake.byte_map[f"https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"] = b"<html>404</html>"
    result = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert result.ok is False
    assert "不是合法 zip" in result.error
    assert list(host.plugins_root.rglob("*.py")) == []


async def test_install_enforces_file_count_and_size_caps(tmp_path: Path) -> None:
    market, host, fake = build_market(tmp_path, settings={"max_files": 2})
    url = "https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"
    add_zip(
        fake,
        url,
        {
            "MutePlugin-main/_manifest.json": "{}",
            "MutePlugin-main/plugin.py": "print(1)\n",
            "MutePlugin-main/extra.py": "print(2)\n",
        },
    )
    too_many = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert too_many.ok is False
    assert "文件数超过上限" in too_many.error
    assert list(host.plugins_root.glob("*/MutePlugin")) == []

    market2, host2, fake2 = build_market(tmp_path / "second", settings={"max_extract_bytes": 1024})
    url2 = "https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"
    add_zip(
        fake2,
        url2,
        {
            "MutePlugin-main/_manifest.json": "{}",
            "MutePlugin-main/plugin.py": "x" * 4096,
        },
    )
    too_big = await market2.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert too_big.ok is False
    assert "体积超过上限" in too_big.error
    assert list(host2.plugins_root.glob("*/MutePlugin")) == []


async def test_install_rejects_target_outside_plugins_root(tmp_path: Path) -> None:
    """自定义源给越界的 target_dir：必须拒装，且不落任何文件。"""

    class OutsideSource:
        name = "outside"
        display_name = "越界源"
        index_url = "https://github.com/example/index.json"

        def parse(self, payload: object) -> list[MarketEntry]:
            return [
                MarketEntry(
                    source=self.name,
                    plugin_id="evil",
                    install_kind="github-zip",
                    repository="https://github.com/example/evil",
                )
            ]

        def plan_install(self, entry: MarketEntry, plugins_root: Path, *, overwrite: bool = False) -> InstallPlan:
            return InstallPlan(
                entry=entry,
                target_dir=Path(plugins_root).parent.parent / "outside",
                urls=("https://github.com/example/evil/archive/refs/heads/main.zip",),
            )

    host = FakeHost(tmp_path / "host")
    fetcher = FakeFetcher(json_map={"https://github.com/example/index.json": OutsideSource().parse(None)})
    market = Market(host, [OutsideSource()], fetcher=fetcher)
    result = await market.install(source="outside", plugin_id="evil")
    assert result.ok is False
    assert "plugins_root 外面" in result.error
    assert not (tmp_path / "host" / "outside").exists()
    assert fetcher.byte_calls == []  # 计划就被拒了，压根不该下载
    assert list(host.plugins_root.rglob("*")) == []


async def test_install_rejects_non_allowlisted_download_host(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """索引在白名单内、下载地址在白名单外：拒绝，并点名那个主机；而且**不发请求**。"""
    seen: list[str] = []

    def fake_urlopen(
        url: str, timeout: float, max_bytes: int, user_agent: str, *, opener: object = None
    ) -> bytes:
        seen.append(url)
        payload = {"id": "evil", "manifest": {"name": "e", "urls": {"repository": "https://evil.example/repo"}}}
        return json.dumps([payload]).encode("utf-8")

    monkeypatch.setattr(market_mod, "_urlopen_bytes", fake_urlopen)

    class OutsideArchiveSource:
        name = "outside"
        display_name = "越界源"
        index_url = "https://github.com/example/index.json"

        def parse(self, payload: object) -> list[MarketEntry]:
            return [
                MarketEntry(
                    source=self.name,
                    plugin_id="evil",
                    name="evil",
                    install_kind="github-zip",
                    repository="https://evil.example/repo",
                )
            ]

        def plan_install(self, entry: MarketEntry, plugins_root: Path, *, overwrite: bool = False) -> InstallPlan:
            return InstallPlan(
                entry=entry,
                target_dir=Path(plugins_root) / self.name / "evil",
                urls=("https://evil.example/repo/archive/refs/heads/main.zip",),
            )

    host = FakeHost(tmp_path / "host")
    market = Market(host, [OutsideArchiveSource()])  # 默认抓取器
    result = await market.install(source="outside", plugin_id="evil")
    assert result.ok is False
    assert "evil.example" in result.error
    assert "白名单" in result.error
    assert seen == ["https://github.com/example/index.json"]  # 只拉了索引
    assert list(host.plugins_root.rglob("*")) == []


async def test_default_fetcher_host_and_scheme_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(
        url: str, timeout: float, max_bytes: int, user_agent: str, *, opener: object = None
    ) -> bytes:
        raise AssertionError(f"不该联网：{url}")

    monkeypatch.setattr(market_mod, "_urlopen_bytes", explode)
    fetcher = default_fetcher({})
    for bad in ("http://github.com/a/b.zip", "https://evil.example/b.zip", "https:///x.zip"):
        with pytest.raises(MarketError) as excinfo:
            await fetcher.get_bytes(bad, timeout=1.0, max_bytes=10)
        message = str(excinfo.value)
        assert ("只允许 https" in message) or ("白名单" in message) or ("没有主机名" in message)

    broadened = default_fetcher({"allow_hosts": ["example.com"], "mirror_prefixes": []})
    assert "example.com" in broadened.allow_hosts  # type: ignore[attr-defined]
    assert broadened.mirror_prefixes == ()  # type: ignore[attr-defined]
    broadened.check_url("https://example.com/x.zip")  # type: ignore[attr-defined] — 白名单扩展生效
    with pytest.raises(MarketError) as excinfo:
        await broadened.get_bytes("https://github.com/a/b.zip", timeout=1.0, max_bytes=10)
    assert "白名单" not in str(excinfo.value)  # 它过了白名单，是假网络炸的
    assert "下载失败" in str(excinfo.value)


async def test_mirror_prefix_retry_at_install_level(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """直连失败 → 镜像前缀重试成功；安装照常完成。"""
    attempts: list[str] = []
    archive = maibot_zip()

    def fake_urlopen(
        url: str, timeout: float, max_bytes: int, user_agent: str, *, opener: object = None
    ) -> bytes:
        attempts.append(url)
        plain = url[len("https://ghproxy.net/") :] if url.startswith("https://ghproxy.net/") else url
        if plain == MAIBOT_INDEX and url.startswith("https://ghproxy.net/"):
            return json.dumps(load_fixture("maibot_plugin_details.json")).encode("utf-8")
        if url.startswith("https://ghproxy.net/") and plain.endswith("/archive/refs/heads/main.zip"):
            return archive
        raise MarketError("证书校验失败（模拟）")

    monkeypatch.setattr(market_mod, "_urlopen_bytes", fake_urlopen)
    host = FakeHost(tmp_path / "host")
    market = Market(host, [MaiBotSource()], fetcher=default_fetcher({"mirror_prefixes": ["https://ghproxy.net/"]}))
    result = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert result.ok is True, result.error
    assert result.url.endswith("/archive/refs/heads/main.zip")
    assert any(url.startswith("https://ghproxy.net/http") for url in attempts)
    assert (result.target_dir / "_manifest.json").is_file()


async def test_permanent_failure_skips_mirrors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """404 这类重试也没用的失败，不该再往镜像上撞。"""
    attempts: list[str] = []

    def fake_urlopen(
        url: str, timeout: float, max_bytes: int, user_agent: str, *, opener: object = None
    ) -> bytes:
        attempts.append(url)
        raise market_mod._PermanentFetchError("HTTP 404 Not Found")

    monkeypatch.setattr(market_mod, "_urlopen_bytes", fake_urlopen)
    fetcher = default_fetcher({"mirror_prefixes": ["https://ghproxy.net/"]})
    with pytest.raises(MarketError) as excinfo:
        await fetcher.get_bytes("https://github.com/a/b/archive/refs/heads/main.zip", timeout=1.0, max_bytes=10)
    assert attempts == ["https://github.com/a/b/archive/refs/heads/main.zip"]
    assert "404" in str(excinfo.value)


async def test_download_is_capped_by_max_archive_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status = 200
        headers = {"Content-Length": "999999"}

        def read(self, size: int) -> bytes:
            return b"x" * size

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

    class FakeOpener:
        def open(self, request: object, timeout: float | None = None) -> FakeResponse:
            return FakeResponse()

    monkeypatch.setattr(market_mod, "_build_opener", lambda check_url: FakeOpener())
    with pytest.raises(MarketError) as excinfo:
        await default_fetcher({}).get_bytes("https://github.com/a/b.zip", timeout=1.0, max_bytes=64)
    assert "体积超过上限" in str(excinfo.value)


def test_redirects_are_checked_against_the_allowlist() -> None:
    """重定向也是下载路径的一部分：跳到白名单外必须当场拒绝。"""
    fetcher = default_fetcher({})
    opener = getattr(fetcher, "_opener")
    assert any(isinstance(handler, market_mod._AllowlistRedirectHandler) for handler in opener.handlers)

    handler = market_mod._AllowlistRedirectHandler(fetcher.check_url)  # type: ignore[attr-defined]
    request = market_mod.urllib.request.Request("https://github.com/o/r/archive/refs/heads/main.zip")
    with pytest.raises(MarketError) as excinfo:
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/x.zip")
    assert "evil.example" in str(excinfo.value) and "白名单" in str(excinfo.value)
    # 白名单内的跳转（GitHub 归档 → codeload / raw）照旧放行
    assert handler.redirect_request(request, None, 302, "Found", {}, "https://codeload.github.com/o/r/zip/refs/heads/main")
    assert handler.redirect_request(
        request, None, 302, "Found", {}, "https://raw.githubusercontent.com/o/r/main/plugin.py"
    )


# --------------------------------------------------------------------------- #
# 安装：失败路径
# --------------------------------------------------------------------------- #


async def test_install_unknown_plugin_id(tmp_path: Path) -> None:
    market, _host, fake = build_market(tmp_path)
    result = await market.install(source="maibot", plugin_id="nobody.nothing")
    assert result.ok is False
    assert result.error.startswith("not in index")
    assert "bridge_market_search" in result.error
    assert fake.byte_calls == []
    assert result.to_payload()["ok"] is False


async def test_install_unknown_source(tmp_path: Path) -> None:
    market, _host, _fake = build_market(tmp_path)
    result = await market.install(source="nope", plugin_id=MAIBOT_RICH)
    assert result.ok is False
    assert "未知的市场源" in result.error


async def test_install_pypi_entry_uses_wheel_and_reports_deps(tmp_path: Path) -> None:
    """有 PyPI 包名的条目走官方 wheel：装出包目录，并把依赖声明如实带出来。"""
    market, host, fake = build_market(tmp_path)
    fake.json_map["https://pypi.org/pypi/ayaka-games/json"] = {
        "info": {"requires_dist": ['httpx>=0.27 ; extra == "http"', "pydantic"]},
        "urls": [
            {
                "packagetype": "sdist",
                "filename": "ayaka_games-1.0.tar.gz",
                "url": "https://files.pythonhosted.org/packages/ayaka_games-1.0.tar.gz",
            },
            {
                "packagetype": "bdist_wheel",
                "filename": "ayaka_games-1.0-py3-none-any.whl",
                "url": "https://files.pythonhosted.org/packages/ayaka_games-1.0-py3-none-any.whl",
            },
        ],
    }
    fake.byte_map["https://files.pythonhosted.org/packages/ayaka_games-1.0-py3-none-any.whl"] = nonebot_wheel()
    result = await market.install(source="nonebot", plugin_id=NONEBOT_PYPI)
    assert result.ok is True, result.error
    assert result.entry.install_kind == "pypi-wheel"
    assert result.url.endswith("py3-none-any.whl")
    assert "httpx" in result.requires and "pydantic" in result.requires
    payload = result.to_payload()
    assert payload["requires"] == list(result.requires)
    assert payload["code"] == ""

    # 落盘布局必须正好是 loader 认的深度 2：<plugins_root>/<框架>/<模块>/__init__.py
    target = result.target_dir
    assert target.name == NONEBOT_PYPI
    assert target.parent.name == "nonebot"
    assert target.parent.parent == market._plugins_root(), target
    assert (target / "__init__.py").is_file()
    assert not list(target.glob("*.dist-info")), "dist-info 不该留在插件目录里"
    assert not (target / NONEBOT_PYPI).exists(), "wheel 的包目录必须被提上来，不能套一层"

    # 最关键的一条：市场装完，桥接层的 loader 必须真的能认出它（装了却扫不到 = 最糟的假成功）
    from bridge.backends.nonebot import BACKEND as NONEBOT_BACKEND

    assert NONEBOT_BACKEND.detect(target) is not None


async def test_install_github_zip_wrapped_package_is_flattened(tmp_path: Path) -> None:
    """仓库把包目录裹在仓库根里（GitHub zip 常见）：也要落到深度 2 并被 loader 认出。"""
    market, host, fake = build_market(tmp_path)
    fake.json_map[NONEBOT_INDEX] = [
        {
            "module_name": NONEBOT_PYPI,
            "project_link": "ayaka-games",
            "name": "ayaka",
            "desc": "",
            "author": "tester",
            "homepage": "",
            "version": "1.0",
            "is_official": False,
            "valid": True,
        }
    ]
    fake.json_map["https://pypi.org/pypi/ayaka-games/json"] = {
        "info": {"requires_dist": []},
        "urls": [
            {
                "packagetype": "bdist_wheel",
                "filename": f"{NONEBOT_PYPI}-1.0-py3-none-any.whl",
                "url": "https://files.pythonhosted.org/packages/x.whl",
            }
        ],
    }
    fake.byte_map["https://files.pythonhosted.org/packages/x.whl"] = make_zip(
        {
            f"{NONEBOT_PYPI}/__init__.py": 'from nonebot import on_command\n\nayaka = on_command("ayaka")\n',
            f"{NONEBOT_PYPI}/utils.py": "helper = 1\n",
            f"{NONEBOT_PYPI}-1.0.dist-info/METADATA": (
                "Metadata-Version: 2.1\nName: ayaka-games\n\nRequires-Dist: nonebot2 (>=2.0.0b1)\n"
                "Requires-Dist: nonebot-adapter-onebot\n"
            ),
        }
    )
    result = await market.install(source="nonebot", plugin_id=NONEBOT_PYPI)
    assert result.ok is True, result.error
    assert (result.target_dir / "__init__.py").is_file()
    assert (result.target_dir / "utils.py").is_file()
    # requires_dist 为空时回退读 wheel 的 METADATA
    assert result.requires == ("nonebot2", "nonebot-adapter-onebot")
    from bridge.backends.nonebot import BACKEND as NONEBOT_BACKEND

    assert NONEBOT_BACKEND.detect(result.target_dir) is not None


async def test_install_entry_without_repository_fails(tmp_path: Path) -> None:
    market, host, fake = build_market(tmp_path)
    result = await market.install(source="maibot", plugin_id=MAIBOT_NO_REPO)
    assert result.ok is False
    assert "install_kind=unsupported" in result.error
    assert "没有可下载的地址" in result.error
    assert fake.byte_calls == []
    assert list(host.plugins_root.rglob("*")) == []


async def test_install_entry_with_broken_repo_fails_with_the_raw_url(tmp_path: Path) -> None:
    """索引里那条坏 repo 的条目：装不了，但错误里要写明索引到底写了什么。"""
    market, host, fake = build_market(tmp_path)
    result = await market.install(source="astrbot", plugin_id="astrbot_plugin_yunsdf")
    assert result.ok is False
    assert "不是可用的 GitHub 仓库" in result.error
    assert "https://github.com/WhiteCloudOL/" in result.error
    assert str(host.plugins_root) in result.error
    assert fake.byte_calls == []


async def test_install_respects_enabled_and_allow_install(tmp_path: Path) -> None:
    disabled, _host, _fake = build_market(tmp_path / "a", settings={"enabled": False})
    with pytest.raises(MarketError):
        await disabled.search("x")
    result = await disabled.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert result.ok is False and "已停用" in result.error

    locked, _host2, _fake2 = build_market(tmp_path / "b", settings={"allow_install": False})
    result = await locked.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert result.ok is False
    assert "allow_install" in result.error


async def test_install_requires_overwrite_for_existing_target(tmp_path: Path) -> None:
    market, host, fake = build_market(tmp_path)
    url = "https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"
    fake.byte_map[url] = maibot_zip()
    first = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert first.ok is True
    (first.target_dir / "left-over.txt").write_text("old", encoding="utf-8")

    again = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert again.ok is False
    assert "already installed (pass overwrite=true)" in again.error
    assert (first.target_dir / "left-over.txt").is_file()  # 没被动过
    assert len(fake.byte_calls) == 1  # 也没重复下载

    overwritten = await market.install(source="maibot", plugin_id=MAIBOT_RICH, overwrite=True)
    assert overwritten.ok is True, overwritten.error
    assert not (first.target_dir / "left-over.txt").exists()
    assert (first.target_dir / "_manifest.json").is_file()
    assert len(fake.byte_calls) == 2


async def test_overwrite_refuses_target_owned_by_another_plugin(tmp_path: Path) -> None:
    market, host, fake = build_market(tmp_path)
    url = "https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"
    fake.byte_map[url] = maibot_zip()
    first = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert first.ok is True
    provenance = first.target_dir / ".bot_bridge_market.json"
    record = json.loads(provenance.read_text(encoding="utf-8"))
    record["plugin_id"] = "someone.else-Plugin"
    provenance.write_text(json.dumps(record), encoding="utf-8")

    blocked = await market.install(source="maibot", plugin_id=MAIBOT_RICH, overwrite=True)
    assert blocked.ok is False
    assert "someone.else-Plugin" in blocked.error
    assert (first.target_dir / "_manifest.json").is_file()


async def test_install_failure_when_download_fails_for_every_candidate(tmp_path: Path) -> None:
    market, host, fake = build_market(tmp_path)
    result = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert result.ok is False
    assert "4 个下载地址都失败了" in result.error
    assert fake.byte_calls[:4] == [
        "https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip",
        "https://github.com/SengokuCola/MutePlugin/archive/refs/heads/master.zip",
        "https://codeload.github.com/SengokuCola/MutePlugin/zip/refs/heads/main",
        "https://codeload.github.com/SengokuCola/MutePlugin/zip/refs/heads/master",
    ]
    assert list(host.plugins_root.rglob("*")) == []
    assert any("安装失败" in message for _level, message in host.logs)


async def test_install_falls_back_to_data_dir_without_plugins_root(tmp_path: Path) -> None:
    """宿主没暴露 plugins_root 时回落到「数据目录/plugins」。"""
    market, host, fake = build_market(tmp_path, with_plugins_root=False)
    fake.byte_map["https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"] = maibot_zip()
    result = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert result.ok is True, result.error
    assert result.target_dir == host.data_dir("") / "plugins" / "maibot" / "MutePlugin"


async def test_install_without_any_plugins_root_fails_early(tmp_path: Path) -> None:
    """宿主既没有 plugins_root 也没有 data_dir：结构化失败，而且不发任何请求。"""

    class BareHost:
        settings: dict[str, object] = {"market": {}}

        def __init__(self) -> None:
            self.logs: list[tuple[str, str]] = []

        def log(self, level: str, message: str, *, source: str = "") -> None:
            self.logs.append((level, message))

        def cache_dir(self, plugin_id: str = "") -> Path:
            path = tmp_path / "cache" / plugin_id
            path.mkdir(parents=True, exist_ok=True)
            return path

    host = BareHost()
    fetcher = FakeFetcher(json_map=index_payloads())
    market = Market(host, build_sources()[0], fetcher=fetcher)
    result = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert result.ok is False
    assert "插件根目录" in result.error
    assert fetcher.json_calls == [] and fetcher.byte_calls == []
    assert market.status()["plugins_root"] == ""


async def test_install_result_to_payload(tmp_path: Path) -> None:
    market, host, fake = build_market(tmp_path)
    fake.byte_map["https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"] = maibot_zip()
    result = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    payload = result.to_payload()
    assert payload["ok"] is True
    assert payload["source"] == "maibot"
    assert payload["plugin_id"] == MAIBOT_RICH
    assert payload["install_kind"] == "github-zip"
    assert payload["target_dir"] == str(result.target_dir)
    assert payload["files"] == result.files
    assert payload["archive_sha256"] == result.archive_sha256
    assert payload["error"] == ""
    assert payload["code"] == ""
    assert payload["requires"] == list(result.requires)


async def test_log_failures_do_not_break_the_market(tmp_path: Path) -> None:
    """宿主 log 抛异常时安装照旧成功，只把次数记在 status 里（不静默吞掉）。"""
    market, _host, fake = build_market(tmp_path, log_raises=True)
    fake.byte_map["https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"] = maibot_zip()
    result = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert result.ok is True, result.error
    assert market.status()["dropped_logs"] > 0


# --------------------------------------------------------------------------- #
# 索引缓存
# --------------------------------------------------------------------------- #


async def test_index_cache_memory_disk_refresh_and_corrupt(tmp_path: Path) -> None:
    market, host, fake = build_market(tmp_path)
    await market.search("", limit=0)
    assert len(fake.json_calls) == 4

    cache_file = host.cache_dir("market") / "nonebot.json"
    assert cache_file.is_file()
    stored = json.loads(cache_file.read_text(encoding="utf-8"))
    assert isinstance(stored["fetched_at"], float) and stored["payload"]

    await market.search("", limit=0)  # 内存缓存
    assert len(fake.json_calls) == 4

    second = Market(host, build_sources()[0], fetcher=fake)  # 新实例 → 磁盘缓存
    rows = await second.sources_status()
    assert all(row["cached"] is True for row in rows)
    assert len(fake.json_calls) == 4
    assert [row["entries"] for row in rows] == [3, 3, 3, 3]

    cache_file.write_text("{ 这不是 JSON", encoding="utf-8")
    third = Market(host, build_sources()[0], fetcher=fake)
    entries = await third.search("", limit=0)
    assert len(entries) == 12
    assert len(fake.json_calls) == 5  # 只有坏掉的那个源重新拉
    assert "缓存损坏" in host.text()

    await third.search("", limit=0, refresh=True)  # refresh 绕过缓存
    assert len(fake.json_calls) == 9
    assert json.loads(cache_file.read_text(encoding="utf-8"))["payload"]


async def test_install_archive_without_plugin_layout_fails(tmp_path: Path) -> None:
    """归档里没有插件该有的东西：结构化失败 + 把顶层内容报出来（不猜）。"""
    market, host, fake = build_market(tmp_path)
    url = "https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"
    add_zip(fake, url, {"MutePlugin-main/README.md": "没有插件\n"})
    result = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert result.ok is False
    assert "没找到 MaiBot 插件目录" in result.error
    assert "README.md" in result.error
    assert not result.target_dir.exists()
    assert [item for item in host.plugins_root.rglob("*") if item.is_file()] == []
    assert list((host.plugins_root / "maibot").glob(".market-*")) == []


# --------------------------------------------------------------------------- #
# 索引缓存
# --------------------------------------------------------------------------- #


async def test_cache_is_invalidated_when_index_url_changes(tmp_path: Path) -> None:
    """缓存里记着索引地址：地址变了（换了镜像/上游迁移）就不再复用旧数据。"""
    market, host, fake = build_market(tmp_path)
    await market.search("", limit=0)
    cache_file = host.cache_dir("market") / "maibot.json"
    stored = json.loads(cache_file.read_text(encoding="utf-8"))
    assert stored["index_url"] == MAIBOT_INDEX

    stored["index_url"] = "https://github.com/someone/else/index.json"
    cache_file.write_text(json.dumps(stored), encoding="utf-8")

    fresh = Market(host, [MaiBotSource()], fetcher=fake)
    assert len(await fresh.search("", limit=0)) == 3
    assert len(fake.json_calls) == 5
    assert "索引地址变了" in host.text()


async def test_cache_ttl_expiry_refetches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """TTL 过了就重新拉（用可控时钟，不靠 sleep）。"""

    class Clock:
        def __init__(self) -> None:
            self.value = 1000.0

        def time(self) -> float:
            self.value += 1.0
            return self.value

    monkeypatch.setattr(market_mod, "time", Clock())
    market, host, fake = build_market(tmp_path, settings={"cache_ttl_seconds": 1})
    await market.search("", limit=0)
    assert len(fake.json_calls) == 4
    await market.search("", limit=0)  # 时钟已过 TTL → 重新拉
    assert len(fake.json_calls) == 8
    assert (host.cache_dir("market") / "maibot.json").is_file()


async def test_cache_ttl_zero_always_refetches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Clock:
        def __init__(self) -> None:
            self.value = 500.0

        def time(self) -> float:
            self.value += 1.0
            return self.value

    monkeypatch.setattr(market_mod, "time", Clock())
    market, _host, fake = build_market(tmp_path, settings={"cache_ttl_seconds": 0})
    await market.search("", limit=0)
    await market.search("", limit=0)
    assert len(fake.json_calls) == 8


# --------------------------------------------------------------------------- #
# 结构化失败码 / 依赖报告（新增语义）
# --------------------------------------------------------------------------- #


def test_requirement_parsing_helpers() -> None:
    from bridge.market import _read_requirements, _requirement_name, _requires_from_pypi

    assert _requirement_name("feedparser>=6 ; python_version>='3'") == "feedparser"
    assert _requirement_name('-r other.txt') == ""
    assert _requirement_name("git+https://example.invalid/x.git") == ""
    assert _requirement_name("") == ""

    record = {"info": {"requires_dist": ["httpx>=0.27", "pydantic", "httpx>=0.27", "bad name!"]}}
    assert _requires_from_pypi(record) == ("httpx", "pydantic")

    root = Path(tempfile.mkdtemp(prefix="bb_req_"))
    (root / "requirements.txt").write_text("feedparser\n# comment\nhttpx\n", encoding="utf-8")
    assert _read_requirements(root) == ("feedparser", "httpx")


async def test_install_not_in_index_sets_code(tmp_path: Path) -> None:
    market, _host, _fake = build_market(tmp_path)
    result = await market.install(source="maibot", plugin_id="no.such.plugin")
    assert result.ok is False
    assert result.code == "not_in_index"
    assert result.to_payload()["code"] == "not_in_index"


async def test_install_already_installed_sets_code(tmp_path: Path) -> None:
    market, _host, fake = build_market(tmp_path)
    fake.byte_map["https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"] = maibot_zip()
    first = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert first.ok is True, first.error
    second = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert second.ok is False
    assert second.code == "already_installed"


async def test_install_missing_manifest_sets_code(tmp_path: Path) -> None:
    market, _host, fake = build_market(tmp_path)
    # 合法 zip，但里面没有 MaiBot 要的 _manifest.json
    fake.byte_map["https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"] = make_zip(
        {"MutePlugin-main/README.md": "no manifest here\n"}
    )
    result = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert result.ok is False
    assert result.code == "manifest_missing"
    assert "没找到" in result.error and "MaiBot" in result.error


def test_fetcher_rejects_non_allowlisted_host(tmp_path: Path) -> None:
    from bridge.market import MarketError, default_fetcher

    fetcher = default_fetcher({})
    with pytest.raises(MarketError) as excinfo:
        fetcher.check_url("https://evil.example.invalid/plugin.zip")  # type: ignore[attr-defined]
    assert getattr(excinfo.value, "code", "") == "unsupported_host"


def test_install_archive_raises_coded_errors(tmp_path: Path) -> None:
    """解压层的失败码要能独立成立（不是靠上层猜出来的）。"""
    from bridge.market import MarketError, _install_archive
    from bridge.market import InstallPlan, MarketEntry

    entry = MarketEntry(source="maibot", plugin_id="x", install_kind="github-zip")
    plan = InstallPlan(entry=entry, target_dir=tmp_path / "maibot" / "x")

    cases = {
        "path_traversal": make_zip({"ok/_manifest.json": "{}", "../escape.py": "x"}),
        "manifest_missing": make_zip({"pkg/README.md": "nothing usable\n"}),
    }
    from bridge.market import _MaiBotShape

    for expected, blob in cases.items():
        with pytest.raises(MarketError) as excinfo:
            _install_archive(
                blob,
                plan,
                shape=_MaiBotShape(),
                max_extract_bytes=8 * 1024 * 1024,
                max_files=100,
            )
        assert getattr(excinfo.value, "code", "") == expected, (expected, excinfo.value)

    too_many = make_zip({f"ok/f{i}.txt": "x" for i in range(6)} | {"ok/_manifest.json": "{}"})
    with pytest.raises(MarketError) as excinfo:
        _install_archive(
            too_many, plan, shape=_MaiBotShape(), max_extract_bytes=8 * 1024 * 1024, max_files=2
        )
    assert getattr(excinfo.value, "code", "") == "too_many_files"


# --------------------------------------------------------------------------- #
# 落盘层级：更深 / 同名优先，且装完必须被对应 loader 认出来
# --------------------------------------------------------------------------- #


async def test_locate_prefers_deeper_match(tmp_path: Path) -> None:
    """仓库根也带清单、真正的插件在更深一层时，要搬更深那一层（否则 loader 扫不到）。"""
    market, host, fake = build_market(tmp_path)
    fake.byte_map["https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"] = make_zip(
        {
            "MutePlugin-main/_manifest.json": '{"manifest_version": 2, "id": "root.monorepo"}',
            "MutePlugin-main/plugin.py": "def create_plugin(): raise RuntimeError\n",
            "MutePlugin-main/plugins/real/_manifest.json": '{"manifest_version": 2, "id": "real.plugin"}',
            "MutePlugin-main/plugins/real/plugin.py": "def create_plugin(): return None\n",
        }
    )
    result = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert result.ok is True, result.error
    # 装的是更深那一层：目标目录里直接就是 real 插件的清单，而不是套着 plugins/
    assert (result.target_dir / "_manifest.json").is_file()
    assert (result.target_dir / "plugin.py").is_file()
    assert not (result.target_dir / "plugins").exists()


async def test_installed_layout_is_accepted_by_every_backend(tmp_path: Path) -> None:
    """三个框架各装一次，落盘目录都必须被各自的 loader 接受（市场成功 ≠ loader 看得见）。"""
    from bridge.backends.astrbot import BACKEND as ASTRBOT_BACKEND
    from bridge.backends.maibot import BACKEND as MAIBOT_BACKEND

    market, host, fake = build_market(tmp_path)

    # MaiBot：目录里有 _manifest.json + plugin.py
    fake.byte_map["https://github.com/SengokuCola/MutePlugin/archive/refs/heads/main.zip"] = make_zip(
        {
            "MutePlugin-main/_manifest.json": '{"manifest_version": 2, "name": "x", "version": "1.0.0"}',
            "MutePlugin-main/plugin.py": "def create_plugin(): return None\n",
        }
    )
    maibot = await market.install(source="maibot", plugin_id=MAIBOT_RICH)
    assert maibot.ok is True, maibot.error
    assert MAIBOT_BACKEND.detect(maibot.target_dir) is not None

    # AstrBot：目录里有 metadata.yaml + main.py
    from bridge.market import _github_archive_urls

    astrbot_entry = next(
        item
        for item in AstrBotSource().parse(load_fixture("astrbot_collection.json"))
        if item.plugin_id == ASTRBOT_COLLECTION_ID
    )
    for url in _github_archive_urls(astrbot_entry.repository):
        fake.byte_map[url] = make_zip(
        {
            "astrbot_plugin_get_px-main/metadata.yaml": (
                "name: get_px\ndesc: demo\nversion: v1.0.0\nauthor: tester\n"
            ),
            "astrbot_plugin_get_px-main/main.py": (
                "from astrbot.api.star import Context, Star\n\n\nclass P(Star):\n"
                "    def __init__(self, context: Context):\n        super().__init__(context)\n"
            ),
        }
        )
    astr = await market.install(source="astrbot", plugin_id=ASTRBOT_COLLECTION_ID)
    assert astr.ok is True, astr.error
    assert ASTRBOT_BACKEND.detect(astr.target_dir) is not None


async def test_sources_status_refresh_really_refetches_and_writes_cache(tmp_path: Path) -> None:
    """refresh=True 必须绕过缓存真拉一次（并落盘缓存）；默认则完全不联网。"""
    fetcher = FakeFetcher(json_map=index_payloads())
    market, _host, _fake = build_market(tmp_path, fetcher=fetcher, load_index=True)

    before = len(fetcher.json_calls)
    await market.sources_status()
    assert len(fetcher.json_calls) == before, "默认状态查询不该联网"

    await market.sources_status(refresh=True)
    assert len(fetcher.json_calls) > before, "refresh=True 必须真的重新拉索引"

    cache_dir = market._cache_dir()
    assert cache_dir is not None
    written = sorted(path.name for path in cache_dir.glob("*.json"))
    assert written, f"索引缓存应当落盘：{cache_dir}"


async def test_fetch_has_a_total_wall_clock_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``timeout`` 必须是这一次抓取的**总预算**：socket timeout 拦不住慢速滴流。"""
    import time as time_mod

    def blocking_urlopen(*_args, **_kwargs):  # noqa: ANN002, ANN003
        time_mod.sleep(2.0)  # to_thread 里跑的同步函数：慢速滴流的等价物
        return b"{}"

    monkeypatch.setattr(market_mod, "_urlopen_bytes", blocking_urlopen)
    # 必须用真抓取器：FakeFetcher 不走 _urlopen_bytes，测不到总预算。
    host = FakeHost(tmp_path / "host_budget", {"timeout": 0.5, "mirror_prefixes": []})
    market = Market(host, build_sources()[0], fetcher=default_fetcher({"mirror_prefixes": []}))

    started = time_mod.monotonic()
    rows = await market.sources_status(refresh=True, only="maibot")
    spent = time_mod.monotonic() - started

    assert spent < 1.5, f"总预算没生效：等了 {spent:.1f}s"
    assert rows and rows[0]["ok"] is False
    assert "总预算" in str(rows[0]["error"]), rows[0]["error"]
    assert any("探活失败" in message for _level, message in host.logs)


async def test_sources_status_logs_probe_failure(tmp_path: Path) -> None:
    """探活失败要写日志：面板只显示一行 error，日志里全空会让人没法查。"""
    dead = FakeFetcher()  # 空 map → 源一律 404
    market, host, _fake = build_market(tmp_path, fetcher=dead, load_index=False)
    rows = await market.sources_status(refresh=True, only="nonebot")
    assert rows and rows[0]["ok"] is False
    messages = [message for _level, message in host.logs]
    assert any("探活失败" in message and "nonebot" in message for message in messages), messages[-5:]


async def test_fetch_budget_is_split_across_mirrors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """总预算要**均分给每个地址**：直连卡满预算时，镜像仍要有一次完整机会。"""
    import time as time_mod

    seen: list[str] = []

    def slow_urlopen(url, *_args, **_kwargs):  # noqa: ANN001, ANN002, ANN003
        seen.append(url)
        time_mod.sleep(1.2)  # 每次尝试都睡满自己那一档
        raise market_mod.MarketError("被墙了")

    monkeypatch.setattr(market_mod, "_urlopen_bytes", slow_urlopen)
    host = FakeHost(tmp_path / "host_mirror", {"timeout": 2.0, "mirror_prefixes": ["https://ghproxy.net/"]})
    market = Market(host, build_sources()[0], fetcher=default_fetcher({"mirror_prefixes": ["https://ghproxy.net/"]}))

    started = time_mod.monotonic()
    rows = await market.sources_status(refresh=True, only="maibot")
    spent = time_mod.monotonic() - started

    assert len(seen) == 2, f"镜像没被尝试：{seen}"  # 直连 + 镜像各一次
    assert seen[1].startswith("https://ghproxy.net/"), seen
    assert spent < 3.0, f"两次尝试各自的档位没生效：{spent:.1f}s"
    assert rows and rows[0]["ok"] is False
