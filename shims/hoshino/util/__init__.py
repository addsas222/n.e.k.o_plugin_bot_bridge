"""``hoshino.util`` 的兼容实现。

上游 ``hoshino/util/`` 是个包（``load_config`` / ``pic2b64`` / ``util.filt_message`` / 频率
限制器…）。这里给同一批名字，落地规则是：

* ``load_config`` / ``save_config`` —— 读写的 JSON 落在 ``host.data_dir(plugin_id)/config/``
  （``save_config`` 写这里；``load_config`` 先看这里，再看上游位置 ``<__file__ 所在目录>/config.json``），
  因此插件目录可以保持只读；配置文件不存在时返回 ``{}``（与上游一致，只写一条 debug 日志）；
* ``get_group_member_info`` / ``get_user_info`` —— N.E.K.O. 没有群成员查询接口；只有"当前这条
  消息的发送者"是已知的，因此仅在查询对象与当前事件一致时返回（并在结果里标注
  ``_source='event'``），否则抛 :class:`UnsupportedCapabilityError`；
* ``delete_msg`` / ``silence``（撤回 / 禁言）—— 显式 ``UnsupportedCapabilityError``；
* ``pic2b64`` / ``fig2b64`` / ``image_to_base64`` —— 纯标准库实现（bytes / 路径 /
  ``base64://`` / PIL 风格对象都支持；``http(s)://`` 不联网，显式不支持）；
* ``filt_message`` / ``gfw`` —— 内置一个 DFA 敏感词过滤器；**没有词库时显式抛能力缺失**
  （绝不假装过滤过），用 ``gfw.parse(路径或词表)`` 装载词库；
* ``FreqLimiter`` / ``DailyNumberLimiter`` —— 与上游同形；时区用固定 UTC+8（不引入 pytz）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from aiocqhttp import _runtime as output
from aiocqhttp._runtime import UnsupportedCapabilityError
from aiocqhttp.message import Message, MessageSegment

from hoshino import _runtime
from hoshino.service import Scheduler

__all__ = [
    "DailyNumberLimiter",
    "FreqLimiter",
    "Scheduler",
    "concat_pic",
    "date_name",
    "delete_msg",
    "fig2b64",
    "filt_message",
    "get_bot",
    "get_bots",
    "get_group_member_info",
    "get_self_ids",
    "get_user_info",
    "gfw",
    "image_to_base64",
    "load_config",
    "month_name",
    "normalize_str",
    "pic2b64",
    "randomizer",
    "render_list",
    "save_config",
    "scheduler",
    "silence",
    "time_name",
]

#: 上游是 ``nonebot.scheduler.scheduler``（apscheduler）；shim 只登记不执行，见 Scheduler。
scheduler = Scheduler()

#: 上海时区（上游用 pytz；这里用固定偏移，中国没有夏令时，行为一致）。
TZ_SHANGHAI = timezone(timedelta(hours=8))


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #


def load_config(inbuilt_file_var: Any) -> Any:
    """读配置：``load_config(__file__)`` 或 ``load_config('模块名')``。

    查找顺序：``<data_dir>/config/<模块>.json``（``save_config`` 写这里）→
    ``<inbuilt_file_var 所在目录>/config.json``（上游位置）。都没有时返回 ``{}``。
    """
    data_path = _data_config_path(inbuilt_file_var)
    source_path = _source_config_path(inbuilt_file_var)
    for path in (data_path, source_path):
        if path is not None and path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:  # noqa: BLE001 — 配置坏了要能看见，但不能拦住插件加载
                output.emit_log("warning", f"读取配置 {path} 失败：{type(exc).__name__}: {exc}")
                return {}
    target = data_path or source_path
    if target is not None:
        output.emit_log("debug", f"配置文件不存在（{target}），返回默认 {{}}")
    return {}


def save_config(inbuilt_file_var: Any, config: Any) -> Path:
    """写配置到 ``<data_dir>/config/<模块>.json``，返回写入路径。"""
    path = _data_config_path(inbuilt_file_var)
    if path is None:
        raise UnsupportedCapabilityError(
            "hoshino.util.save_config",
            "没有宿主数据目录（插件不是在 bot_bridge 里加载的？）",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _data_config_path(inbuilt_file_var: Any) -> Path | None:
    host = output.RUNTIME.host_or_none()
    plugin_id = output.current_plugin_id()
    if host is None or not plugin_id:
        return None
    return host.data_dir(plugin_id) / "config" / f"{_config_name(inbuilt_file_var)}.json"


def _source_config_path(inbuilt_file_var: Any) -> Path | None:
    if not isinstance(inbuilt_file_var, (str, os.PathLike)):
        return None
    raw = str(inbuilt_file_var)
    if not raw or ("/" not in raw and "\\" not in raw and not raw.endswith(".py")):
        return None
    return Path(raw).parent / "config.json"


def _config_name(inbuilt_file_var: Any) -> str:
    """配置文件名：路径参数取模块名（``x/__init__.py`` → ``x``），字符串参数取其自身。"""
    if not isinstance(inbuilt_file_var, (str, os.PathLike)):
        return "config"
    raw = str(inbuilt_file_var)
    if "/" in raw or "\\" in raw or raw.endswith(".py"):
        path = Path(raw)
        stem = path.stem
        name = path.parent.name if stem == "__init__" else stem
    else:
        name = raw.rsplit(".", 1)[-1]
    return re.sub(r"[^A-Za-z0-9_\u4e00-\u9fff]+", "_", name).strip("_") or "config"


# --------------------------------------------------------------------------- #
# 图片 → base64
# --------------------------------------------------------------------------- #


def pic2b64(pic: Any) -> str:
    """把图片转成 ``base64://…``（上游同名）。"""
    return image_to_base64(pic)


