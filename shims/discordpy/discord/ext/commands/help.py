"""``discord.ext.commands.HelpCommand`` —— 帮助命令基类。

上游的 ``HelpCommand`` 是「可替换的帮助系统」抽象：cog 里写
``bot.help_command = MyHelp()``，再由 ``ctx.send_help()`` 触发。

桥接层的 ``Context.send_help`` 已经能直接吐出一段文字帮助，所以这里只需要保证：

* ``HelpCommand`` **可继承、可实例化**（cog 在 import 阶段写子类不会炸）；
* 子类常用的 ``send_bot_help`` / ``send_cog_help`` / ``send_group_help`` /
  ``send_command_help`` 四个钩子存在，默认实现把内容发到当前会话；
* ``filter_commands`` / ``get_bot_mapping`` / ``clean_prefix`` 这些会被子类调用的
  辅助方法也在。
"""

from __future__ import annotations

from typing import Any, Iterable

__all__ = ["DefaultHelpCommand", "HelpCommand"]


class HelpCommand:
    """帮助命令基类（对应上游 ``discord.ext.commands.HelpCommand``）。"""

    def __init__(self, **options: Any) -> None:
        self.verify_checks: bool = bool(options.pop("verify_checks", True))
        self.show_hidden: bool = bool(options.pop("show_hidden", False))
        self.no_category: str = str(options.pop("no_category", "No Category"))
        self.command_attrs: dict[str, Any] = dict(options.pop("command_attrs", {}) or {})
        #: 上游由 ``Bot`` 在设置 ``help_command`` 时注入
        self.context: Any = None
        self._original_kwargs: dict[str, Any] = options

    # -- 会被子类调用的辅助方法 -------------------------------------------- #
    @property
    def clean_prefix(self) -> str:
        ctx = self.context
        prefix = getattr(ctx, "clean_prefix", None) or getattr(ctx, "prefix", None) or "/"
        return str(prefix)

    def get_command_signature(self, command: Any) -> str:
        signature = getattr(command, "signature", "") or ""
        qualified = getattr(command, "qualified_name", None) or getattr(command, "name", "command")
        return f"{self.clean_prefix}{qualified} {signature}".strip()

    async def filter_commands(
        self,
        commands: Iterable[Any],
        /,
        *,
        sort: bool = False,
        key: Any = None,
    ) -> list[Any]:
        """过滤命令（桥接层不做 checks/hidden 过滤，全部保留）。"""
        items = [c for c in commands if self.show_hidden or not getattr(c, "hidden", False)]
        if sort:
            items.sort(key=key or (lambda c: getattr(c, "name", "")))
        return items

    async def get_bot_mapping(self) -> dict[Any, list[Any]]:
        """返回 ``{cog: [commands]}``；桥接层没有 cog 分组，统一放进 ``None`` 桶。"""
        bot = getattr(self.context, "bot", None)
        commands = getattr(bot, "all_commands", {}) or {}
        return {None: [commands[name] for name in sorted(commands)]}

    # -- 四个发送钩子（默认实现：把名字发到当前会话） ---------------------- #
    async def _send_lines(self, lines: list[str], title: str = "帮助") -> Any:
        ctx = self.context
        if ctx is None:
            return None
        body = "\n".join(lines) if lines else "（没有可显示的命令）"
        return await ctx.send(f"**{title}**\n{body}")

    async def send_bot_help(self, mapping: Any, /) -> Any:
        names: list[str] = []
        for commands in (mapping or {}).values():
            for command in commands or ():
                name = getattr(command, "qualified_name", None) or getattr(command, "name", "")
                if name:
                    names.append(f"`{self.clean_prefix}{name}`")
        return await self._send_lines(names, "可用命令")

    async def send_cog_help(self, cog: Any, /) -> Any:
        commands = getattr(cog, "get_commands", lambda: [])()
        names = [f"`{self.clean_prefix}{getattr(c, 'name', c)}`" for c in commands]
        return await self._send_lines(names, getattr(cog, "qualified_name", "分类"))

    async def send_group_help(self, group: Any, /) -> Any:
        names = [f"`{self.clean_prefix}{getattr(group, 'qualified_name', getattr(group, 'name', 'group'))}`"]
        for command in getattr(group, "commands", []) or []:
            names.append(f"  `{self.clean_prefix}{getattr(command, 'name', command)}`")
        return await self._send_lines(names, "命令组")

    async def send_command_help(self, command: Any, /) -> Any:
        text = getattr(command, "help", None) or getattr(command, "short_doc", "") or ""
        return await self._send_lines([f"`{self.get_command_signature(command)}`", text], "命令")

    # -- 上游 ``command`` 属性（把帮助本身注册成一条命令） ------------------ #
    @property
    def command(self) -> Any:
        """桥接层不把帮助注册成命令，返回 ``None``。"""
        return None

    async def prepare_help_command(self, ctx: Any, command: Any = None) -> None:
        self.context = ctx


class DefaultHelpCommand(HelpCommand):
    """上游的默认帮助实现（同名）；行为等价于基类。"""
