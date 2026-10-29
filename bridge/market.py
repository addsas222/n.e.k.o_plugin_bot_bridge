"""机器人插件市场：读官方索引 → 搜索 → 装进隔离的插件目录。

三段职责分得很开：

* :class:`MarketSource` 负责「把某个官方索引的 JSON 变成 :class:`MarketEntry`」以及
  「告诉安装器该下哪个包、装到哪个目录」——三个源（MaiBot 插件仓库 / NoneBot 商店 /
  AstrBot 市场）各实现一份，索引形态的差异全部关在这里面。
* :class:`HttpFetcher` 负责「把 URL 变成字节」——只走 https、只走主机白名单（**重定向的每一跳**
  都要重新过白名单）、有体积上限、直连失败时按 ``mirror_prefixes`` 依次重试。
* :class:`Market` 负责编排：缓存索引、搜索、下载、**安全解压**、写溯源文件。

安全边界（装第三方代码必须保守）：

* 只认索引里的条目，未知 ``plugin_id`` 直接结构化失败；
* 解压拒绝绝对路径 / ``..`` / 符号链接 / 越界成员，限制文件数与解压总量，
  跳过 ``__pycache__`` / ``.git`` / ``*.pyc``；
* 目标目录必须在 ``plugins_root`` 内（``resolve()`` + ``is_relative_to`` 校验），
  解压先落在同级暂存目录，校验通过后才搬进目标；
* 所有失败路径都返回 ``ok=False`` + 非空 ``error``；异常只留给编程错误。

本模块只用标准库，Python 3.11 起可用。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from .contracts import HostServices
from .errors import BridgeError

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 默认 https 主机白名单（``[bridge.market].allow_hosts`` 只做追加）。
DEFAULT_ALLOW_HOSTS: tuple[str, ...] = (
    "github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "ghproxy.net",
    "api.github.com",
    "registry.nonebot.dev",
    "registry.npmmirror.com",
    "cdn.npmmirror.com",
    "pypi.org",
    "files.pythonhosted.org",
    "cloud.astrbot.app",
    "api.astrbot.app",
    "astrbot.app",
)

#: 默认镜像前缀（``前缀 + 原始 URL``），按顺序重试。
DEFAULT_MIRROR_PREFIXES: tuple[str, ...] = ("https://ghproxy.net/",)

#: 缓存目录名（``host.cache_dir("market")`` 下）。
CACHE_NAMESPACE = "market"

#: 溯源文件名（装在目标插件目录里）。
PROVENANCE_FILE = ".bot_bridge_market.json"

_USER_AGENT = "neko-bot-bridge-market/0.1 (+stdlib urllib)"
_DEFAULT_TIMEOUT = 60.0  # 单源一次完整抓取（含镜像重试）的总预算；astrbot 索引实测 ~25s，30s 太贴边
_DEFAULT_MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
_DEFAULT_MAX_FILES = 5000
_DEFAULT_CACHE_TTL = 600.0
_DEFAULT_MAX_ENTRIES = 500
_MAX_INDEX_BYTES = 8 * 1024 * 1024
_CHUNK = 64 * 1024
_FALLBACK_DIR_NAME = "plugin"
_MAX_DIR_NAME = 64

_SKIP_DIR_NAMES = frozenset({"__pycache__", ".git"})
_SKIP_SUFFIXES = (".pyc", ".pyo")
_DIR_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_SEPARATOR_RUN_RE = re.compile(r"[-._]{2,}")
_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:")
_RESERVED_DIR_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)

_GITHUB_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/"
    r"(?P<owner>[A-Za-z0-9._-]+)/(?P<repo>[A-Za-z0-9._-]+?)"
    r"(?:\.git)?(?:[/#?].*)?$",
    re.IGNORECASE,
)

#: 三个框架的名字（也是 :attr:`MarketSource.name` 与插件子目录名）。
# 有官方「插件市场 JSON 索引」的框架才进这张表；koishi 的索引是 npm 镜像的
# 搜索 JSON（text=koishi-plugin，单页上限 250，取相关度排序）。
FRAMEWORKS: tuple[str, ...] = ("maibot", "nonebot", "astrbot", "koishi")

#: 三个源的顺序（搜索结果按它排：MaiBot → NoneBot → AstrBot）。
_SOURCE_ORDER: tuple[str, ...] = FRAMEWORKS


class MarketError(BridgeError):
    """市场层的结构化错误。

    只用于「不该发生」的情况（索引结构不符、宿主缺能力）；业务失败一律走
    :class:`InstallResult` 的 ``ok=False``。``code`` 取各调用点写入的市场层失败码。
    """

    def __init__(self, message: str, *, code: str = "") -> None:
        super().__init__(message)
        # 没显式给码时保留类级默认（``market_error``），不要伪装成某个具体失败类型。
        self.code = code or type(self).code

    code = "market_error"


# --------------------------------------------------------------------------- #
# 数据类型
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class MarketSearchReport:
    """一次跨源搜索的结果报告。

    ``counts`` / ``truncated`` / ``failures`` 让调用方能解释「为什么列表里只有这几个源的插件」：
    条目多的源容易把别的源挤没（astrbot 1329 条 vs maibot 366 条），所以跨源结果按源轮流取，
    每源命中数一并返回，面板据此显示来源分布。
    """

    entries: list[MarketEntry]
    counts: dict[str, int] = field(default_factory=dict)
    truncated: dict[str, int] = field(default_factory=dict)
    failures: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class MarketEntry:
    """索引里的一条插件条目。"""

    source: str
    plugin_id: str
    name: str = ""
    description: str = ""
    version: str = ""
    author: str = ""
    repository: str = ""
    download_url: str = ""
    install_kind: str = ""
    raw: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class InstallPlan:
    """一次安装的「怎么装」：候选 URL、目标目录、要不要剥一层前缀目录。"""

    entry: MarketEntry
    target_dir: Path
    urls: tuple[str, ...] = ()
    strip_prefix: bool = True
    warnings: tuple[str, ...] = ()


@dataclass(slots=True)
class InstallResult:
    """一次安装的结果（无论成败都返回它，绝不靠异常表达业务失败）。"""

    ok: bool
    entry: MarketEntry
    target_dir: Path
    url: str = ""
    files: int = 0
    archive_sha256: str = ""
    error: str = ""
    #: 结构化失败码（见 ``FailureCode``）；成功时为空串。
    code: str = ""
    #: 插件自己声明的额外依赖（requirements.txt / wheel METADATA），安装时如实报出来。
    requires: tuple[str, ...] = ()

    def to_payload(self) -> dict[str, object]:
        """给插件入口 / LLM 工具用的普通字典。"""
        return {
            "ok": self.ok,
            "code": self.code,
            "source": self.entry.source,
            "plugin_id": self.entry.plugin_id,
            "name": self.entry.name,
            "version": self.entry.version,
            "install_kind": self.entry.install_kind,
            "target_dir": str(self.target_dir),
            "url": self.url,
            "files": self.files,
            "archive_sha256": self.archive_sha256,
            "requires": list(self.requires),
            "error": self.error,
        }


# --------------------------------------------------------------------------- #
# 网络
# --------------------------------------------------------------------------- #


class HttpFetcher(Protocol):
    """把 URL 变成字节 / JSON 的最小接口（离线测试换成假的即可）。"""

    async def get_bytes(self, url: str, *, timeout: float, max_bytes: int) -> bytes:
        """下载 ``url``；超时、超体积、主机不在白名单等一律抛 :class:`MarketError`。"""
        ...

    async def get_json(self, url: str, *, timeout: float) -> object:
        """下载并解析 JSON；不是合法 JSON 时抛 :class:`MarketError`。"""
        ...


class _PermanentFetchError(MarketError):
    """内部：镜像重试救不回来的失败（404 / 太大 / 太可疑）。"""


class _AllowlistRedirectHandler(urllib.request.HTTPRedirectHandler):
    """跟随重定向，但**每一跳**都要重新过 https + 主机白名单。

    urllib 默认会默默跟到任意主机；允许 ghproxy 这类第三方镜像之后，那是真的可以
    把下载带到白名单外，所以这里把检查钉在每个跳转目标上。
    """

    def __init__(self, check_url: Callable[[str], str]) -> None:
        super().__init__()
        self._check_url = check_url

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> urllib.request.Request | None:
        self._check_url(urllib.parse.urljoin(req.full_url, str(newurl)))
        return super().redirect_request(req, fp, code, msg, headers, newurl)  # type: ignore[arg-type]


def _build_opener(check_url: Callable[[str], str]) -> urllib.request.OpenerDirector:
    """构造「重定向也要过白名单」的 opener。"""
    return urllib.request.build_opener(_AllowlistRedirectHandler(check_url))


def _urlopen_bytes(
    url: str,
    timeout: float,
    max_bytes: int,
    user_agent: str,
    *,
    opener: urllib.request.OpenerDirector,
) -> bytes:
    """同步 GET 一个 URL（重定向每一跳都过白名单），带体积上限。

    **测试通过替换这个函数来模拟网络**（签名保持一致即可）。
    """
    request = urllib.request.Request(
        url,
        headers={"User-Agent": user_agent, "Accept": "*/*"},
        method="GET",
    )
    chunks: list[bytes] = []
    total = 0
    try:
        with opener.open(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            if status != 200:
                raise _PermanentFetchError(f"HTTP {status}")
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > max_bytes:
                raise _PermanentFetchError(f"体积超过上限（{declared} > {max_bytes} 字节）")
            while True:
                chunk = response.read(_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise _PermanentFetchError(f"体积超过上限（{max_bytes} 字节）")
                chunks.append(chunk)
    except urllib.error.HTTPError as exc:
        message = f"HTTP {exc.code} {exc.reason}"
        if exc.code in (400, 401, 403, 404, 410):
            raise _PermanentFetchError(message) from exc
        raise MarketError(message) from exc
    except TimeoutError as exc:
        raise MarketError(f"超时（{timeout:.0f}s）") from exc
    except urllib.error.URLError as exc:
        raise MarketError(f"网络错误：{exc.reason}") from exc
    except OSError as exc:  # TLS 证书、连接被重置等
        raise MarketError(f"网络错误：{type(exc).__name__}: {exc}") from exc
    return b"".join(chunks)


class _UrllibFetcher:
    """stdlib ``urllib`` 实现：https + 主机白名单 + 镜像前缀重试 + 体积上限。"""

    def __init__(
        self,
        *,
        allow_hosts: Iterable[str],
        mirror_prefixes: Iterable[str] = (),
        user_agent: str = _USER_AGENT,
    ) -> None:
        self._allow_hosts = tuple(
            dict.fromkeys(str(host).strip().lower() for host in allow_hosts if str(host).strip())
        )
        self._mirror_prefixes = tuple(
            str(prefix).strip() for prefix in mirror_prefixes if str(prefix).strip()
        )
        self._user_agent = user_agent
        self._opener = _build_opener(self.check_url)

    @property
    def allow_hosts(self) -> tuple[str, ...]:
        """实际生效的主机白名单。"""
        return self._allow_hosts

    @property
    def mirror_prefixes(self) -> tuple[str, ...]:
        """实际生效的镜像前缀。"""
        return self._mirror_prefixes

    def check_url(self, url: str) -> str:
        """校验协议与主机白名单，返回原 URL。不合法时抛 :class:`MarketError`。"""
        parsed = urllib.parse.urlsplit(str(url or ""))
        if parsed.scheme != "https":
            raise MarketError(f"只允许 https 地址：{_short_url(url)}", code="unsupported_host")
        host = (parsed.hostname or "").lower()
        if not host:
            raise MarketError(f"地址里没有主机名：{_short_url(url)}", code="unsupported_host")
        if not self.host_allowed(host):
            raise MarketError(f"主机 '{host}' 不在市场白名单里（可用 allow_hosts 追加）", code="unsupported_host")
        return url

    def host_allowed(self, host: str) -> bool:
        """精确匹配，或匹配白名单里的裸域名的子域（``astrbot.app`` → ``*.astrbot.app``）。"""
        candidate = str(host or "").strip().lower()
        return any(
            candidate == allowed or candidate.endswith(f".{allowed}") for allowed in self._allow_hosts
        )

    def _attempts(self, url: str) -> list[tuple[str, str]]:
        attempts = [("直连", url)]
        for prefix in self._mirror_prefixes:
            attempts.append((f"镜像 {prefix}", f"{prefix}{url}"))
        return attempts

    async def get_bytes(self, url: str, *, timeout: float, max_bytes: int) -> bytes:
        """抓一个 URL；``timeout`` 是**这次抓取的总预算**（直连 + 各镜像合计，均分给每个地址）。

        ``urllib`` 的 ``timeout`` 只作用于单次 socket 操作，慢速滴流的响应可以远超它
        （真机上出现过删掉缓存后探活卡 4 分钟、日志一行没有）。所以这里用一个总的墙钟
        预算把每次尝试包进 ``asyncio.wait_for``，预算按剩余地址数均分（镜像不会被直连饿死），
        用完就如实报出来。
        """
        self.check_url(url)  # 原始地址不合法就直接点名主机失败，不浪费镜像重试
        errors: list[str] = []
        budget = max(1.0, float(timeout))
        deadline = time.monotonic() + budget
        attempts = self._attempts(url)
        for index, (label, candidate) in enumerate(attempts):
            remaining = deadline - time.monotonic()
            if remaining <= 0.5:
                errors.append(f"{label}: 总预算 {budget:.0f}s 已用尽，跳过")
                break
            # 按剩余地址数**均分**剩余预算：直连要是「卡到预算耗尽」（被墙时的典型表现），
            # 镜像还得有完整一档机会，否则 mirror_prefixes 这个兜底等于不存在。
            attempt_budget = max(1.0, remaining / (len(attempts) - index))
            try:
                self.check_url(candidate)
            except MarketError as exc:
                errors.append(f"{label}: {exc}")
                continue
            try:
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        _urlopen_bytes,
                        candidate,
                        attempt_budget,
                        max_bytes,
                        self._user_agent,
                        opener=self._opener,
                    ),
                    timeout=attempt_budget,
                )
            except (asyncio.TimeoutError, TimeoutError):
                # 超时原因写进汇总错误里（调用方会把整条原因落日志：探活失败 / 索引不可用）。
                errors.append(f"{label}: 抓取超过总预算 {budget:.0f}s")
            except _PermanentFetchError as exc:
                errors.append(f"{label}: {exc}")
                break  # 404 / 体积超限：换镜像也一样
            except MarketError as exc:
                errors.append(f"{label}: {exc}")
            except Exception as exc:  # noqa: BLE001 — 网络层的意外错误也要如实汇总
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
        raise MarketError(f"下载失败（试了 {len(errors)} 个地址）：{'；'.join(errors)}", code="download_failed")

    async def get_json(self, url: str, *, timeout: float) -> object:
        payload = await self.get_bytes(url, timeout=timeout, max_bytes=_MAX_INDEX_BYTES)
        try:
            return json.loads(payload.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise MarketError(f"索引不是 UTF-8 文本：{exc}", code="source_unavailable") from exc
        except json.JSONDecodeError as exc:
            raise MarketError(f"索引不是合法 JSON：{exc}", code="source_unavailable") from exc


def default_fetcher(settings: Mapping[str, object]) -> HttpFetcher:
    """按 ``[bridge.market]`` 配置构造默认抓取器。"""
    allow_hosts = list(DEFAULT_ALLOW_HOSTS) + _string_list(settings.get("allow_hosts"))
    if "mirror_prefixes" in settings:
        mirrors = _string_list(settings.get("mirror_prefixes"))
    else:
        mirrors = list(DEFAULT_MIRROR_PREFIXES)
    return _UrllibFetcher(allow_hosts=allow_hosts, mirror_prefixes=mirrors)


# --------------------------------------------------------------------------- #
# 抓取器 / 源的公共接口
# --------------------------------------------------------------------------- #


class MarketSource(Protocol):
    """一个市场源：官方索引 → 条目，以及「这条目该怎么装」。"""

    name: str
    display_name: str
    index_url: str

    def parse(self, payload: object) -> list[MarketEntry]:
        """把索引 JSON 解析成条目；形态不符时抛 :class:`MarketError`（绝不默默返回空）。"""
        ...

    def plan_install(
        self, entry: MarketEntry, plugins_root: Path, *, overwrite: bool = False
    ) -> InstallPlan:
        """算出一个条目的安装计划（候选 URL + 目标目录）。"""
        ...


# --------------------------------------------------------------------------- #
# 解压后的插件形态
# --------------------------------------------------------------------------- #


class _PluginShape(Protocol):
    """某个框架的插件目录长什么样（用于校验解压结果、决定要不要下钻）。"""

    label: str

    def matches(self, path: Path) -> bool:
        """``path`` 是不是「可以直接放进 ``plugins_root/<框架>/`` 的那一层」。"""
        ...

    def rank(self, path: Path) -> int:
        """同一批候选里的优先级，越小越好（0 = 首选）。

        默认实现给所有命中打平手；NoneBot 那种「目录名必须等于模块名」的框架会
        给名字对上的目录 0、其它 1 —— 于是仓库根恰好也像插件目录时不会选错层。
        """
        ...

    def entry_warning(self, root: Path) -> str:
        """入口文件缺失时的告警文案（没有问题时返回空串）。"""
        ...

    def error_hint(self, base: Path) -> str:
        """找不到插件目录时的补充提示（例如单文件插件装不了）。"""
        ...


@dataclass(frozen=True, slots=True)
class _MaiBotShape:
    """MaiBot：目录里要有 ``_manifest.json``，入口是 ``plugin.py``。"""

    label: str = "MaiBot"

    def rank(self, path: Path) -> int:
        return 0

    def matches(self, path: Path) -> bool:
        return (path / "_manifest.json").is_file()

    def entry_warning(self, root: Path) -> str:
        if (root / "plugin.py").is_file():
            return ""
        return "目录里没有 plugin.py，MaiBot 兼容层可能识别不了它"

    def error_hint(self, base: Path) -> str:
        return ""


@dataclass(frozen=True, slots=True)
class _AstrBotShape:
    """AstrBot：目录里要有 ``metadata.yaml``，入口是 ``main.py`` 或 ``<目录名>.py``。"""

    label: str = "AstrBot"

    def rank(self, path: Path) -> int:
        return 0

    def matches(self, path: Path) -> bool:
        return any((path / name).is_file() for name in ("metadata.yaml", "metadata.yml"))

    def entry_warning(self, root: Path) -> str:
        if (root / "main.py").is_file() or (root / f"{root.name}.py").is_file():
            return ""
        return f"目录里没有 main.py 也没有 {root.name}.py，AstrBot 兼容层可能识别不了它"

    def error_hint(self, base: Path) -> str:
        return ""


@dataclass(frozen=True, slots=True)
class _NoneBotShape:
    """NoneBot：目标那一层必须是以模块名为名的包（``<module>/__init__.py``）。"""

    module_name: str
    label: str = "NoneBot"

    def rank(self, path: Path) -> int:
        return 0 if self.module_name and path.name == self.module_name else 1

    def matches(self, path: Path) -> bool:
        if not self.module_name or path.name != self.module_name:
            return False
        return (path / "__init__.py").is_file()

    def entry_warning(self, root: Path) -> str:
        return ""  # matches() 已经保证 __init__.py 存在

    def error_hint(self, base: Path) -> str:
        if self._find_module_file(base) is not None:
            return (
                f"这个插件是单文件插件（{self.module_name}.py）：桥接层的插件发现只认目录布局，"
                f"市场不会替你改布局，所以没有安装。请手动把 {self.module_name}.py 放进 "
                f"<plugins_root>/nonebot/ 后重载。"
            )
        return ""

    def _find_module_file(self, base: Path) -> Path | None:
        wanted = f"{self.module_name}.py"
        for depth in range(3):
            for directory in _dirs_at_depth(base, depth):
                if (directory / wanted).is_file():
                    return directory / wanted
        return None


def _shape_for(source_name: str, entry: MarketEntry) -> _PluginShape | None:
    """按源名（= 框架名，允许 ``maibot-xxx`` 这类别名后缀）挑插件形态。"""
    key = str(source_name or "").strip().lower()
    for framework in FRAMEWORKS:
        if key != framework and not key.startswith(f"{framework}-") and not key.startswith(
            f"{framework}_"
        ):
            continue
        if framework == "maibot":
            return _MaiBotShape()
        if framework == "astrbot":
            return _AstrBotShape()
        if framework == "koishi":
            return _KoishiShape()
        module_name = str(entry.plugin_id or "").strip() or _text(entry.raw.get("module_name"))
        return _NoneBotShape(module_name=module_name)
    return None


def _read_package_json_safe(directory: Path) -> dict[str, object] | None:
    """读取并解析 ``package.json``；缺失/损坏返回 ``None``（市场校验用，绝不抛）。"""
    try:
        raw = (directory / "package.json").read_bytes()
    except OSError:
        return None
    try:
        data = json.loads(raw.decode("utf-8-sig", errors="replace"))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _resolve_entry_rel(package: dict[str, object]) -> str:
    """按 npm 规则解析入口相对路径（main / module / exports["."]）。"""
    exported = package.get("exports")
    if isinstance(exported, dict):
        dot = exported.get(".")
        if isinstance(dot, dict):
            candidate = dot.get("import") or dot.get("require") or dot.get("default")
            if isinstance(candidate, str):
                return candidate
        if isinstance(dot, str):
            return dot
    main_value = package.get("main")
    if isinstance(main_value, str) and main_value:
        return main_value
    module_value = package.get("module")
    if isinstance(module_value, str) and module_value:
        return module_value
    return "index.js"


@dataclass(frozen=True, slots=True)
class _KoishiShape:
    """Koishi：目录里要有 ``package.json``（入口由 main/module/exports 声明）。"""

    label: str = "Koishi"

    def rank(self, path: Path) -> int:
        return 0

    def matches(self, path: Path) -> bool:
        return (path / "package.json").is_file()

    def entry_warning(self, root: Path) -> str:
        package = _read_package_json_safe(root)
        entry = _resolve_entry_rel(package) if package else ""
        if entry and (root / entry).is_file():
            return ""
        return f"目录里没有可解析的入口文件（main/module/exports → {entry or '未声明'}），Koishi 兼容层可能加载不了它"

    def error_hint(self, base: Path) -> str:
        return ""


# --------------------------------------------------------------------------- #
# 三个官方源
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class MaiBotSource:
    """MaiBot 官方插件仓库索引（``plugin_details.json``：``[{id, manifest}, …]``）。"""

    name: str = "maibot"
    display_name: str = "MaiBot 插件仓库"
    index_url: str = "https://raw.githubusercontent.com/Mai-with-u/plugin-repo/main/plugin_details.json"

    def parse(self, payload: object) -> list[MarketEntry]:
        items = _require_list(payload, self.display_name)
        entries: list[MarketEntry] = []
        for position, item in enumerate(items, start=1):
            record = _require_mapping(item, f"{self.display_name} 第 {position} 项")
            plugin_id = _text(record.get("id")) or _text(record.get("plugin_id"))
            if not plugin_id:
                raise MarketError(
                    f"{self.display_name} 第 {position} 项缺少 id"
                    f"（期望 {{\"id\": str, \"manifest\": {{…}}}}，实际键：{_keys(record)}）"
                )
            manifest = _require_mapping(record.get("manifest"), f"{self.display_name} 第 {position} 项的 manifest")
            urls = manifest.get("urls") if isinstance(manifest.get("urls"), Mapping) else {}
            repository = _text(urls.get("repository")) or _text(manifest.get("repository_url"))
            entries.append(
                MarketEntry(
                    source=self.name,
                    plugin_id=plugin_id,
                    name=_text(manifest.get("name")) or plugin_id,
                    description=_text(manifest.get("description")),
                    version=_text(manifest.get("version")),
                    author=_author_text(manifest.get("author")),
                    repository=_github_repo_url(repository),
                    install_kind="github-zip" if _github_repo(repository) else "unsupported",
                    raw=dict(record),
                )
            )
        return entries

    def plan_install(
        self, entry: MarketEntry, plugins_root: Path, *, overwrite: bool = False
    ) -> InstallPlan:
        target = _plan_target(
            self.name,
            entry,
            plugins_root,
            candidates=(_url_basename(entry.repository), entry.plugin_id, entry.name),
        )
        urls = _github_archive_urls(entry.repository)
        warnings = []
        if overwrite and target.exists():
            warnings.append("目标目录已存在，安装时先删掉它再放新的")
        if not urls:
            warnings.append("索引里没有仓库地址，这个条目没有可下载的东西")
        return InstallPlan(
            entry=entry, target_dir=target, urls=urls, strip_prefix=True, warnings=tuple(warnings)
        )



@dataclass(slots=True)
class NoneBotSource:
    """NoneBot 商店索引（``registry.nonebot.dev/plugins.json``：``[{module_name, …}, …]``）。

    NoneBot 插件的官方分发渠道是 PyPI；市场只在「仓库名与插件名对得上」（即仓库就是
    这个插件本身，不是装一堆插件的 monorepo）时才用仓库压缩包安装，其余条目老实报
    「本桥不代管第三方依赖」。
    """

    name: str = "nonebot"
    display_name: str = "NoneBot 插件商店"
    index_url: str = "https://registry.nonebot.dev/plugins.json"

    def parse(self, payload: object) -> list[MarketEntry]:
        items = _require_list(payload, self.display_name)
        entries: list[MarketEntry] = []
        for position, item in enumerate(items, start=1):
            record = _require_mapping(item, f"{self.display_name} 第 {position} 项")
            module_name = _text(record.get("module_name"))
            if not module_name:
                raise MarketError(
                    f"{self.display_name} 第 {position} 项缺少 module_name"
                    f"（期望 NoneBot 商店条目，实际键：{_keys(record)}）"
                )
            project_link = _text(record.get("project_link"))
            repository = _github_repo_url(_text(record.get("homepage")))
            installable = bool(repository) and _nonebot_repo_matches(module_name, project_link, repository)
            if installable:
                install_kind = "github-zip"
            elif project_link:
                # 官方分发渠道是 PyPI：直接下 wheel（纯 Python 包），不代管依赖。
                install_kind = "pypi-wheel"
            else:
                install_kind = "unsupported"
            entries.append(
                MarketEntry(
                    source=self.name,
                    plugin_id=module_name,
                    name=_text(record.get("name")) or module_name,
                    description=_text(record.get("desc")),
                    version=_text(record.get("version")),
                    author=_author_text(record.get("author")),
                    repository=repository,
                    install_kind=install_kind,
                    raw=dict(record),
                )
            )
        return entries

    def plan_install(
        self, entry: MarketEntry, plugins_root: Path, *, overwrite: bool = False
    ) -> InstallPlan:
        target = _plan_target(
            self.name,
            entry,
            plugins_root,
            candidates=(
                entry.plugin_id,
                _url_basename(entry.repository),
                _normalize_plugin_name(entry.repository),
                entry.name,
            ),
        )
        urls = _github_archive_urls(entry.repository) if entry.install_kind == "github-zip" else ()
        warnings: list[str] = []
        if overwrite and target.exists():
            warnings.append("目标目录已存在，安装时先删掉它再放新的")
        if entry.install_kind == "pypi-wheel":
            warnings.append("从 PyPI 装官方 wheel：它声明的依赖需要 N.E.K.O. 运行时里已经有，否则加载时会失败")
        elif entry.install_kind == "unsupported":
            warnings.append("这个条目既没有可用仓库也没有 PyPI 包名，无法安装")
        if not bool(entry.raw.get("valid", True)):
            warnings.append("官方索引标记这条为未通过测试（valid=false），装之前请自己看一眼")
        return InstallPlan(
            entry=entry, target_dir=target, urls=urls, strip_prefix=True, warnings=tuple(warnings)
        )


@dataclass(slots=True)
class AstrBotSource:
    """AstrBot 插件市场索引（``{条目键: {repo, …}}``，``$meta`` 是元数据）。

    默认用官方仓库里那份 ``plugin_cache_original.json``（GitHub 直链，落在主机白名单内）。
    它没有 ``download_url``，所以走仓库压缩包；如果换成 ``https://api.soulter.top/astrbot/plugins``
    那种带直链的索引（需要在 ``allow_hosts`` 里加 ``api.soulter.top``），会优先用直链。
    """

    name: str = "astrbot"
    display_name: str = "AstrBot 插件市场"
    index_url: str = (
        "https://github.com/AstrBotDevs/AstrBot_Plugins_Collection"
        "/raw/refs/heads/main/plugin_cache_original.json"
    )

    def parse(self, payload: object) -> list[MarketEntry]:
        table = _require_mapping(payload, self.display_name)
        entries: list[MarketEntry] = []
        for key, item in table.items():
            if str(key).startswith("$"):
                continue  # $meta 之类的保留键
            record = _require_mapping(item, f"{self.display_name} 条目 '{key}'")
            # 上游数据里确实有「repo 字段存在但不是一个可用的 GitHub 仓库」的条目
            # （例如 ``https://github.com/WhiteCloudOL/``）：标成装不了，但**不能让整个索引挂掉**。
            raw_repo = _text(record.get("repo"))
            repository = _github_repo_url(raw_repo)
            # 条目键就是索引里的身份：集合索引里是插件目录名（``astrbot_plugin_x``），
            # 官方市场索引里是 ``作者/插件名``（与 cloud.astrbot.app 的插件页一致）。
            # 条目自带的 name 只是人看的短名，不能当 id（会跟别的插件撞、也不利于搜索）。
            plugin_id = str(key).strip() or _text(record.get("name"))
            name = _text(record.get("name"))
            description = _text(record.get("desc")) or _text(record.get("short_desc"))
            entries.append(
                MarketEntry(
                    source=self.name,
                    plugin_id=plugin_id,
                    name=_text(record.get("display_name")) or name or plugin_id,
                    description=description,
                    version=_text(record.get("version")),
                    author=_author_text(record.get("author")),
                    repository=repository,
                    download_url=_text(record.get("download_url")),
                    install_kind="github-zip" if repository else "unsupported",
                    raw=dict(record),
                )
            )
        return entries

    def plan_install(
        self, entry: MarketEntry, plugins_root: Path, *, overwrite: bool = False
    ) -> InstallPlan:
        target = _plan_target(
            self.name,
            entry,
            plugins_root,
            candidates=(_url_basename(entry.repository), entry.plugin_id, entry.name),
        )
        warnings: list[str] = []
        if entry.download_url:
            urls = (entry.download_url, *_github_archive_urls(entry.repository))
            strip_prefix = False  # 市场直链压缩包的布局未知，交给「找插件目录」那步判断
            warnings.append("索引给了直链压缩包：先试它，失败再回退到仓库压缩包")
        else:
            urls = _github_archive_urls(entry.repository)
            strip_prefix = True
        if overwrite and target.exists():
            warnings.append("目标目录已存在，安装时先删掉它再放新的")
        if not urls:
            warnings.append("索引里没有仓库地址，这个条目没有可下载的东西")
        return InstallPlan(
            entry=entry,
            target_dir=target,
            urls=urls,
            strip_prefix=strip_prefix,
            warnings=tuple(warnings),
        )


@dataclass(slots=True)
class KoishiSource:
    """Koishi 插件市场索引（npm 镜像的搜索 JSON：``text=koishi-plugin``）。

    Koishi 生态的插件是 npm 包（``koishi-plugin-*``），市场数据来自 npm 镜像的
    搜索接口——这就是本桥读的「市场 JSON」。单页上限 250（npm search 的硬上限），
    取相关度排序；tarball 直链按 registry 规则拼出。
    """

    name: str = "koishi"
    display_name: str = "Koishi 插件市场"
    index_url: str = (
        "https://registry.npmmirror.com/-/v1/search?text=koishi-plugin&size=250"
    )
    registry_base: str = "https://registry.npmmirror.com"

    def parse(self, payload: object) -> list[MarketEntry]:
        payload_map = _require_mapping(payload, self.display_name)
        objects = _require_list(payload_map.get("objects"), f"{self.display_name} objects")
        entries: list[MarketEntry] = []
        for item in objects:
            if not isinstance(item, Mapping):
                continue
            package = item.get("package")
            if not isinstance(package, Mapping):
                continue
            name = _text(package.get("name"))
            if not name:
                continue
            version = _text(package.get("version"))
            links = package.get("links") if isinstance(package.get("links"), Mapping) else {}
            repository = _text(links.get("repository")) if links else ""
            publisher = package.get("publisher")
            author = (
                _text(publisher.get("username"))
                if isinstance(publisher, Mapping)
                else _author_text(package.get("author"))
            )
            display = _normalize_plugin_name(
                re.sub(r"^koishi-plugin-", "", name) or name
            )
            entries.append(
                MarketEntry(
                    source=self.name,
                    plugin_id=name,
                    name=display,
                    description=_preview(_text(package.get("description"))),
                    version=version,
                    author=author,
                    repository=repository,
                    download_url=self._tarball_url(name, version),
                    install_kind="koishi",
                    raw={
                        "keywords": _string_list(package.get("keywords")),
                        "date": _text(package.get("date")),
                    },
                )
            )
        return entries

    def _tarball_url(self, name: str, version: str) -> str:
        """npm registry 的 tarball 规则：scoped 包要把 ``@scope/name`` 编码成 ``@scope%2Fname``。"""
        encoded = urllib.parse.quote(name, safe="")
        filename = name.rsplit("/", 1)[-1]
        return f"{self.registry_base}/{encoded}/-/{filename}-{version}.tgz"



    def plan_install(
        self, entry: MarketEntry, plugins_root: Path, *, overwrite: bool = False
    ) -> InstallPlan:
        target = _plan_target(
            self.name,
            entry,
            plugins_root,
            candidates=(entry.plugin_id, entry.name),
        )
        urls = (entry.download_url,) if entry.download_url else ()
        warnings = []
        if overwrite and target.exists():
            warnings.append("目标目录已存在，安装时先删掉它再放新的")
        if not urls:
            warnings.append("索引里没有 tarball 直链，这个条目没有可下载的东西")
        return InstallPlan(
            entry=entry, target_dir=target, urls=urls, strip_prefix=True, warnings=tuple(warnings)
        )


_SOURCE_FACTORIES: dict[str, type] = {
    "maibot": MaiBotSource,
    "nonebot": NoneBotSource,
    "astrbot": AstrBotSource,
    "koishi": KoishiSource,
}


def build_sources(enabled: Sequence[str] | None = None) -> tuple[list[MarketSource], dict[str, str]]:
    """构造市场源。

    Args:
        enabled: 只启用这些源（``maibot`` / ``nonebot`` / ``astrbot``）；空 = 全部。

    Returns:
        ``(sources, errors)``。**用不了 / 配不出来的源一定带着原因出现在 errors 里**，
        不会无声消失。
    """
    requested, config_error = _requested_sources(enabled)
    errors: dict[str, str] = {}
    if config_error:
        errors["sources"] = config_error
    names = requested or [name for name in _SOURCE_ORDER if name in _SOURCE_FACTORIES]
    sources: list[MarketSource] = []
    seen: set[str] = set()
    for name in names:
        factory = _SOURCE_FACTORIES.get(name)
        if factory is None:
            errors[name] = f"未知的市场源（可用：{', '.join(_SOURCE_ORDER)}）"
            continue
        if name in seen:
            continue
        seen.add(name)
        try:
            source = factory()
        except Exception as exc:  # noqa: BLE001 — 源构造失败要带原因报出来
            errors[name] = f"市场源构造失败：{type(exc).__name__}: {exc}"
            continue
        index_url = str(getattr(source, "index_url", "") or "").strip()
        if not index_url.startswith("https://"):
            errors[name] = f"索引地址不可用（需要 https）：{index_url or '(空)'}"
            continue
        sources.append(source)
    return sources, errors


def _requested_sources(enabled: Sequence[str] | None) -> tuple[list[str], str]:
    """规范化 ``sources`` 配置，返回 ``(名字列表, 配置错误说明)``。"""
    if enabled is None:
        return [], ""
    if isinstance(enabled, str):
        return ([enabled.strip().lower()] if enabled.strip() else []), ""
    if isinstance(enabled, Sequence):
        names = [str(item).strip().lower() for item in enabled if str(item).strip()]
        return names, ""
    return [], f"sources 配置类型不对（{type(enabled).__name__}），已忽略并按全部源处理"


# --------------------------------------------------------------------------- #
# 市场
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _IndexSnapshot:
    """一次索引快照（条目 + 抓取时间 + 是否来自缓存）。"""

    entries: list[MarketEntry]
    fetched_at: float
    cached: bool
    index_url: str = ""


class Market:
    """插件市场的门面：索引缓存 + 搜索 + 安装。

    Args:
        host: 宿主能力面（只用 ``settings`` / ``log`` / ``cache_dir``，外加可选的
            ``plugins_root``；没有 ``plugins_root`` 时回落到数据目录下的 ``plugins/``）。
        sources: 市场源；``None`` = 按配置里的 ``[bridge.market].sources`` 现构造。
        fetcher: 抓取器；``None`` = :func:`default_fetcher`。
    """

    def __init__(
        self,
        host: HostServices,
        sources: Sequence[MarketSource] | None = None,
        *,
        fetcher: HttpFetcher | None = None,
    ) -> None:
        self._host = host
        settings = _market_settings(host)
        self._settings: dict[str, object] = settings
        self._enabled = _as_bool(settings.get("enabled"), True)
        self._allow_install = _as_bool(settings.get("allow_install"), True)
        self._timeout = _as_float(settings.get("timeout"), _DEFAULT_TIMEOUT, minimum=1.0)
        self._max_archive_bytes = _as_int(
            settings.get("max_archive_bytes"), _DEFAULT_MAX_ARCHIVE_BYTES, minimum=1024
        )
        self._max_extract_bytes = _as_int(
            settings.get("max_extract_bytes"), self._max_archive_bytes * 4, minimum=1024
        )
        self._max_files = _as_int(settings.get("max_files"), _DEFAULT_MAX_FILES, minimum=1)
        self._cache_ttl = _as_float(
            settings.get("cache_ttl_seconds"), _DEFAULT_CACHE_TTL, minimum=0.0
        )
        self._max_entries = _as_int(
            settings.get("max_entries_per_source"), _DEFAULT_MAX_ENTRIES, minimum=1
        )
        self._fetcher: HttpFetcher = fetcher if fetcher is not None else default_fetcher(settings)

        self._skipped_sources: list[str] = []
        if sources is None:
            configured = settings.get("sources")
            built, errors = build_sources(configured if configured is not None else None)  # type: ignore[arg-type]
            self._sources = list(built)
            for name, reason in errors.items():
                self._log("warning", f"市场源 {name} 不可用：{reason}")
        else:
            self._sources = self._dedupe_sources(sources)
        self._source_errors: dict[str, str] = {}
        self._cache: dict[str, _IndexSnapshot] = {}
        self._dropped_logs = 0

    # ---------------------------------------------------------------- 只读状态

    def status(self) -> dict[str, object]:
        """市场的当前状态（给 ``bridge_status`` / 排障看）。"""
        plugins_root = self._plugins_root()
        return {
            "enabled": self._enabled,
            "allow_install": self._allow_install,
            "plugins_root": str(plugins_root) if plugins_root is not None else "",
            "cache_dir": str(self._cache_dir()) if self._cache_dir() is not None else "",
            "sources": [source.name for source in self._sources],
            "skipped_sources": list(self._skipped_sources),
            "source_errors": dict(self._source_errors),
            "allow_hosts": list(getattr(self._fetcher, "allow_hosts", ()) or ()),
            "mirror_prefixes": list(getattr(self._fetcher, "mirror_prefixes", ()) or ()),
            "timeout": self._timeout,
            "max_archive_bytes": self._max_archive_bytes,
            "max_extract_bytes": self._max_extract_bytes,
            "max_files": self._max_files,
            "cache_ttl_seconds": self._cache_ttl,
            "max_entries_per_source": self._max_entries,
            "dropped_logs": self._dropped_logs,
        }

    async def sources_status(
        self, *, refresh: bool = False, only: str = ""
    ) -> list[dict[str, object]]:
        """每个源的状态；**拉不到索引也返回一行带原因的记录**（不抛异常）。

        默认 ``refresh=False``：只看缓存，**不联网**（冷启动时 ``entries`` 为 ``None``、
        ``stale=True``），所以状态查询在任何时候都是毫秒级。要主动探活/预热就传
        ``refresh=True`` —— 那会真的去拉索引，正常几秒到几十秒，**受网络影响可能到分钟级**（单次抓取受 ``timeout`` 总预算约束）。
        """
        if not refresh:
            return self._cached_status()
        # refresh=True 是「现在就探活」：必须绕过内存/磁盘缓存真的去拉，
        # 否则面板上的「探活/预热」按钮在缓存没过期时什么也没做。
        # ``only`` 用来一次只探一个源 —— 宿主的单次入口调用有默认超时（约 30 秒），
        # 三个索引一起拉很容易超预算，点名探活才是稳的用法。
        wanted = str(only or "").strip().lower()
        targets = [source for source in self._sources if not wanted or source.name == wanted]
        if not targets:
            return self._cached_status()
        snapshots = await asyncio.gather(
            *(self._snapshot(source, refresh=True) for source in targets),
            return_exceptions=True,
        )
        rows: list[dict[str, object]] = []
        for source, snapshot in zip(targets, snapshots):
            row: dict[str, object] = {
                "name": source.name,
                "display_name": source.display_name,
                "index_url": source.index_url,
                "ok": True,
                "entries": None,
                "cached": False,
                "stale": True,
                "age_seconds": None,
                "fetched_at": 0.0,
                "error": "",
                "note": "",
            }
            if isinstance(snapshot, BaseException):
                reason = _error_text(snapshot)
                self._source_errors[source.name] = reason
                # 探活失败必须留痕：面板上只显示一行 error，日志里没有线索会让人只能瞎猜。
                self._log("warning", f"市场源 {source.name} 探活失败：{reason}")
                row["ok"] = False
                row["error"] = reason
                row["note"] = "拉取失败：见 error"
            else:
                self._source_errors.pop(source.name, None)
                now = time.time()
                row["entries"] = len(snapshot.entries)
                row["cached"] = snapshot.cached
                row["stale"] = False
                row["fetched_at"] = snapshot.fetched_at
                row["age_seconds"] = max(0.0, now - snapshot.fetched_at)
            rows.append(row)
        return rows

    def _cached_status(self) -> list[dict[str, object]]:
        """只报缓存情况的状态行（不联网）。"""
        now = time.time()
        rows: list[dict[str, object]] = []
        for source in self._sources:
            snapshot = self._peek(source)
            row: dict[str, object] = {
                "name": source.name,
                "display_name": source.display_name,
                "index_url": source.index_url,
                "ok": True,
                "entries": None,
                "cached": False,
                "stale": True,
                "age_seconds": None,
                "fetched_at": 0.0,
                "error": "",
                "note": "未预热：调用时传 refresh=true，或先 search 一次",
            }
            if snapshot is not None:
                fresh = self._fresh(snapshot, now)
                row["entries"] = len(snapshot.entries) if fresh else None
                row["cached"] = True
                row["stale"] = not fresh
                row["fetched_at"] = snapshot.fetched_at
                row["age_seconds"] = max(0.0, now - snapshot.fetched_at)
                row["note"] = "" if fresh else "缓存已过期：调用时传 refresh=true 重新拉取"
            if source.name in self._source_errors:
                row["ok"] = False
                row["error"] = self._source_errors[source.name]
            rows.append(row)
        return rows

    # ------------------------------------------------------------------- 搜索

    async def search_report(
        self,
        query: str = "",
        *,
        source: str = "",
        limit: int = 20,
        refresh: bool = False,
    ) -> MarketSearchReport:
        """在索引里搜索，连带每源命中统计。

        跨源搜索按源轮流取条目（:func:`_interleave`）：不限源时，任何单个源都不会吃光
        ``limit``（修之前是「按 ``_sources`` 顺序拼接后截断」，排在最前的源 —— 默认 maibot ——
        会占满 limit=20 的结果，后面的源看不见）。

        Args:
            query: 关键词，命中 ``plugin_id`` / ``name`` / ``description`` / ``author`` /
                ``repository``（大小写不敏感）；空 = 全部。
            source: 只搜这个源；空 = 全部。
            limit: 返回条数上限；``<= 0`` = 不截断。
            refresh: ``True`` 时忽略缓存重新拉索引。

        Raises:
            MarketError: 市场被停用、``source`` 不认识，或**所有**受影响的源都拉不到索引。
        """
        if not self._enabled:
            raise MarketError("插件市场已停用（[bridge.market].enabled = false）")
        selected = self._select_sources(source)
        if not selected:
            return MarketSearchReport(entries=[])
        needle = str(query or "").strip().lower()
        snapshots = await asyncio.gather(
            *(self._snapshot(item, refresh=refresh) for item in selected), return_exceptions=True
        )
        failures: dict[str, str] = {}
        counts: dict[str, int] = {}
        truncated: dict[str, int] = {}
        groups: list[list[MarketEntry]] = []
        for item, snapshot in zip(selected, snapshots):
            if isinstance(snapshot, BaseException):
                reason = _error_text(snapshot)
                failures[item.name] = reason
                self._source_errors[item.name] = reason
                self._log("warning", f"市场源 {item.name} 索引不可用：{reason}")
                continue
            self._source_errors.pop(item.name, None)
            hits = [entry for entry in snapshot.entries if _entry_matches(entry, needle)]
            counts[item.name] = len(hits)
            if len(hits) > self._max_entries:
                # 上限只截「单次返回」，索引本身照样整份可搜（不然大商店一半插件会凭空消失）
                truncated[item.name] = len(hits) - self._max_entries
                self._log(
                    "info",
                    f"市场源 {item.name} 命中 {len(hits)} 条，按配置每次只取前 {self._max_entries} 条"
                    "（把 query 写具体一点，或用 [bridge.market].max_entries_per_source 调大）",
                )
                hits = hits[: self._max_entries]
            groups.append(hits)
        if failures and len(failures) == len(selected):
            detail = "；".join(f"{name}: {reason}" for name, reason in failures.items())
            raise MarketError(f"所有市场源都拉不到索引：{detail}")
        matched = _interleave(groups)
        count = int(limit) if limit else 0
        return MarketSearchReport(
            entries=matched[:count] if count > 0 else matched,
            counts=counts,
            truncated=truncated,
            failures=failures,
        )

    async def search(
        self,
        query: str = "",
        *,
        source: str = "",
        limit: int = 20,
        refresh: bool = False,
    ) -> list[MarketEntry]:
        """只要条目的搜索（统计见 :meth:`search_report`）。"""
        report = await self.search_report(query, source=source, limit=limit, refresh=refresh)
        return report.entries

    # ------------------------------------------------------------------- 安装

    async def install(
        self,
        *,
        source: str,
        plugin_id: str,
        overwrite: bool = False,
        refresh: bool = False,
    ) -> InstallResult:
        """安装索引里的一个条目。

        Returns:
            :class:`InstallResult`；失败时 ``ok=False`` 且 ``error`` 非空（不问异常）。
        """
        wanted_source = str(source or "").strip().lower()
        wanted_id = str(plugin_id or "").strip()
        entry = MarketEntry(source=wanted_source, plugin_id=wanted_id)
        plugins_root = self._plugins_root()
        fallback_target = (
            plugins_root / (wanted_source or "unknown") / _safe_dir_name([wanted_id])
            if plugins_root is not None
            else Path(_safe_dir_name([wanted_id]))
        )
        if not self._enabled:
            return self._fail(
                entry,
                fallback_target,
                "插件市场已停用（[bridge.market].enabled = false）",
                code="market_disabled",
            )
        if not self._allow_install:
            return self._fail(
                entry,
                fallback_target,
                "安装已被配置关闭：把 [bridge.market].allow_install 设为 true 后重试"
                "（安装会把第三方代码写进插件目录）",
                code="install_disabled",
            )
        if plugins_root is None:
            # 连装到哪都不知道，就别去拉索引了
            return self._fail(
                entry,
                fallback_target,
                "宿主没有提供插件根目录（plugins_root / data_dir 都拿不到）",
                code="plugins_root_unavailable",
            )
        plugins_root = Path(plugins_root)
        source_obj = self._find_source(wanted_source)
        if source_obj is None:
            available = ", ".join(item.name for item in self._sources) or "(无)"
            return self._fail(
                entry,
                fallback_target,
                f"未知的市场源 '{source}'（可用：{available}）",
                code="unknown_source",
            )
        entry = MarketEntry(source=source_obj.name, plugin_id=wanted_id)

        self._log("info", f"市场安装开始：{source_obj.name}:{wanted_id}（overwrite={overwrite}）")
        try:
            snapshot = await self._snapshot(source_obj, refresh=refresh)
        except Exception as exc:  # noqa: BLE001 — 索引拉不到也算安装失败
            reason = _error_text(exc)
            self._source_errors[source_obj.name] = reason
            return self._fail(entry, fallback_target, f"索引不可用：{reason}", code="source_unavailable")
        resolved = _find_entry(snapshot.entries, wanted_id)
        if resolved is None:
            return self._fail(
                entry,
                fallback_target,
                f"not in index：{source_obj.name}:{wanted_id} 不在索引里"
                f"（索引有 {len(snapshot.entries)} 条，先用 bridge_market_search 查准确 id）",
                code="not_in_index",
            )
        entry = resolved

        try:
            plan = source_obj.plan_install(entry, plugins_root, overwrite=overwrite)
        except MarketError as exc:
            return self._fail(entry, fallback_target, str(exc), code=getattr(exc, "code", "install_failed"))
        target = plan.target_dir
        try:
            _assert_within_plugins_root(plugins_root, target)
        except MarketError as exc:
            return self._fail(entry, fallback_target, str(exc), code=getattr(exc, "code", "install_failed"))
        for warning in plan.warnings:
            self._log("info", f"{entry.source}:{entry.plugin_id} 提示：{warning}")

        requires: tuple[str, ...] = ()
        if entry.install_kind == "pypi-wheel":
            try:
                plan, requires = await self._resolve_wheel_plan(entry, plan)
            except MarketError as exc:
                return self._fail(
                    entry, target, str(exc), code=getattr(exc, "code", "unsupported_install_kind")
                )
        elif entry.install_kind == "pypi":
            return self._fail(entry, target, self._pypi_message(entry, plugins_root), code="unsupported_install_kind")
        if not plan.urls:
            return self._fail(entry, target, self._unsupported_message(entry, plugins_root), code="unsupported_install_kind")
        if target.exists() and _has_entries(target):
            occupied = _read_provenance(target)
            owner = str(occupied.get("plugin_id") or "") if occupied else ""
            if overwrite and owner and owner != entry.plugin_id:
                return self._fail(
                    entry,
                    target,
                    f"目标目录已被另一个插件占用（{owner}），拒绝覆盖：{target}",
                    code="target_occupied",
                )
            if not overwrite:
                return self._fail(
                    entry,
                    target,
                    "already installed (pass overwrite=true)",
                    code="already_installed",
                    log_level="info",
                )

        errors: list[str] = []
        payload = b""
        chosen = ""
        for url in plan.urls:
            try:
                payload = await self._fetcher.get_bytes(
                    url, timeout=self._timeout, max_bytes=self._max_archive_bytes
                )
            except Exception as exc:  # noqa: BLE001 — 每个候选地址失败都记下来继续试
                errors.append(f"{_short_url(url)}: {_error_text(exc)}")
                continue
            chosen = url
            break
        if not chosen:
            return self._fail(
                entry,
                target,
                f"{len(errors)} 个下载地址都失败了：{'；'.join(errors[:6])}",
                code="download_failed",
            )

        digest = hashlib.sha256(payload).hexdigest()
        shape = _shape_for(entry.source, entry)
        try:
            files, warning, wheel_requires = await asyncio.to_thread(
                _install_archive,
                payload,
                plan,
                shape=shape,
                max_extract_bytes=self._max_extract_bytes,
                max_files=self._max_files,
            )
            _write_provenance(
                target,
                {
                    "source": entry.source,
                    "plugin_id": entry.plugin_id,
                    "name": entry.name,
                    "version": entry.version,
                    "url": chosen,
                    "archive_sha256": digest,
                    "install_kind": entry.install_kind,
                    "requires": list(requires),
                    "installed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception as exc:  # noqa: BLE001 — 解压/落盘失败要回滚成干净的失败
            try:
                _remove_target(target)
            except MarketError as cleanup_error:
                self._log("warning", f"回滚目标目录失败（{target}）：{cleanup_error}")
            code = getattr(exc, "code", "") if isinstance(exc, MarketError) else ""
            # 具体失败码一路带上去（path_traversal / too_many_files / manifest_missing …），
            # 没带码的才退回笼统的 install_failed。
            return self._fail(
                entry,
                target,
                f"解压或落盘失败：{_error_text(exc)}",
                code=code if code and code != "market_error" else "install_failed",
            )
        if warning:
            self._log("warning", f"{entry.source}:{entry.plugin_id} 装好了但有隐患：{warning}")

        requires = tuple(
            dict.fromkeys([*requires, *wheel_requires, *_read_requirements(target)])
        )
        if requires:
            self._log(
                "info",
                f"{entry.source}:{entry.plugin_id} 声明了额外依赖：{', '.join(requires[:12])}"
                f"{' …' if len(requires) > 12 else ''}（N.E.K.O. 运行时里没有的话，插件会在加载时报出来）",
            )
        self._log("info", f"市场安装完成：{entry.source}:{entry.plugin_id} → {target}（{files} 个文件）")
        return InstallResult(
            ok=True,
            entry=entry,
            target_dir=target,
            url=chosen,
            files=files,
            archive_sha256=digest,
            requires=requires,
        )

    async def _resolve_wheel_plan(
        self, entry: MarketEntry, plan: InstallPlan
    ) -> tuple[InstallPlan, tuple[str, ...]]:
        """把 ``pypi-wheel`` 条目解析成一条 wheel 下载地址（并带回它声明的依赖）。"""
        project = _text(entry.raw.get("project_link")) or entry.plugin_id
        meta = await self._fetcher.get_json(f"https://pypi.org/pypi/{project}/json", timeout=self._timeout)
        record = _require_mapping(meta, f"PyPI {project}")
        urls = record.get("urls")
        candidates = [item for item in urls if isinstance(item, Mapping)] if isinstance(urls, list) else []
        wheels = [item for item in candidates if _text(item.get("packagetype")) == "bdist_wheel"]
        if not wheels:
            raise MarketError(
                f"PyPI 上 {project} 没有 wheel（只有源码包）：市场不代管构建，请手动安装",
                code="archive_invalid",
            )
        wheels.sort(key=lambda item: (0 if "py3-none-any" in _text(item.get("filename")) else 1, _text(item.get("filename"))))
        chosen = wheels[0]
        url = _text(chosen.get("url"))
        if not url:
            raise MarketError(f"PyPI 条目 {project} 缺下载地址", code="archive_invalid")
        requires = _requires_from_pypi(record)
        warnings = list(plan.warnings)
        if requires:
            warnings.append(f"wheel 声明依赖：{', '.join(requires[:12])}")
        resolved = InstallPlan(
            entry=entry,
            target_dir=plan.target_dir,
            urls=(url,),
            strip_prefix=True,
            warnings=tuple(warnings),
        )
        return resolved, requires

    # ------------------------------------------------------------ 内部：索引

    async def _snapshot(self, source: MarketSource, *, refresh: bool) -> _IndexSnapshot:
        """拿索引（内存 → 磁盘 → 网络），必要时刷新缓存。"""
        now = time.time()
        if not refresh:
            cached = self._cache.get(source.name)
            if cached is not None and self._fresh(cached, now):
                return cached
            disk = self._read_cache(source.name, source.index_url)
            if disk is not None:
                fetched_at, payload = disk
                if now - fetched_at <= self._cache_ttl:
                    entries = self._parse(source, payload)
                    snapshot = _IndexSnapshot(
                        entries=entries, fetched_at=fetched_at, cached=True, index_url=source.index_url
                    )
                    self._cache[source.name] = snapshot
                    return snapshot
        payload = await self._fetcher.get_json(source.index_url, timeout=self._timeout)
        entries = self._parse(source, payload)
        snapshot = _IndexSnapshot(
            entries=entries, fetched_at=now, cached=False, index_url=source.index_url
        )
        self._cache[source.name] = snapshot
        self._write_cache(source.name, source.index_url, payload, now)
        return snapshot

    def _peek(self, source: MarketSource) -> _IndexSnapshot | None:
        """只看缓存，**不联网**（给状态查询用；冷启动返回 None）。"""
        now = time.time()
        cached = self._cache.get(source.name)
        if cached is not None and self._fresh(cached, now):
            return cached
        disk = self._read_cache(source.name, source.index_url)
        if disk is None:
            return None
        fetched_at, payload = disk
        if now - fetched_at > self._cache_ttl:
            return _IndexSnapshot(entries=[], fetched_at=fetched_at, cached=True, index_url=source.index_url)
        try:
            entries = self._parse(source, payload)
        except MarketError:
            return None
        snapshot = _IndexSnapshot(
            entries=entries, fetched_at=fetched_at, cached=True, index_url=source.index_url
        )
        self._cache[source.name] = snapshot
        return snapshot

    def _fresh(self, snapshot: _IndexSnapshot, now: float) -> bool:
        return (now - snapshot.fetched_at) <= self._cache_ttl

    def _parse(self, source: MarketSource, payload: object) -> list[MarketEntry]:
        try:
            parsed = source.parse(payload)
        except MarketError:
            raise
        except Exception as exc:  # noqa: BLE001 — 第三方源的意外异常也要变成结构化错误
            raise MarketError(f"{source.name} 索引解析失败：{type(exc).__name__}: {exc}") from exc
        if not isinstance(parsed, list):
            raise MarketError(
                f"{source.name}.parse 没有返回条目列表（实际 {type(parsed).__name__}）"
            )
        for item in parsed:
            if not isinstance(item, MarketEntry):
                raise MarketError(
                    f"{source.name}.parse 返回了非 MarketEntry 的条目（{type(item).__name__}）"
                )
        entries = list(parsed)
        return entries

    def _cache_dir(self) -> Path | None:
        cache_dir = getattr(self._host, "cache_dir", None)
        if not callable(cache_dir):
            return None
        try:
            return Path(cache_dir(CACHE_NAMESPACE))
        except Exception as exc:  # noqa: BLE001 — 拿不到缓存目录就只走内存
            self._log("debug", f"市场缓存目录不可用：{type(exc).__name__}: {exc}")
            return None

    def _cache_path(self, name: str) -> Path | None:
        cache_dir = self._cache_dir()
        if cache_dir is None or not name:
            return None
        return cache_dir / f"{_safe_dir_name([name])}.json"

    def _read_cache(self, name: str, index_url: str) -> tuple[float, object] | None:
        path = self._cache_path(name)
        if path is None or not path.is_file():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._log("warning", f"市场缓存损坏，已忽略并重新拉取（{path.name}）：{exc}")
            return None
        if not isinstance(raw, Mapping) or "fetched_at" not in raw or "payload" not in raw:
            self._log("warning", f"市场缓存格式不对，已忽略并重新拉取：{path.name}")
            return None
        cached_url = _text(raw.get("index_url"))
        if cached_url and cached_url != str(index_url):
            self._log(
                "warning",
                f"市场源的索引地址变了（缓存 {cached_url} → 现在 {index_url}），缓存作废重新拉取",
            )
            return None
        try:
            fetched_at = float(raw["fetched_at"])  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            self._log("warning", f"市场缓存时间戳不对，已忽略并重新拉取（{path.name}）：{exc}")
            return None
        return fetched_at, raw["payload"]

    def _write_cache(self, name: str, index_url: str, payload: object, fetched_at: float) -> None:
        path = self._cache_path(name)
        if path is None:
            return
        temp = path.with_suffix(".json.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(
                json.dumps(
                    {"fetched_at": fetched_at, "index_url": str(index_url), "payload": payload},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            os.replace(temp, path)
        except (OSError, TypeError, ValueError) as exc:
            self._log("warning", f"市场缓存写入失败（不影响本次结果）：{type(exc).__name__}: {exc}")
            try:
                temp.unlink(missing_ok=True)
            except OSError as cleanup_error:
                self._log("debug", f"清理临时缓存文件失败：{cleanup_error}")

    # ------------------------------------------------------------ 内部：杂项

    def _dedupe_sources(self, sources: Sequence[MarketSource]) -> list[MarketSource]:
        unique: list[MarketSource] = []
        seen: set[str] = set()
        for item in sources:
            name = str(getattr(item, "name", "") or "").strip()
            if not name:
                self._skipped_sources.append(repr(item))
                continue
            if name in seen:
                self._skipped_sources.append(name)
                continue
            seen.add(name)
            unique.append(item)
        if self._skipped_sources:
            self._log("warning", f"忽略重复/无名的市场源：{', '.join(self._skipped_sources)}")
        return unique

    def _find_source(self, name: str) -> MarketSource | None:
        for item in self._sources:
            if item.name.strip().lower() == name:
                return item
        return None

    def _select_sources(self, source: str) -> list[MarketSource]:
        key = str(source or "").strip().lower()
        if not key:
            return list(self._sources)
        exact = self._find_source(key)
        if exact is not None:
            return [exact]
        matches = [
            item
            for item in self._sources
            if item.display_name.strip().lower() == key or item.name.strip().lower().startswith(key)
        ]
        if len(matches) == 1:
            return matches
        available = ", ".join(item.name for item in self._sources) or "(无)"
        raise MarketError(f"未知的市场源 '{source}'（可用：{available}）")

    def _plugins_root(self) -> Path | None:
        """插件根目录：优先宿主的 ``plugins_root``，其次「数据目录/plugins」。"""
        value = getattr(self._host, "plugins_root", None)
        if value:
            return Path(str(value))
        data_dir = getattr(self._host, "data_dir", None)
        if not callable(data_dir):
            return None
        try:
            base = Path(str(data_dir("")))
        except Exception as exc:  # noqa: BLE001 — 宿主没实现 data_dir 时老实报缺
            self._log("debug", f"市场拿不到数据目录：{type(exc).__name__}: {exc}")
            return None
        return base / "plugins"

    def _unsupported_message(self, entry: MarketEntry, plugins_root: Path) -> str:
        """「没有可下载地址」这类失败要说清楚是索引里没给，还是给的地址用不了。"""
        raw_repo = _text(entry.raw.get("repo")) or _text(entry.raw.get("repository_url"))
        if entry.repository:
            why = f"仓库地址不可用（{entry.repository}）"
        elif raw_repo:
            why = f"仓库地址不是可用的 GitHub 仓库（索引里写的是 {raw_repo}）"
        else:
            why = "索引里既没有仓库地址也没有直链"
        return (
            f"该条目没有可下载的地址（install_kind={entry.install_kind or 'unknown'}）：{why}；"
            f"市场只从 GitHub 仓库压缩包装，请手动把插件放进 {plugins_root}/{entry.source}/"
        )

    def _pypi_message(self, entry: MarketEntry, plugins_root: Path) -> str:
        package = _text(entry.raw.get("project_link")) or _text(entry.raw.get("name")) or entry.plugin_id
        return (
            f"{entry.source}:{entry.plugin_id} 只能从 PyPI 安装（包名 {package}）："
            "本桥不代管第三方依赖，所以不装。两条出路："
            f"① 自己 pip install {package} 后把插件目录放进 {plugins_root}/{entry.source}/；"
            "② 换一个带仓库地址的插件。"
        )

    def _fail(
        self,
        entry: MarketEntry,
        target_dir: Path,
        error: str,
        *,
        code: str = "install_failed",
        log_level: str = "warning",
    ) -> InstallResult:
        if log_level:
            self._log(log_level, f"市场安装失败 [{code}] {entry.source}:{entry.plugin_id}：{error}")
        return InstallResult(ok=False, entry=entry, target_dir=target_dir, error=error, code=code)

    def _log(self, level: str, message: str) -> None:
        log = getattr(self._host, "log", None)
        if not callable(log):
            return
        try:
            log(level, message, source=CACHE_NAMESPACE)
        except Exception:  # noqa: BLE001 — 日志实现有问题也不能影响市场流程
            self._dropped_logs += 1


# --------------------------------------------------------------------------- #
# 解压 / 落盘
# --------------------------------------------------------------------------- #


def _install_archive(
    payload: bytes,
    plan: InstallPlan,
    *,
    shape: _PluginShape | None,
    max_extract_bytes: int,
    max_files: int,
) -> tuple[int, str, tuple[str, ...]]:
    """把压缩包安全地解到 ``plan.target_dir``，返回 ``(文件数, 告警文案, 声明的依赖)``。

    依赖取自解压出来的 ``*.dist-info/METADATA``（wheel 会带，zip 通常没有）；
    这些 dist-info 不会跟着插件落到目标目录，所以必须在这里读。

    先解到目标同级的暂存目录，校验出「真正的插件目录」之后再搬过去；
    任何一步失败都不会在 ``plugins_root`` 里留下半个插件。
    """
    target = plan.target_dir
    framework_dir = target.parent
    framework_dir.mkdir(parents=True, exist_ok=True)
    staging = framework_dir / f".market-{target.name}-{uuid.uuid4().hex[:8]}"
    if not _inside(framework_dir, staging):  # 理论上不可能，但绝不在检查上偷懒
        raise MarketError(f"暂存目录越界：{staging}", code="install_failed")
    payload_dir = staging / "payload"
    try:
        payload_dir.mkdir(parents=True, exist_ok=True)
        first_url = str((plan.urls or ("",))[0]).lower()
        is_tgz = first_url.endswith((".tgz", ".tar.gz")) or payload[:2] == b"\x1f\x8b"
        if is_tgz:
            _unpack_tgz(
                payload, payload_dir, max_extract_bytes=max_extract_bytes, max_files=max_files
            )
        else:
            try:
                archive = zipfile.ZipFile(io.BytesIO(payload))
            except zipfile.BadZipFile as exc:
                raise MarketError(f"下载内容不是合法 zip（可能被网关改成了错误页）：{exc}", code="archive_invalid") from exc
            try:
                _unpack_zip(
                    archive, payload_dir, max_extract_bytes=max_extract_bytes, max_files=max_files
                )
            finally:
                archive.close()

        base = payload_dir
        if plan.strip_prefix:
            base = _strip_single_root(base)
        root, error = _locate_plugin_root(base, shape)
        if root is None:
            raise MarketError(error, code="manifest_missing")
        warning = shape.entry_warning(root) if shape is not None else ""
        requires = _read_wheel_metadata(root) or _read_wheel_metadata(payload_dir)

        if target.exists():
            _remove_target(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        _move_tree(root, target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    files = sum(1 for path in target.rglob("*") if path.is_file())
    return files, warning, requires


def _unpack_tgz(
    payload: bytes, dest: Path, *, max_extract_bytes: int, max_files: int
) -> None:
    """保守解压 npm 风格的 ``.tgz``；成员校验与 zip 路径同一套规则。"""
    import tarfile  # 局部导入：只有 koishi 源会走到这里

    entries_seen = 0
    written = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            members = archive.getmembers()
            if len(members) > max_files * 4:
                raise MarketError(
                    f"压缩包条目过多（{len(members)} 条 > {max_files * 4} 条上限），拒绝解压",
                    code="too_many_files",
                )
            for member in members:
                name = str(member.name).replace("\\", "/")
                if not name:
                    continue
                if name.startswith("/") or _ABSOLUTE_PATH_RE.match(name):
                    raise MarketError(f"压缩包成员是绝对路径：{member.name}", code="path_traversal")
                parts = [part for part in name.split("/") if part not in ("", ".")]
                if any(part == ".." for part in parts):
                    raise MarketError(
                        f"压缩包成员含上级目录（..）：{member.name}", code="path_traversal"
                    )
                if member.issym() or member.islnk() or member.isdev():
                    raise MarketError(f"压缩包成员是链接/设备文件：{member.name}")
                lowered = [part.lower() for part in parts]
                if any(part in _SKIP_DIR_NAMES for part in lowered):
                    continue
                if parts and parts[-1].lower().endswith(_SKIP_SUFFIXES):
                    continue
                if not member.isfile():
                    continue
                entries_seen += 1
                if entries_seen > max_files:
                    raise MarketError(
                        f"解压文件数超过上限（{max_files}），拒绝继续", code="too_many_files"
                    )
                declared = max(0, member.size)
                written += declared
                if written > max_extract_bytes:
                    raise MarketError(
                        f"解压后体积超过上限（{written} 字节 > {max_extract_bytes} 字节）",
                        code="extract_too_large",
                    )
                target = dest.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    continue
                with target.open("wb") as handle:
                    shutil.copyfileobj(source, handle, 1024 * 64)
    except tarfile.TarError as exc:
        raise MarketError(f"下载内容不是合法的 .tgz：{exc}", code="archive_invalid") from exc


def _unpack_zip(
    archive: zipfile.ZipFile, dest: Path, *, max_extract_bytes: int, max_files: int
) -> None:
    """保守解压；任何可疑成员、超量成员都直接拒绝（解压总量实时记账）。"""
    infos = archive.infolist()
    if len(infos) > max_files * 4:
        raise MarketError(
            f"压缩包条目过多（{len(infos)} 条 > {max_files * 4} 条上限），拒绝解压",
            code="too_many_files",
        )
    declared = sum(max(0, info.file_size) for info in infos if not info.is_dir())
    if declared > max_extract_bytes:
        raise MarketError(
            f"解压后体积超过上限（声明 {declared} 字节 > {max_extract_bytes} 字节）",
            code="extract_too_large",
        )
    files = 0
    written = 0
    for info in infos:
        parts = _member_parts(info)
        if parts is None:
            continue
        member_target = dest / Path(*parts)
        if not _inside(dest, member_target):
            raise MarketError(
                f"压缩包成员会写到目标目录外面：{info.filename}", code="path_traversal"
            )
        if info.is_dir():
            member_target.mkdir(parents=True, exist_ok=True)
            continue
        if files >= max_files:
            raise MarketError(f"文件数超过上限（{max_files}），拒绝解压", code="too_many_files")
        member_target.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info) as source, member_target.open("wb") as sink:
            while True:
                chunk = source.read(_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_extract_bytes:
                    raise MarketError(f"解压总量超过上限（{max_extract_bytes} 字节），拒绝解压", code="extract_too_large")
                sink.write(chunk)
        files += 1


def _member_parts(info: zipfile.ZipInfo) -> list[str] | None:
    """校验一个 zip 成员，返回相对路径分量；该跳过时返回 ``None``，可疑时抛错。"""
    raw_name = str(info.filename).replace("\\", "/")
    if not raw_name:
        return None
    if raw_name.startswith("/") or _ABSOLUTE_PATH_RE.match(raw_name):
        raise MarketError(f"压缩包成员是绝对路径：{info.filename}", code="path_traversal")
    mode = info.external_attr >> 16
    if mode and (mode & 0o170000) == 0o120000:
        raise MarketError(f"压缩包成员是符号链接：{info.filename}")
    parts = [part for part in raw_name.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise MarketError(f"压缩包成员含上级目录（..）：{info.filename}", code="path_traversal")
    if not parts:
        return None
    lowered = [part.lower() for part in parts]
    if any(part in _SKIP_DIR_NAMES for part in lowered):
        return None
    if parts[-1].lower().endswith(_SKIP_SUFFIXES):
        return None
    if info.compress_size == 0 and info.file_size > 0:
        raise MarketError(f"压缩包成员声明可疑（压缩后 0 字节却要写 {info.file_size} 字节）：{info.filename}")
    return parts


def _strip_single_root(base: Path) -> Path:
    """GitHub 归档的顶层是 ``<仓库>-<分支>/``：只有一个目录时剥掉它。"""
    try:
        children = sorted(base.iterdir(), key=lambda path: path.name)
    except OSError as exc:
        raise MarketError(f"读不了解压出来的目录 {base.name}：{exc}") from exc
    directories = [path for path in children if path.is_dir()]
    files = [path for path in children if path.is_file()]
    if len(directories) == 1 and not files:
        return directories[0]
    return base


def _locate_plugin_root(base: Path, shape: _PluginShape | None) -> tuple[Path | None, str]:
    """在解压结果里找出「该放进插件根目录的那一层」。"""
    if not base.is_dir():
        return None, "压缩包里没有目录"
    if shape is None:
        return base, ""
    found: list[tuple[int, int, Path]] = []
    for depth in range(3):
        for path in _dirs_at_depth(base, depth):
            if shape.matches(path):
                found.append((shape.rank(path), depth, path))
    if found:
        best_rank = min(item[0] for item in found)
        preferred = [item for item in found if item[0] == best_rank]
        deepest = max(item[1] for item in preferred)
        at_deepest = [item[2] for item in preferred if item[1] == deepest]
        if len(at_deepest) == 1:
            return at_deepest[0], ""
        names = ", ".join(sorted(path.name for path in at_deepest)[:6])
        return None, (
            f"压缩包里找到 {len(at_deepest)} 个 {shape.label} 插件目录（{names}），"
            "不敢替用户猜哪一个，已放弃安装"
        )
    hint = shape.error_hint(base)
    if hint:
        return None, hint
    return None, (
        f"压缩包里没找到 {shape.label} 插件目录；顶层内容：{_listing(base)}"
    )


def _dirs_at_depth(base: Path, depth: int) -> list[Path]:
    """``base`` 往下 ``depth`` 层的目录（跳过点开头的目录，例如 ``.git``）。"""
    current = [base]
    for _ in range(depth):
        following: list[Path] = []
        for directory in current:
            try:
                children = sorted(
                    (
                        path
                        for path in directory.iterdir()
                        if path.is_dir() and not path.name.startswith(".")
                    ),
                    key=lambda path: path.name,
                )
            except OSError:
                children = []
            following.extend(children)
        current = following
        if not current:
            break
    return current


def _move_tree(source: Path, target: Path) -> None:
    """把解压好的插件目录搬进目标（同一分区时是原子 rename）。"""
    try:
        os.replace(source, target)
    except OSError as rename_error:
        try:
            shutil.move(str(source), str(target))
        except OSError as exc:
            raise MarketError(f"把插件目录搬到 {target} 失败：{exc}") from exc
        if not target.is_dir():
            raise MarketError(f"搬到 {target} 之后目标不是目录（rename 失败原因：{rename_error}）")


def _remove_target(path: Path) -> None:
    """删掉一个已存在的目标（文件/符号链接/目录都容忍）。"""
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
            return
        if path.is_dir():
            shutil.rmtree(path)
    except OSError as exc:
        raise MarketError(f"删除已有目标目录失败（{path}）：{exc}") from exc


def _has_entries(path: Path) -> bool:
    """目录里是否有东西（空目录视为「没装过」）。"""
    if not path.is_dir():
        return True
    try:
        return next(iter(path.iterdir()), None) is not None
    except OSError:
        return True


def _requirement_name(line: str) -> str:
    """从一行依赖声明里取出包名（``feedparser>=6 ; python_version>="3"`` → ``feedparser``）。"""
    text = str(line or "").strip()
    if not text or text.startswith("#") or text.startswith("-"):
        return ""
    text = text.split("#", 1)[0].strip()
    if not text:
        return ""
    if "://" in text or text.startswith("git+"):
        return ""
    # 名字后面只允许跟版本约束 / 环境标记 / extras；否则这条声明是坏的，不猜。
    name_part, _, rest = text.partition(" ")
    if rest.strip():
        tail = rest.strip()
        if tail[0] not in "<>=!~,;[(.":
            return ""
    text = name_part
    for separator in (";", "[", "(", ">", "<", "=", "!", "~", ","):
        index = text.find(separator)
        if index > 0:
            text = text[:index]
    name = text.strip().strip("'\"").strip()
    return name if name and all(ch.isalnum() or ch in "-_." for ch in name) else ""


def _requires_from_pypi(record: Mapping[str, object]) -> tuple[str, ...]:
    """PyPI JSON 的 ``info.requires_dist`` → 去重的包名列表。"""
    info = record.get("info")
    if not isinstance(info, Mapping):
        return ()
    raw = info.get("requires_dist")
    if not isinstance(raw, list):
        return ()
    names: list[str] = []
    for item in raw:
        name = _requirement_name(str(item))
        if name and name not in names:
            names.append(name)
        if len(names) >= 40:
            break
    return tuple(names)


def _read_wheel_metadata(target: Path) -> tuple[str, ...]:
    """从 wheel 留下的 ``*.dist-info/METADATA`` 里读 ``Requires-Dist``（PyPI JSON 的兜底）。"""
    for dist_info in sorted(target.glob("*.dist-info")):
        meta = dist_info / "METADATA"
        if not meta.is_file():
            continue
        try:
            lines = meta.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        names: list[str] = []
        for line in lines:
            if not line.lower().startswith("requires-dist:"):
                continue
            name = _requirement_name(line.split(":", 1)[1])
            if name and name not in names:
                names.append(name)
            if len(names) >= 40:
                break
        if names:
            return tuple(names)
    return ()


def _read_requirements(target: Path) -> tuple[str, ...]:
    """读插件目录里的 ``requirements.txt``（顶层或一层子目录），返回包名。"""
    candidates: list[Path] = [target / "requirements.txt"]
    try:
        for child in sorted(target.iterdir()):
            if child.is_dir() and not child.name.startswith("."):
                candidates.append(child / "requirements.txt")
    except OSError:
        return ()
    for path in candidates:
        if not path.is_file():
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        names: list[str] = []
        for line in lines:
            name = _requirement_name(line)
            if name and name not in names:
                names.append(name)
            if len(names) >= 40:
                break
        if names:
            return tuple(names)
    return ()


def _write_provenance(target: Path, record: Mapping[str, object]) -> None:
    """把安装溯源写进插件目录（来源 / id / 版本 / URL / sha256 / 时间）。"""
    path = target / PROVENANCE_FILE
    try:
        path.write_text(
            json.dumps(dict(record), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise MarketError(f"写溯源文件 {PROVENANCE_FILE} 失败：{exc}") from exc


def _read_provenance(target: Path) -> dict[str, object]:
    """读目标目录里的溯源文件；没有或坏了都返回空字典。"""
    path = target / PROVENANCE_FILE
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #


def _plan_target(
    framework: str,
    entry: MarketEntry,
    plugins_root: Path,
    *,
    candidates: Sequence[str],
) -> Path:
    """算出 ``plugins_root/<框架>/<安全目录名>``，并确保没有跑出 ``plugins_root``。"""
    root = Path(plugins_root)
    name = _safe_dir_name(candidates, fallback=entry.plugin_id or entry.name or entry.source)
    target = root / str(framework) / name
    _assert_within_plugins_root(root, target)
    return target


def _assert_within_plugins_root(plugins_root: Path, target: Path) -> None:
    """目标必须落在 ``plugins_root`` 里面（自定义源给的计划也要过这一关）。"""
    root_resolved = _resolve(Path(plugins_root))
    target_resolved = _resolve(Path(target))
    if target_resolved == root_resolved or not target_resolved.is_relative_to(root_resolved):
        raise MarketError(f"拒绝把插件装到 plugins_root 外面：{target}")


def _resolve(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.abspath(str(path)))


def _inside(base: Path, path: Path) -> bool:
    """``path`` 是否落在 ``base`` 里（含解析后的比较）。"""
    base_resolved = _resolve(base)
    path_resolved = _resolve(path)
    return path_resolved == base_resolved or path_resolved.is_relative_to(base_resolved)


def _safe_dir_name(candidates: Sequence[str], *, fallback: str = _FALLBACK_DIR_NAME) -> str:
    """把候选名字洗成安全的目录名：``[A-Za-z0-9._-]``，≤64 字符。"""
    for raw in tuple(candidates) + (fallback,):
        cleaned = _DIR_NAME_RE.sub("-", str(raw or "").strip())
        cleaned = _SEPARATOR_RUN_RE.sub("-", cleaned).strip("-._")
        if not cleaned or cleaned.lower() in _RESERVED_DIR_NAMES:
            continue
        cleaned = cleaned[:_MAX_DIR_NAME].strip("-._")
        if cleaned and cleaned.lower() not in _RESERVED_DIR_NAMES:
            return cleaned
    return _FALLBACK_DIR_NAME


def _github_repo(url: str) -> tuple[str, str] | None:
    """``https://github.com/<owner>/<repo>[.git][/…]`` → ``(owner, repo)``。"""
    match = _GITHUB_RE.match(str(url or "").strip())
    if match is None:
        return None
    owner = match.group("owner")
    repo = match.group("repo")
    if not owner or not repo:
        return None
    return owner, repo


