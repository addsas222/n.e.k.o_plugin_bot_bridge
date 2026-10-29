"""pydantic 的轻量替身（上游 ``nonebot.compat`` 的子集）。

上游 ``nonebot.compat`` 存在的唯一目的是抹平 pydantic v1/v2 差异。N.E.K.O. 的
frozen Python 3.11 里第三方依赖是白名单制（pydantic 虽在，但 shim 要求 **零第三方依赖**），
因此这里用纯标准库实现同样的最小语义：

* :data:`PydanticUndefined` —— ``DefaultParam`` 用来表示“该用参数默认值”的哨兵；
* :class:`FieldInfo` / :class:`ModelField` —— 仅承载「参数名 + 注解 + 默认值」；
* :func:`model_dump` / :func:`model_validate` / :func:`type_validate_python` —— 结构性转换。

偏差：不做任何类型校验/强制转换（上游会）。凡依赖 pydantic 校验行为的插件代码在
shim 下不会报校验错，只会拿到原值。
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

PYDANTIC_V2 = False
PYDANTIC_V1 = False


class _PydanticUndefinedType:
    """``PydanticUndefined`` 的单例类型。"""

    _instance: "_PydanticUndefinedType | None" = None

    def __new__(cls) -> "_PydanticUndefinedType":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "PydanticUndefined"

    def __bool__(self) -> bool:
        return False


PydanticUndefined = _PydanticUndefinedType()
"""表示“字段没有值”的哨兵（上游来自 ``pydantic_core``）。"""


class FieldInfo:
    """参数信息（上游是 ``pydantic.fields.FieldInfo``）。"""

    def __init__(self, default: Any = PydanticUndefined, **kwargs: Any) -> None:
        self.default = default
        self.extra = dict(kwargs)

    def __repr__(self) -> str:
        return f"FieldInfo(default={self.default!r})"


@dataclass
class ModelField:
    """字段描述（上游是 ``pydantic.fields.ModelField``）。"""

    name: str = ""
    annotation: Any = Any
    field_info: FieldInfo = field(default_factory=FieldInfo)

    @classmethod
    def construct(cls, *, name: str, annotation: Any, field_info: FieldInfo) -> "ModelField":
        return cls(name=name, annotation=annotation, field_info=field_info)

    def get_default(self) -> Any:
        default = getattr(self.field_info, "default", PydanticUndefined)
        if default is PydanticUndefined:
            return None
        return default


def model_dump(model: Any, **kwargs: Any) -> Any:
    """尽力把一个对象转成普通 dict（上游调用 pydantic 的 ``model_dump``）。"""
    if isinstance(model, Mapping):
        return {str(key): value for key, value in model.items()}
    dumper = getattr(model, "model_dump", None)
    if callable(dumper):
        return dumper(**kwargs)
    dumper = getattr(model, "dict", None)
    if callable(dumper) and not isinstance(model, dict):
        try:
            return dumper(**kwargs)
        except TypeError:
            return dumper()
    if dataclasses.is_dataclass(model) and not isinstance(model, type):
        return dataclasses.asdict(model)
    if hasattr(model, "__dict__"):
        return {key: value for key, value in vars(model).items() if not key.startswith("__")}
    return model


def model_validate(model: Any, value: Any, **kwargs: Any) -> Any:
    """不做校验的 ``model_validate``：结构相符就转换，否则原样返回。"""
    return type_validate_python(model, value)


def type_validate_python(type_: Any, value: Any) -> Any:
    """把 dict 转成 ``type_`` 的实例；已经是目标类型则原样返回。"""
    if type_ is Any or not isinstance(type_, type):
        return value
    if isinstance(value, type_):
        return value
    if isinstance(value, Mapping) and (dataclasses.is_dataclass(type_) or getattr(type_, "__init__", None)):
        try:
            return type_(**{str(key): item for key, item in value.items()})
        except TypeError:
            return value
    if isinstance(value, Iterable) and not isinstance(value, (str, bytes, Mapping)):
        return type_(value)
    return value


__all__ = [
    "FieldInfo",
    "ModelField",
    "PYDANTIC_V1",
    "PYDANTIC_V2",
    "PydanticUndefined",
    "model_dump",
    "model_validate",
    "type_validate_python",
]
