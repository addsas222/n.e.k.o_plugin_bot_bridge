"""pytest 引导层。

把两件跟运行环境相关的事集中做一次，这样**任何一个测试文件单独跑**都能工作：

* 让 ``import plugin.sdk.*`` 解析到真实的 N.E.K.O. SDK（``NEKO_SOURCE_ROOT``）；
* 让 ``import plugin.plugins.bot_bridge`` 解析到本插件目录。

实现见 ``support.install_import_shims``（临时 shim 包 + ``__path__`` 扩展）。
"""

from __future__ import annotations

import os
import pytest
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import support  # noqa: E402 — 需要 sys.path 就位后再导入

support.install_import_shims()

# 整个测试会话的宿主存储根指向临时目录：任何走宿主存储的代码都不会碰真机目录。
if not os.environ.get("NEKO_STORAGE_SELECTED_ROOT"):
    os.environ["NEKO_STORAGE_SELECTED_ROOT"] = tempfile.mkdtemp(prefix="bot_bridge_test_storage_")


@pytest.fixture(autouse=True)
def _isolated_storage_root(tmp_path_factory, monkeypatch):
    """每个测试一个独立存储根。

    启停状态是**跨实例持久化**的（by design），共用会话级目录会让「前一个测试停掉的
    插件」在下一个测试里仍然是停用态（踩过：hello_nonebot 被串味成 disabled）。
    """
    root = tmp_path_factory.mktemp("storage")
    monkeypatch.setenv("NEKO_STORAGE_SELECTED_ROOT", str(root))
    yield root