def image_to_base64(image: Any) -> str:
    """把图片转成 ``base64://…``。

    接受 ``bytes`` / 本地路径 / ``file://`` 路径 / ``MessageSegment``（image）/
    带 ``save(fp, format=…)`` 的对象（PIL.Image 那种鸭子类型）。``http(s)://`` 与
    ``base64://`` 前缀原样返回（前者不联网下载，后者本来就是 base64）。
    """
    if isinstance(image, MessageSegment):
        if image.type != "image":
            raise TypeError(f"image_to_base64 expects an image segment, got {image.type!r}")
        return image_to_base64(image.data.get("file") or "")
    if isinstance(image, (bytes, bytearray, memoryview)):
        return "base64://" + base64.b64encode(bytes(image)).decode("ascii")
    if isinstance(image, (str, os.PathLike)):
        raw = str(image)
        if raw.startswith("base64://"):
            return raw
        if raw.startswith(("http://", "https://")):
            raise UnsupportedCapabilityError(
                "hoshino.util.image_to_base64",
                "shim 不联网：远程图片不会被下载，请先把图片落到本地路径",
            )
        path = Path(raw[len("file:///") :] if raw.startswith("file:///") else raw)
        return "base64://" + base64.b64encode(path.read_bytes()).decode("ascii")
    saver = getattr(image, "save", None)
    if callable(saver):
        buffer = BytesIO()
        saver(buffer, format="PNG")
        return "base64://" + base64.b64encode(buffer.getvalue()).decode("ascii")
    raise TypeError(f"cannot convert {type(image).__name__} to base64")


def fig2b64(plt: Any) -> str:
    """把 matplotlib 的图存成 ``base64://…``（鸭子类型，不强依赖 matplotlib）。"""
    saver = getattr(plt, "savefig", None)
    if not callable(saver):
        raise TypeError(f"fig2b64 expects a matplotlib figure, got {type(plt).__name__}")
    buffer = BytesIO()
    saver(buffer, format="png")
    return "base64://" + base64.b64encode(buffer.getvalue()).decode("ascii")


# --------------------------------------------------------------------------- #
# 文本
# --------------------------------------------------------------------------- #


def normalize_str(string: Any) -> str:
    """规范化文本：NFKC + 小写（上游还会做繁→简，shim 没有 zhconv，见已知缺口）。"""
    return _runtime.normalize(string)