def _github_repo_url(url: str) -> str:
    """归一化成 ``https://github.com/<owner>/<repo>``；不是 GitHub 仓库则返回空串。"""
    repo = _github_repo(url)
    if repo is None:
        return ""
    return f"https://github.com/{repo[0]}/{repo[1]}"


def _github_archive_urls(repository: str) -> tuple[str, ...]:
    """GitHub 仓库 → 候选压缩包地址（main → master → codeload，与上游用法一致）。"""
    repo = _github_repo(repository)
    if repo is None:
        return ()
    owner, name = repo
    base = f"https://github.com/{owner}/{name}"
    codeload = f"https://codeload.github.com/{owner}/{name}/zip/refs/heads"
    return (
        f"{base}/archive/refs/heads/main.zip",
        f"{base}/archive/refs/heads/master.zip",
        f"{codeload}/main",
        f"{codeload}/master",
    )


def _url_basename(url: str) -> str:
    """URL 最后一段（去掉 ``.git``）；拿不到返回空串。"""
    text = str(url or "").strip()
    if not text:
        return ""
    path = urllib.parse.urlsplit(text).path.rstrip("/")
    tail = path.rsplit("/", 1)[-1]
    return tail[:-4] if tail.endswith(".git") else tail


def _normalize_plugin_name(value: str) -> str:
    """把 ``nonebot_plugin_foo`` / ``nonebot-plugin-foo`` / ``foo`` 归一成同一个键。"""
    text = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    for prefix in ("nonebotplugin", "nonebot", "plugin"):
        if text.startswith(prefix) and len(text) > len(prefix):
            text = text[len(prefix) :]
            break
    return text


