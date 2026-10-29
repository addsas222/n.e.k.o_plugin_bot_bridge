"""指令组过滤器（N.E.K.O. shim 子集，与上游同语义）。"""

from __future__ import annotations

from ...config import AstrBotConfig
from ...platform.astr_message_event import AstrMessageEvent
from . import HandlerFilter
from .command import CommandFilter
from .custom_filter import CustomFilter


class CommandGroupFilter(HandlerFilter):
    """指令组过滤器（受唤醒前缀约束）。"""

    def __init__(
        self,
        group_name: str,
        alias: set[str] | None = None,
        parent_group: CommandGroupFilter | None = None,
    ) -> None:
        self.group_name = group_name
        self.alias = alias if alias else set()
        self._original_group_name = group_name
        self.sub_command_filters: list[CommandFilter | CommandGroupFilter] = []
        self.custom_filter_list: list[CustomFilter] = []
        self.parent_group = parent_group
        self._cmpl_cmd_names: list[str] | None = None

    def add_sub_command_filter(
        self,
        sub_command_filter: CommandFilter | CommandGroupFilter,
    ) -> None:
        """注册一个子指令 / 子指令组。"""
        self.sub_command_filters.append(sub_command_filter)

    def add_custom_filter(self, custom_filter: CustomFilter) -> None:
        """追加一个自定义过滤器。"""
        self.custom_filter_list.append(custom_filter)

    def get_complete_command_names(self) -> list[str]:
        """返回包含父级与别名在内的完整指令组名列表。"""
        if self._cmpl_cmd_names is not None:
            return self._cmpl_cmd_names
        parent_cmd_names = (
            self.parent_group.get_complete_command_names() if self.parent_group else []
        )
        if not parent_cmd_names:
            # 差异：上游用 `list(self.alias)`，这里排序以保证每次加载顺序一致。
            return [self.group_name, *sorted(self.alias)]
        result: list[str] = []
        candidates = [self.group_name, *sorted(self.alias)]
        for parent_cmd_name in parent_cmd_names:
            for candidate in candidates:
                result.append(f"{parent_cmd_name} {candidate}")
        self._cmpl_cmd_names = result
        return result

    def print_cmd_tree(
        self,
        sub_command_filters: list[CommandFilter | CommandGroupFilter],
        prefix: str = "",
        event: AstrMessageEvent | None = None,
        cfg: AstrBotConfig | None = None,
    ) -> str:
        """把指令组渲染成树形文本（用户没给子指令时的提示）。"""
        parts: list[str] = []
        for sub_filter in sub_command_filters:
            if isinstance(sub_filter, CommandFilter):
                custom_filter_pass = True
                if event and cfg:
                    custom_filter_pass = sub_filter.custom_filter_ok(event, cfg)
                if custom_filter_pass:
                    cmd_types = sub_filter.print_types()
                    line = f"{prefix}├── {sub_filter.command_name}"
                    line += f" ({cmd_types})" if cmd_types else " (无参数指令)"
                    handler_md = sub_filter.get_handler_md()
                    if handler_md and handler_md.desc:
                        line += f": {handler_md.desc}"
                    parts.append(line + "\n")
            elif isinstance(sub_filter, CommandGroupFilter):
                custom_filter_pass = True
                if event and cfg:
                    custom_filter_pass = sub_filter.custom_filter_ok(event, cfg)
                if custom_filter_pass:
                    parts.append(f"{prefix}├── {sub_filter.group_name}\n")
                    parts.append(
                        sub_filter.print_cmd_tree(
                            sub_filter.sub_command_filters,
                            prefix + "│   ",
                            event=event,
                            cfg=cfg,
                        ),
                    )
        return "".join(parts)

    def custom_filter_ok(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """自定义过滤器是否全部通过。"""
        for custom_filter in self.custom_filter_list:
            if not custom_filter.filter(event, cfg):
                return False
        return True

    def startswith(self, message_str: str) -> bool:
        """消息是否以组名开头。"""
        return message_str.startswith(tuple(self.get_complete_command_names()))

    def equals(self, message_str: str) -> bool:
        """消息是否恰好是组名。"""
        return message_str in self.get_complete_command_names()

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        """消息是否属于本指令组。

        Raises:
            ValueError: 只写了组名时抛出，异常文本就是该组的指令树（与上游一致）。
        """
        if not event.is_at_or_wake_command:
            return False
        if not self.custom_filter_ok(event, cfg):
            return False
        if self.equals(event.message_str.strip()):
            tree = (
                self.group_name
                + "\n"
                + self.print_cmd_tree(self.sub_command_filters, event=event, cfg=cfg)
            )
            raise ValueError(
                f"参数不足。{self.group_name} 指令组下有如下指令，请参考：\n{tree}",
            )
        return self.startswith(event.message_str)


__all__ = ["CommandGroupFilter"]