def render_list(lines: Iterable[str], prompt: str = "") -> str:
    """把若干行渲染成树形列表（上游同名）。"""
    items = [str(item) for item in lines]
    if not items:
        return prompt
    if len(items) == 1:
        return prompt + "\n┗" + items[0]
    return prompt + "\n┣" + "\n┣".join(items[:-1]) + "\n┗" + items[-1]


class _SensitiveFilter:
    """DFA 敏感词过滤器（上游 ``util.gfw``；未装载词库时**显式**不支持）。"""

    def __init__(self) -> None:
        self._trie: dict[str, Any] = {}

    @property
    def loaded(self) -> bool:
        return bool(self._trie)

    def parse(self, words: Any) -> int:
        """装载词库：文件路径（每行一个词）或可迭代的词表，返回装载的词数。"""
        if isinstance(words, (str, os.PathLike)):
            raw = Path(words).read_text(encoding="utf-8", errors="replace")
            items = [line.strip() for line in raw.splitlines()]
        else:
            items = [str(item).strip() for item in (words or ())]
        count = 0
        for word in items:
            if word and self.add(word):
                count += 1
        return count

    def add(self, word: str) -> bool:
        if not word:
            return False
        node = self._trie
        for char in str(word):
            node = node.setdefault(char, {})
        node["\x00"] = True
        return True

    def filter(self, text: str) -> str:
        """把命中的敏感词替换成等长的 ``*``。"""
        if not self.loaded:
            raise UnsupportedCapabilityError(
                "hoshino.util.gfw.filter",
                "shim 不随包提供敏感词库；请先用 util.gfw.parse(路径或词表) 装载",
            )
        source = str(text)
        result: list[str] = []
        index = 0
        while index < len(source):
            node = self._trie
            cursor = index
            matched = 0
            while cursor < len(source) and source[cursor] in node:
                node = node[source[cursor]]
                cursor += 1
                if node.get("\x00"):
                    matched = cursor - index
            if matched:
                result.append("*" * matched)
                index += matched
            else:
                result.append(source[index])
                index += 1
        return "".join(result)


#: 上游 ``hoshino.util.gfw``（DFAFilter 实例）。
gfw = _SensitiveFilter()


def filt_message(message: Any) -> Any:
    """过滤字符串或 :class:`Message` 里的敏感词（上游同名）。"""
    if isinstance(message, str):
        return gfw.filter(message)
    if isinstance(message, Message):
        for segment in message:
            if segment.type == "text":
                segment.data["text"] = gfw.filter(str(segment.data.get("text") or ""))
        return message
    raise TypeError(f"filt_message expects str or Message, got {type(message).__name__}")


# --------------------------------------------------------------------------- #
# 频率 / 次数限制（上游同形）
# --------------------------------------------------------------------------- #


class FreqLimiter:
    """CD 限制器（上游同形，秒级）。"""

    def __init__(self, default_cd_seconds: float) -> None:
        self.next_time: dict[Any, float] = defaultdict(float)
        self.default_cd = float(default_cd_seconds)

    def check(self, key: Any) -> bool:
        """是否可以再次触发。"""
        return bool(time.time() >= self.next_time[key])

    def start_cd(self, key: Any, cd_time: float = 0) -> None:
        """开始冷却（``cd_time <= 0`` 时用默认值）。"""
        self.next_time[key] = time.time() + (cd_time if cd_time > 0 else self.default_cd)

    def left_time(self, key: Any) -> float:
        """剩余冷却秒数。"""
        return self.next_time[key] - time.time()


class DailyNumberLimiter:
    """每日次数限制器（上游同形；按 UTC+8 的"日"重置）。"""

    tz = TZ_SHANGHAI

    def __init__(self, max_num: int) -> None:
        self.today = -1
        self.count: dict[Any, int] = defaultdict(int)
        self.max = int(max_num)

    def check(self, key: Any) -> bool:
        """今天还能不能用。"""
        now = datetime.now(self.tz)
        day = (now - timedelta(hours=5)).day
        if day != self.today:
            self.today = day
            self.count.clear()
        return bool(self.count[key] < self.max)

    def get_num(self, key: Any) -> int:
        return int(self.count[key])

    def increase(self, key: Any, num: int = 1) -> None:
        self.count[key] += int(num)

    def reset(self, key: Any) -> None:
        self.count[key] = 0