def _nonebot_repo_matches(module_name: str, project_link: str, repository: str) -> bool:
    """仓库名与插件名对得上才敢用仓库压缩包装（否则可能是装一堆插件的 monorepo）。"""
    slug = _url_basename(repository)
    if not slug:
        return False
    normalized_slug = _normalize_plugin_name(slug)
    if not normalized_slug:
        return False
    for candidate in (module_name, project_link):
        normalized = _normalize_plugin_name(candidate)
        if normalized and normalized == normalized_slug:
            return True
    return False


def _find_entry(entries: Sequence[MarketEntry], plugin_id: str) -> MarketEntry | None:
    """按 id 精确找；找不到再试大小写不敏感的**唯一**命中。"""
    for entry in entries:
        if entry.plugin_id == plugin_id:
            return entry
    lowered = plugin_id.lower()
    matches = [entry for entry in entries if entry.plugin_id.lower() == lowered]
    return matches[0] if len(matches) == 1 else None


def _interleave(groups: Sequence[Sequence[MarketEntry]]) -> list[MarketEntry]:
    """按源轮流取条目：groups = [[maibot...], [nonebot...], [astrbot...]] → m,n,a,m,n,a…

    跨源搜索必须公平：按源顺序拼接后截断时，排在最前、且在索引里命中够多的那个源会占满
    ``limit``（默认顺序 maibot 打头，真机实测 20 条全是 maibot），后面的源在面板上看起来
    像「不存在」。轮转保证 ``limit`` 内每个源都能露脸。
    """
    merged: list[MarketEntry] = []
    depth = max((len(group) for group in groups), default=0)
    for index in range(depth):
        for group in groups:
            if index < len(group):
                merged.append(group[index])
    return merged


