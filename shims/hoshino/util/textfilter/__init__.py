"""``hoshino.util.textfilter`` —— 上游这个子目录没有 ``__init__.py``（是命名空间包），
所以 ``from hoshino.util.textfilter.filter import DFAFilter`` 才是插件的真实写法。

这里补一层 ``__init__.py`` 方便直接 ``from hoshino.util.textfilter import DFAFilter``，
两种写法都能用。实现见 :mod:`.filter`。
"""

from __future__ import annotations

from .filter import BSFilter, DFAFilter, NaiveFilter, test_first_character

#: 便利重导出：``hoshino.util.filt_message`` 的实现就在父包里，这里也放一份，
#: 免得 ``from hoshino.util.textfilter import filt_message`` 这种写法落空。
from .. import filt_message as filt_message

__all__ = ["BSFilter", "DFAFilter", "NaiveFilter", "filt_message", "test_first_character"]