# --------------------------------------------------------------------------- #
# 机器人 / 用户信息
# --------------------------------------------------------------------------- #


def get_bot() -> Any:
    """当前插件绑定的机器人（等价 ``hoshino.get_bot()``）。"""
    bot = output.RUNTIME.bot_or_none()
    if bot is None:
        bot = output.RUNTIME.any_bot()
    if bot is None:
        raise ValueError("HoshinoBot has not been initialized")
    return bot


def get_bots() -> dict[str, Any]:
    """所有在线账号（shim 里只有一个逻辑账号）。"""
    bot = get_bot()
    return {str(item): bot for item in bot.get_self_ids()}


def get_self_ids() -> list[str]:
    """在线账号 id 列表（上游 ``hoshino.get_self_ids``）。"""
    return list(get_bot().get_self_ids())


async def get_group_member_info(self_id: Any, group_id: Any, user_id: Any, *, cache: bool = True) -> dict[str, Any]:
    """查群成员信息。

    N.E.K.O. 侧没有群成员查询接口，只有"当前这条消息的发送者"是已知的：查询对象与当前
    事件一致时返回这份（部分）信息并标注 ``_source='event'``，否则抛能力缺失 —— 不伪造资料。
    """
    event = output.current_event()
    if event is not None and str(event.group_id) == str(group_id) and str(event.user_id) == str(user_id):
        sender = dict(event.sender or {})
        return {
            "group_id": event.group_id,
            "user_id": event.user_id,
            "nickname": sender.get("nickname", ""),
            "card": sender.get("card", ""),
            "role": sender.get("role", "member"),
            **{key: value for key, value in sender.items() if key not in ("nickname", "card", "role")},
            "_source": "event",
            "_partial": True,
        }
    raise UnsupportedCapabilityError(
        "OneBot.get_group_member_info",
        "N.E.K.O. 没有群成员查询接口；只有当前消息发送者的信息可用（查询别的成员请改用别的方式）",
    )


async def get_user_info(self_id: Any, user_id: Any, *, cache: bool = True) -> dict[str, Any]:
    """查用户资料（同上：只有当前消息发送者）。"""
    event = output.current_event()
    if event is not None and str(event.user_id) == str(user_id):
        sender = dict(event.sender or {})
        return {
            "user_id": event.user_id,
            "nickname": sender.get("nickname", ""),
            **{key: value for key, value in sender.items() if key != "nickname"},
            "_source": "event",
            "_partial": True,
        }
    raise UnsupportedCapabilityError(
        "OneBot.get_stranger_info",
        "N.E.K.O. 没有用户资料查询接口；只有当前消息发送者的信息可用",
    )


# --------------------------------------------------------------------------- #
# 群里动手（撤回 / 禁言）：N.E.K.O. 没有对应接口
# --------------------------------------------------------------------------- #


async def delete_msg(ev: Any) -> None:
    """撤回消息 —— 显式不支持。"""
    raise UnsupportedCapabilityError("hoshino.util.delete_msg", "N.E.K.O. 没有消息撤回接口")


async def silence(ev: Any, ban_time: float, skip_su: bool = True) -> None:
    """禁言 —— 显式不支持。"""
    raise UnsupportedCapabilityError("hoshino.util.silence", "N.E.K.O. 没有禁言接口")


# --------------------------------------------------------------------------- #
# 日期 / 时刻 / 拼图 / 随机器
#
# 上游 ``hoshino/util/__init__.py`` 里的同名实现；数值表逐项照抄（这是给
# priccord / 抽卡类插件做「初音月·初十」这类中文日期用的，不能自己编）。
# --------------------------------------------------------------------------- #

MONTH_NAME = (
    "睦月", "如月", "弥生", "卯月", "皐月", "水無月",
    "文月", "葉月", "長月", "神無月", "霜月", "師走",
)


def month_name(x: int) -> str:
    """1 月 → 「睦月」…（上游同名）。"""
    return MONTH_NAME[x - 1]