def _entry_matches(entry: MarketEntry, needle: str) -> bool:
    if not needle:
        return True
    haystack = "\n".join(
        (entry.plugin_id, entry.name, entry.description, entry.author, entry.repository)
    ).lower()
    return needle in haystack


def _market_settings(host: HostServices) -> dict[str, object]:
    """取 ``host.settings["market"]``（没有 / 类型不对时返回空配置）。"""
    settings = getattr(host, "settings", None)
    if not isinstance(settings, Mapping):
        return {}
    section = settings.get("market")
    if not isinstance(section, Mapping):
        return {}
    return dict(section)


def _require_list(payload: object, label: str) -> list:
    if not isinstance(payload, list):
        raise MarketError(
            f"{label} 索引形态不符：期望顶层是 list（条目数组），实际是 {type(payload).__name__}"
        )
    return payload


def _require_mapping(payload: object, label: str) -> Mapping:
    if not isinstance(payload, Mapping):
        raise MarketError(
            f"{label} 形态不符：期望 object，实际是 {type(payload).__name__}"
            + (f"（内容：{_preview(payload)}）" if not isinstance(payload, (list, tuple)) else "")
        )
    return payload


def _text(value: object) -> str:
    if value is None or isinstance(value, (Mapping, list, tuple, set)):
        return ""
    return str(value).strip()


