"""AstrBot 消息组件（N.E.K.O. shim 子集）。

上游实现基于 pydantic；本 shim 用普通类复刻同名字段与工厂方法，字段名、工厂方法名
与 ``toDict()`` 输出保持与上游一致，便于插件无感迁移。

差异（已记录）：不做 pydantic 校验；``Image.convert_to_file_path`` /
``convert_to_base64`` / ``register_to_file_service`` 需要下游平台的文件服务，
N.E.K.O. 侧没有等价实现，调用会抛 :class:`astrbot.UnsupportedCapabilityError`。
"""

from __future__ import annotations

import base64
from enum import Enum
from pathlib import Path
from typing import Any

from ..._runtime import UnsupportedCapabilityError


class ComponentType(str, Enum):
    """消息段类型（与上游 ``astrbot.core.message.components.ComponentType`` 同名同值）。"""

    Plain = "Plain"
    Image = "Image"
    Record = "Record"
    Video = "Video"
    File = "File"
    Face = "Face"
    At = "At"
    AtAll = "AtAll"
    Reply = "Reply"
    Node = "Node"
    Nodes = "Nodes"
    Poke = "Poke"
    Forward = "Forward"
    RPS = "RPS"
    Dice = "Dice"
    Shake = "Shake"
    Share = "Share"
    Contact = "Contact"
    Location = "Location"
    Music = "Music"
    Json = "Json"
    Unknown = "Unknown"


class BaseMessageComponent:
    """所有消息段的父类。"""

    type: ComponentType = ComponentType.Unknown

    def __init__(self, **_data: object) -> None:
        """上游由 pydantic 填充字段；shim 的子类各自显式接收参数，多余关键字被忽略。"""

    def toDict(self) -> dict[str, Any]:
        """同步导出为 AstrBot 的消息段字典。"""
        data = {
            key: value
            for key, value in vars(self).items()
            if key != "type" and value is not None
        }
        return {"type": self.type.value.lower(), "data": data}

    async def to_dict(self) -> dict[str, Any]:
        """异步导出（上游同名方法的兼容包装）。"""
        return self.toDict()

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({vars(self)!r})"


class Plain(BaseMessageComponent):
    """纯文本消息段。"""

    type: ComponentType = ComponentType.Plain

    def __init__(self, text: str, convert: bool = True, **_: object) -> None:
        del convert  # 上游这个参数只对部分适配器有意义，shim 不保留
        self.text = text

    def toDict(self) -> dict[str, Any]:
        return {"type": "text", "data": {"text": self.text}}

    async def to_dict(self) -> dict[str, Any]:
        return self.toDict()


class Image(BaseMessageComponent):
    """图片消息段。"""

    type: ComponentType = ComponentType.Image

    def __init__(
        self,
        file: str | None = "",
        url: str | None = "",
        path: str | None = "",
        **_: object,
    ) -> None:
        self.file = file
        self.url = url
        self.path = path

    @staticmethod
    def fromURL(url: str, **_: object) -> Image:
        """从网络链接构造图片消息段。"""
        if not (url.startswith("http://") or url.startswith("https://")):
            raise ValueError("not a valid url")
        return Image(file=url)

    @staticmethod
    def fromFileSystem(path: str, **_: object) -> Image:
        """从本地文件构造图片消息段（``file`` 为 file:// URI，``path`` 为绝对路径）。"""
        file_path = Path(path).resolve(strict=False)
        return Image(file=file_path.as_uri(), path=str(file_path))

    @staticmethod
    def fromBase64(base64_str: str, **_: object) -> Image:
        """从 base64 正文构造图片消息段。"""
        return Image(file=f"base64://{base64_str}")

    @staticmethod
    def fromBytes(data: bytes) -> Image:
        """从字节构造图片消息段。"""
        return Image.fromBase64(base64.b64encode(data).decode())

    @staticmethod
    def fromIO(io: Any) -> Image:
        """从文件对象构造图片消息段。"""
        return Image.fromBytes(io.read())

    async def convert_to_file_path(self) -> str:
        raise UnsupportedCapabilityError(
            "image.convert_to_file_path",
            "N.E.K.O. 不提供图片下载/落盘服务",
        )

    async def convert_to_base64(self) -> str:
        raise UnsupportedCapabilityError(
            "image.convert_to_base64",
            "N.E.K.O. 不提供图片下载/编码服务",
        )

    async def register_to_file_service(self) -> str:
        raise UnsupportedCapabilityError(
            "image.register_to_file_service",
            "N.E.K.O. 没有 callback_api_base 文件服务",
        )