DATE_NAME = (
    "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
    "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
    "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
    "卅一",
)


def date_name(x: int) -> str:
    """1 日 → 「初一」…（上游同名）。"""
    return DATE_NAME[x - 1]


NUM_NAME = (
    "〇〇", "〇一", "〇二", "〇三", "〇四", "〇五", "〇六", "〇七", "〇八", "〇九",
    "一〇", "一一", "一二", "一三", "一四", "一五", "一六", "一七", "一八", "一九",
    "二〇", "二一", "二二", "二三", "二四", "二五", "二六", "二七", "二八", "二九",
    "三〇", "三一", "三二", "三三", "三四", "三五", "三六", "三七", "三八", "三九",
    "四〇", "四一", "四二", "四三", "四四", "四五", "四六", "四七", "四八", "四九",
    "五〇", "五一", "五二", "五三", "五四", "五五", "五六", "五七", "五八", "五九",
    "六〇", "六一", "六二", "六三", "六四", "六五", "六六", "六七", "六八", "六九",
    "七〇", "七一", "七二", "七三", "七四", "七五", "七六", "七七", "七八", "七九",
    "八〇", "八一", "八二", "八三", "八四", "八五", "八六", "八七", "八八", "八九",
    "九〇", "九一", "九二", "九三", "九四", "九五", "九六", "九七", "九八", "九九",
)


def time_name(hh: int, mm: int) -> str:
    """(21, 30) → 「二一三〇」（上游同名）。"""
    return NUM_NAME[hh] + NUM_NAME[mm]


def concat_pic(pics: Any, border: int = 5) -> Any:
    """把多张 PIL 图纵向拼接，中间留 ``border`` 像素白边（上游同名）。

    上游直接依赖 PIL；shim 不在 import 期强制要求 PIL（宿主冻结解释器不一定装），
    改成调用时按需导入，缺了就抛 :class:`UnsupportedCapabilityError`。
    """
    try:
        from PIL import Image  # noqa: F401 — 只为确认可用，实际用其鸭子类型
    except ImportError as exc:  # pragma: no cover — 取决于运行环境
        raise UnsupportedCapabilityError(
            "hoshino.util.concat_pic",
            "拼接图片需要 PIL；当前解释器没有安装 Pillow",
        ) from exc

    pic_list = list(pics)
    if not pic_list:
        raise ValueError("concat_pic expects at least one picture")
    num = len(pic_list)
    width, height = pic_list[0].size
    canvas = Image.new("RGBA", (width, num * height + (num - 1) * border), (255, 255, 255, 255))
    for index, pic in enumerate(pic_list):
        canvas.paste(pic, (0, index * (height + border)), pic)
    return canvas


#: 上游那串「电探/水侦」梗名单（随机播报用），逐项照抄。
DEVICES = [
    "22号对水上电探改四(后期调整型)",
    "42号对空电探改二",
    "15m二重测距仪改+21号电探改二+熟练射击指挥所",
    "FuMO25 雷达",
    "SK+SG 雷达",
    "SG 雷达(后期型)",
    "GFCS Mk.37",
    "潜水舰搭载电探&逆探(E27)",
    "HF/DF+Type144/147 ASDIC",
    "三式指挥联络机(对潜)",
    "O号观测机改二",
    "S-51J改",
    "二式陆上侦察机(熟练)",
    "东海(九〇一空)",
    "二式大艇",
    "PBY-5A Catalina",
    "零式水上侦察机11型乙(熟练)",
    "零式水上侦察机11型乙改(夜侦)",
    "紫云",
    "Ar196改",
    "Ro.43水侦",
    "OS2U",
    "S9 Osprey",
    "彩云(东加罗林空)",
    "彩云(侦四)",
    "试制景云(舰侦型)",
]


def randomizer(target: Any) -> Any:
    """返回一个「XX监测到 <target>」的格式化函数（上游同名，纯玩梗）。"""
    import random

    return lambda m: f'{random.choice(DEVICES)}监测到{target}!{"!" * random.randint(0, 4)}\n{m}'