def _author_text(value: object) -> str:
    """作者字段可能是字符串，也可能是 ``{"name": …, "url": …}``。"""
    if isinstance(value, Mapping):
        return _text(value.get("name")) or _text(value.get("id"))
    return _text(value)


def _keys(record: Mapping) -> str:
    return ", ".join(sorted(str(key) for key in record)[:10]) or "(空)"


def _preview(value: object, limit: int = 120) -> str:
    text = str(value).replace("\n", " ")
    return text[:limit] + ("…" if len(text) > limit else "")


def _listing(path: Path, limit: int = 12) -> str:
    try:
        names = sorted(item.name for item in path.iterdir())
    except OSError as exc:
        return f"(列不出来：{exc})"
    if not names:
        return "(空目录)"
    extra = f" 等 {len(names)} 项" if len(names) > limit else ""
    return ", ".join(names[:limit]) + extra


def _short_url(url: str, limit: int = 120) -> str:
    text = str(url or "")
    return text if len(text) <= limit else f"{text[:limit]}…"


def _string_list(value: object) -> list[str]:
    """配置里「字符串或字符串列表」的统一读法。"""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _as_bool(value: object, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0"):
        return False
    return default


def _as_int(value: object, default: int, *, minimum: int) -> int:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, number)


def _as_float(value: object, default: float, *, minimum: float) -> float:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return max(minimum, number)


def _error_text(exc: BaseException) -> str:
    if isinstance(exc, BridgeError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


__all__ = [
    "CACHE_NAMESPACE",
    "DEFAULT_ALLOW_HOSTS",
    "DEFAULT_MIRROR_PREFIXES",
    "FRAMEWORKS",
    "PROVENANCE_FILE",
    "AstrBotSource",
    "HttpFetcher",
    "InstallPlan",
    "InstallResult",
    "MaiBotSource",
    "Market",
    "MarketEntry",
    "MarketError",
    "MarketSource",
    "NoneBotSource",
    "build_sources",
    "default_fetcher",
]
