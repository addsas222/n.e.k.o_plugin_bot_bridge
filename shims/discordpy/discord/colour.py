"""``discord.Colour`` / ``discord.Color``（同名别名，与上游一致）。

纯值对象：调色板常量、``from_rgb`` / ``from_hsv`` / ``from_str`` / ``to_rgb``。
不做任何渲染（N.E.K.O. 的消息是纯文本）。
"""

from __future__ import annotations

import colorsys
import random
from typing import Any

__all__ = ["Colour", "Color"]


class Colour:
    """RGB 颜色（上游 ``discord.Colour``）。"""

    def __init__(self, value: int = 0) -> None:
        if not isinstance(value, int):
            raise TypeError(f"Expected int parameter, received {type(value).__name__} instead.")
        self.value = int(value) & 0xFFFFFF

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Colour):
            return self.value == other.value
        if isinstance(other, int):
            return self.value == other
        return NotImplemented

    def __ne__(self, other: object) -> bool:
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self) -> int:
        return self.value

    def __int__(self) -> int:
        return self.value

    def __repr__(self) -> str:
        return f"<Colour value={self.value}>"

    def __str__(self) -> str:
        return f"#{self.value:06x}"

    # ---- 工厂 ------------------------------------------------------------ #

    @classmethod
    def from_rgb(cls, r: int, g: int, b: int) -> "Colour":
        return cls((int(r) << 16) + (int(g) << 8) + int(b))

    @classmethod
    def from_hsv(cls, h: float, s: float, v: float) -> "Colour":
        red, green, blue = colorsys.hsv_to_rgb(h, s, v)
        return cls.from_rgb(round(red * 255), round(green * 255), round(blue * 255))

    @classmethod
    def from_str(cls, value: str) -> "Colour":
        text = str(value)
        if text.startswith("#"):
            text = text[1:]
        if text.startswith("0x"):
            text = text[2:]
        if not text or len(text) > 6:
            raise ValueError(f"Invalid colour string: {value!r}")
        return cls(int(text, 16))

    @classmethod
    def random(cls, *, seed: Any = None) -> "Colour":
        generator = random.Random(seed)
        return cls(generator.randint(0, 0xFFFFFF))

    @classmethod
    def default(cls) -> "Colour":
        return cls(0)

    def to_rgb(self) -> tuple[int, int, int]:
        return ((self.value >> 16) & 0xFF, (self.value >> 8) & 0xFF, self.value & 0xFF)


#: 上游调色板（名字与数值都取自 discord.py）。
Colour.teal = Colour(0x1ABC9C)
Colour.dark_teal = Colour(0x11806A)
Colour.brand_green = Colour(0x3BA55D)
Colour.green = Colour(0x2ECC71)
Colour.dark_green = Colour(0x1F8B4C)
Colour.blue = Colour(0x3498DB)
Colour.dark_blue = Colour(0x206694)
Colour.purple = Colour(0x9B59B6)
Colour.dark_purple = Colour(0x71368A)
Colour.magenta = Colour(0xE91E63)
Colour.dark_magenta = Colour(0xAD1457)
Colour.gold = Colour(0xF1C40F)
Colour.dark_gold = Colour(0xC27C0E)
Colour.orange = Colour(0xE67E22)
Colour.dark_orange = Colour(0xA84300)
Colour.red = Colour(0xE74C3C)
Colour.dark_red = Colour(0x992D22)
Colour.lighter_grey = Colour(0x95A5A6)
Colour.dark_grey = Colour(0x607D8B)
Colour.light_grey = Colour(0x979C9F)
Colour.darker_grey = Colour(0x546E7A)
Colour.blurple = Colour(0x5865F2)
Colour.greyple = Colour(0x99AAB5)
Colour.dark_theme = Colour(0x292B2F)
Colour.fuchsia = Colour(0xEB459E)
Colour.yellow = Colour(0xFEE75C)
Colour.dark_yellow = Colour(0xC9A227)

#: 上游别名。
Colour.gray = Colour.light_grey
Colour.dark_gray = Colour.dark_grey
Colour.darker_gray = Colour.darker_grey
Colour.lighter_gray = Colour.lighter_grey

Color = Colour
