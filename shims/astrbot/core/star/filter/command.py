"""指令过滤器（N.E.K.O. shim 子集，含参数解析，与上游同语义）。

上游实现见 AstrBot ``astrbot/core/star/filter/command.py``；这里逐段复刻，仅把
``typing_extensions`` 之类的外部依赖去掉。
"""

from __future__ import annotations

import inspect
import re
import types
import typing
from typing import Any

from ...._runtime import RUNTIME
from ...config import AstrBotConfig
from ...platform.astr_message_event import AstrMessageEvent
from ..star_handler import StarHandlerMetadata
from . import HandlerFilter
from .custom_filter import CustomFilter


class GreedyStr(str):
    """标记指令完成其他参数接收后的所有剩余文本。"""


def unwrap_optional(annotation: Any) -> tuple[Any, ...]:
    """去掉 Optional[T] / Union[T, None] / T | None，返回非 None 的成员。"""
    args = typing.get_args(annotation)
    non_none_args = [arg for arg in args if arg is not type(None)]
    if len(non_none_args) == 1:
        return (non_none_args[0],)
    if len(non_none_args) > 1:
        return tuple(non_none_args)
    return ()


class CommandFilter(HandlerFilter):
    """标准指令过滤器（受唤醒前缀约束）。"""

    def __init__(
        self,
        command_name: str,
        alias: set[str] | None = None,
        handler_md: StarHandlerMetadata | None = None,
        parent_command_names: list[str] | None = None,
    ) -> None:
        self.command_name = command_name
        self.alias = alias if alias else set()
        self._original_command_name = command_name
        self.parent_command_names = (
            parent_command_names if parent_command_names is not None else [""]
        )
        self.handler_md: StarHandlerMetadata | None = None
        self.handler_params: dict[str, Any] = {}
        if handler_md:
            self.init_handler_md(handler_md)
        self.custom_filter_list: list[CustomFilter] = []
        self._cmpl_cmd_names: list[str] | None = None

    def print_types(self) -> str:
        """把参数列表打印成 ``name(type),`` 形式（用于错误提示）。"""
        parts: list[str] = []
        for key, value in self.handler_params.items():
            if isinstance(value, type):
                parts.append(f"{key}({value.__name__}),")
            elif isinstance(value, types.UnionType) or typing.get_origin(value) is typing.Union:
                parts.append(f"{key}({value}),")
            else:
                parts.append(f"{key}({type(value).__name__})={value},")
        return "".join(parts).rstrip(",")

    def init_handler_md(self, handle_md: StarHandlerMetadata) -> None:
        """绑定 Handler，并解析它声明的参数（跳过 self / event）。"""
        self.handler_md = handle_md
        try:
            signature = inspect.signature(handle_md.handler, eval_str=True)
        except (NameError, TypeError) as exc:
            # 上游在这一步会直接抛错；插件模块里的注解有时无法求值，这里降级为
            # 不求值签名，并把原因写进宿主日志（差异已记录）。
            RUNTIME.log(
                "warning",
                f"failed to evaluate annotations of {handle_md.handler_full_name}: {exc!r}; "
                "falling back to raw annotations",
                source="astrbot.filter.command",
            )
            signature = inspect.signature(handle_md.handler)
        self.handler_params = {}
        index = 0
        for key, value in signature.parameters.items():
            if index < 2:
                # 忽略前两个参数：self 和 event
                index += 1
                continue
            if value.default == inspect.Parameter.empty:
                self.handler_params[key] = value.annotation
            else:
                self.handler_params[key] = value.default

    def get_handler_md(self) -> StarHandlerMetadata | None:
        """绑定到此过滤器的 Handler 元数据。"""
        return self.handler_md

    def add_custom_filter(self, custom_filter: CustomFilter) -> None:
        """追加一个自定义过滤器。"""
        self.custom_filter_list.append(custom_filter)

    def custom_filter_ok(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """自定义过滤器是否全部通过。"""
        for custom_filter in self.custom_filter_list:
            if not custom_filter.filter(event, cfg):
                return False
        return True

    def validate_and_convert_params(
        self,
        params: list[Any],
        param_type: dict[str, Any],
    ) -> dict[str, Any]:
        """把位置参数按声明类型转换（与上游一致，含 GreedyStr 与联合类型处理）。"""
        result: dict[str, Any] = {}
        param_items = list(param_type.items())
        for index, (param_name, param_type_or_default_val) in enumerate(param_items):
            is_greedy = param_type_or_default_val is GreedyStr
            if is_greedy:
                if index != len(param_items) - 1:
                    raise ValueError(f"参数 '{param_name}' (GreedyStr) 必须是最后一个参数。")
                result[param_name] = " ".join(params[index:])
                break
            if index >= len(params):
                if (
                    isinstance(param_type_or_default_val, type | types.UnionType)
                    or typing.get_origin(param_type_or_default_val) is typing.Union
                    or param_type_or_default_val is inspect.Parameter.empty
                ):
                    raise ValueError(f"必要参数缺失。该指令完整参数: {self.print_types()}")
                result[param_name] = param_type_or_default_val
                continue
            try:
                if param_type_or_default_val is None:
                    if params[index].isdigit():
                        result[param_name] = int(params[index])
                    else:
                        result[param_name] = params[index]
                elif isinstance(param_type_or_default_val, str):
                    result[param_name] = params[index]
                elif isinstance(param_type_or_default_val, bool):
                    lower_param = str(params[index]).lower()
                    if lower_param in ("true", "yes", "1"):
                        result[param_name] = True
                    elif lower_param in ("false", "no", "0"):
                        result[param_name] = False
                    else:
                        raise ValueError(
                            f"参数 {param_name} 必须是布尔值（true/false, yes/no, 1/0）。",
                        )
                elif isinstance(param_type_or_default_val, int):
                    result[param_name] = int(params[index])
                elif isinstance(param_type_or_default_val, float):
                    result[param_name] = float(params[index])
                else:
                    origin = typing.get_origin(param_type_or_default_val)
                    if origin in (typing.Union, types.UnionType):
                        non_none = unwrap_optional(param_type_or_default_val)
                        if len(non_none) == 1:
                            result[param_name] = non_none[0](params[index])
                        else:
                            result[param_name] = params[index]
                    else:
                        result[param_name] = param_type_or_default_val(params[index])
            except ValueError as exc:
                raise ValueError(
                    f"参数 {param_name} 类型错误。完整参数: {self.print_types()}",
                ) from exc
        return result

    def get_complete_command_names(self) -> list[str]:
        """返回包含别名在内的完整指令名列表（含父指令名）。"""
        if self._cmpl_cmd_names is not None:
            return self._cmpl_cmd_names
        # 差异：上游用 `list(self.alias)`（set 顺序随进程哈希种子变化），这里排序以
        # 保证同一插件每次加载得到的指令名列表顺序稳定（便于桥接层展示与测试）。
        self._cmpl_cmd_names = [
            f"{parent} {cmd}" if parent else cmd
            for cmd in [self.command_name, *sorted(self.alias)]
            for parent in self.parent_command_names or [""]
        ]
        return self._cmpl_cmd_names

    def equals(self, message_str: str) -> bool:
        """消息是否恰好等于某个完整指令名。"""
        return message_str in self.get_complete_command_names()

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """消息是否触发本指令；命中时把解析出的参数写进 ``parsed_params``。"""
        if not event.is_at_or_wake_command:
            return False
        if not self.custom_filter_ok(event, cfg):
            return False
        message_str = re.sub(r"\s+", " ", event.get_message_str().strip())
        matched = False
        for full_cmd in self.get_complete_command_names():
            if message_str.startswith(f"{full_cmd} ") or message_str == full_cmd:
                matched = True
                message_str = message_str[len(full_cmd):].strip()
        if not matched:
            return False
        tokens = [token for token in message_str.split(" ") if token]
        params = self.validate_and_convert_params(tokens, self.handler_params)
        event.set_extra("parsed_params", params)
        return True


__all__ = ["CommandFilter", "GreedyStr", "unwrap_optional"]
