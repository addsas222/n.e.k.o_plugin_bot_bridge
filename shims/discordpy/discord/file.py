"""``discord.File``：待上传的文件（N.E.K.O. 只做结构化记录）。

上游的 ``File`` 是「准备上传的内容」；N.E.K.O. 的 ``send_chat`` 只承载文本，因此这里
只保留文件名 / 大小 / 原始路径这些元数据，发送时由兼容层登记成「附件无法投递」的能力缺失
（见 ``discord._runtime.note_attachment``）。绝不假装上传成功。
"""

from __future__ import annotations

import io
import os
from typing import Any

__all__ = ["File"]


class File:
    """一个待发送的文件（数据载体）。"""

    def __init__(self, fp: Any, filename: str | None = None, *, spoiler: bool = False, description: str | None = None) -> None:
        self.fp = fp
        self.spoiler = bool(spoiler)
        self.description = description
        raw_name = filename or getattr(fp, "name", None)
        if not raw_name and isinstance(fp, (str, os.PathLike)):
            raw_name = os.path.basename(os.fspath(fp))
        name = str(raw_name or "file")
        if spoiler and not name.startswith("SPOILER_"):
            name = f"SPOILER_{name}"
        self.filename = name
        self._owner = False
        self.url = ""
        self.size = _size_of(fp)
        self.content_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """导出元数据（用户可见的部分）。"""
        return {
            "filename": self.filename,
            "size": self.size,
            "spoiler": self.spoiler,
            "description": self.description,
            "url": self.url,
        }

    def close(self) -> None:
        closer = getattr(self.fp, "close", None)
        if callable(closer):
            closer()

    def __enter__(self) -> "File":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def __repr__(self) -> str:
        return f"<File filename={self.filename!r} size={self.size}>"


def _size_of(fp: Any) -> int:
    if isinstance(fp, (bytes, bytearray)):
        return len(fp)
    if isinstance(fp, str):
        try:
            return os.path.getsize(fp)
        except OSError:
            return len(fp.encode("utf-8"))
    if isinstance(fp, io.IOBase):
        try:
            position = fp.tell()
            fp.seek(0, os.SEEK_END)
            size = fp.tell()
            fp.seek(position)
            return int(size)
        except (OSError, ValueError):
            return 0
    return 0