class Face(BaseMessageComponent):
    """QQ 表情消息段。"""

    type: ComponentType = ComponentType.Face

    def __init__(self, id: int, **_: object) -> None:
        self.id = id


class At(BaseMessageComponent):
    """@ 某人消息段（``qq='all'`` 表示 @全体成员）。"""

    type: ComponentType = ComponentType.At

    def __init__(self, qq: int | str, name: str | None = "", **_: object) -> None:
        self.qq = qq
        self.name = name

    def toDict(self) -> dict[str, Any]:
        return {"type": "at", "data": {"qq": str(self.qq)}}


class AtAll(At):
    """@全体成员消息段。"""

    type: ComponentType = ComponentType.AtAll

    def __init__(self, **_: object) -> None:
        super().__init__(qq="all")


class Record(BaseMessageComponent):
    """语音消息段。"""

    type: ComponentType = ComponentType.Record

    def __init__(
        self,
        file: str | None = "",
        url: str | None = "",
        text: str | None = None,
        **_: object,
    ) -> None:
        self.file = file
        self.url = url
        self.text = text


class Video(BaseMessageComponent):
    """视频消息段。"""

    type: ComponentType = ComponentType.Video

    def __init__(self, file: str | None = "", url: str | None = "", **_: object) -> None:
        self.file = file
        self.url = url

    @staticmethod
    def fromURL(url: str, **_: object) -> Video:
        """从网络链接构造视频消息段。"""
        return Video(url=url)

    @staticmethod
    def fromFileSystem(path: str, **_: object) -> Video:
        """从本地文件构造视频消息段。"""
        return Video(file=str(Path(path).resolve(strict=False)))


class File(BaseMessageComponent):
    """文件消息段。"""

    type: ComponentType = ComponentType.File

    def __init__(self, file: str = "", name: str = "", **_: object) -> None:
        self.file = file
        self.name = name


class Reply(BaseMessageComponent):
    """引用回复消息段。"""

    type: ComponentType = ComponentType.Reply

    def __init__(
        self,
        id: str | int,
        chain: list[BaseMessageComponent] | None = None,
        sender_id: int | str = 0,
        **_: object,
    ) -> None:
        self.id = id
        self.chain = chain if chain is not None else []
        self.sender_id = sender_id


class Poke(BaseMessageComponent):
    """戳一戳消息段。"""

    type: ComponentType = ComponentType.Poke

    def __init__(self, id: int | str = 0, **_: object) -> None:
        self.id = id


class Json(BaseMessageComponent):
    """JSON 消息段。"""

    type: ComponentType = ComponentType.Json

    def __init__(self, data: Any = None, **_: object) -> None:
        self.data = data


class Node(BaseMessageComponent):
    """合并转发中的单个节点。"""

    type: ComponentType = ComponentType.Node

    def __init__(
        self,
        uin: int | str = 0,
        name: str = "",
        content: list[BaseMessageComponent] | None = None,
        **_: object,
    ) -> None:
        self.uin = uin
        self.name = name
        self.content = content if content is not None else []


class Nodes(BaseMessageComponent):
    """合并转发消息（多个节点）。"""

    type: ComponentType = ComponentType.Nodes

    def __init__(self, nodes: list[Node] | None = None, **_: object) -> None:
        self.nodes = nodes if nodes is not None else []


class Unknown(BaseMessageComponent):
    """未知消息段。"""

    type: ComponentType = ComponentType.Unknown

    def __init__(self, text: str = "", **_: object) -> None:
        self.text = text


__all__ = [
    "At",
    "AtAll",
    "BaseMessageComponent",
    "ComponentType",
    "Face",
    "File",
    "Image",
    "Json",
    "Node",
    "Nodes",
    "Poke",
    "Plain",
    "Record",
    "Reply",
    "Unknown",
    "Video",
]
