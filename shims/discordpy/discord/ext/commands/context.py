"""``discord.ext.commands.context.Context``：一次命令调用的上下文。

``send`` / ``reply`` 是插件回话的主入口，走 ``discord.abc.Messageable`` 的实现：
文本进当前 ``RunReport.replies``，附件 / 交互组件只做结构化记录（N.E.K.O. 没有载体）。
"""

from __future__ import annotations

import inspect
from typing import Any

from ... import _runtime
from ...abc import Messageable

__all__ = ["Context"]


class Context(Messageable):
    """命令上下文（上游 ``discord.ext.commands.Context`` 的子集）。"""

    def __init__(
        self,
        *,
        bot: Any,
        view: Any = None,
        args: Any = None,
        kwargs: Any = None,
        prefix: str | None = None,
        message: Any = None,
        command: Any = None,
        invoked_with: str = "",
        invoked_parents: Any = (),
        invoked_subcommand: Any = None,
        subcommand_passed: Any = None,
    ) -> None:
        self.bot = bot
        self.view = view
        self.args: list[Any] = list(args or [])
        self.kwargs: dict[str, Any] = dict(kwargs or {})
        self.prefix = prefix
        self.message = message
        self.command = command
        self.invoked_with = invoked_with
        self.invoked_parents: list[str] = list(invoked_parents or ())
        self.invoked_subcommand = invoked_subcommand
        self.subcommand_passed = subcommand_passed
        #: 兼容层填的原始载荷（权限 / 角色 / 提及数据都在这里）
        self.raw: dict[str, Any] = dict(getattr(message, "raw", None) or {})
        self.command_failed = False
        self.current_parameter: Any = None
        self.current_argument: Any = None
        self._bridge_plugin_id = getattr(bot, "_bridge_plugin_id", "")
        self._bridge_sender = getattr(getattr(message, "channel", None), "_bridge_sender", None) or getattr(
            bot, "_bridge_sender", None
        )
        self._bridge_permissions = None

    # ---- 视图 ------------------------------------------------------------ #

    @property
    def channel(self) -> Any:
        return getattr(self.message, "channel", None)

    @property
    def guild(self) -> Any:
        return getattr(self.message, "guild", None)

    @property
    def author(self) -> Any:
        return getattr(self.message, "author", None)

    @property
    def me(self) -> Any:
        return self.bot.user

    @property
    def clean_prefix(self) -> str:
        """可读前缀（``@机器人`` 会显示成 ``@N.E.K.O.``）。"""
        prefix = self.prefix or ""
        user = getattr(self.bot, "user", None)
        if user is not None and prefix in (f"<@{user.id}>", f"<@!{user.id}>", f"<@{user.id}> "):
            return f"@{user.display_name} "
        return prefix

    @property
    def cog(self) -> Any:
        return self.command.cog if self.command is not None else None

    @property
    def permissions(self) -> Any:
        """调用者的权限位：载荷里带了 ``permissions`` 才为真，否则是「无数据」（不是「无权限」）。"""
        from ...permissions import Permissions

        return self._bridge_permissions if self._bridge_permissions is not None else Permissions.none()

    @property
    def author_permissions(self) -> Any:
        return self.permissions

    @property
    def valid(self) -> bool:
        """前缀命中了、命令也找到了。"""
        return self.prefix is not None and self.command is not None

    # ---- 发送 ------------------------------------------------------------ #

    def _bridge_channel_label(self) -> str:
        name = getattr(self.channel, "name", "")
        return f"Context({name or 'channel'})"

    async def send(self, content: Any = None, **kwargs: Any) -> Any:
        """在当前频道发言（文本进 ``CommandResult.replies``）。"""
        return await Messageable.send(self, content, **kwargs)

    async def reply(self, content: Any = None, **kwargs: Any) -> Any:
        """``Context.reply``（上游 2.x 起是 ``send`` 的同义写法）。"""
        return await Messageable.send(self, content, **kwargs)

    async def confirm_reply(self, *args: Any, **kwargs: Any) -> Any:
        """上游的交互确认（N.E.K.O. 没有确认组件）。"""
        raise _runtime.unsupported("Context.confirm_reply", "N.E.K.O. 没有消息组件确认流")

    async def fetch_message(self, id: int) -> Any:  # noqa: A002 — 与上游签名一致
        channel = self.channel
        if channel is None:
            raise _runtime.unsupported("Context.fetch_message", "当前上下文没有频道")
        return await channel.fetch_message(id)

    async def invoke(self, command: Any, *args: Any, **kwargs: Any) -> Any:
        """调用一个命令 / 回调，自动注入 Cog 与上下文（上游 ``Context.invoke``）。"""
        from .core import Command

        callback = command.callback if isinstance(command, Command) else command
        cog = self.cog
        if cog is not None and not inspect.ismethod(callback) and getattr(callback, "__self__", None) is None:
            return await callback(cog, self, *args, **kwargs)
        return await callback(self, *args, **kwargs)

    async def reinvoke(self, *, call_hooks: bool = False, restart: bool = True) -> None:
        """重新解析并执行当前命令（上游 ``Context.reinvoke``）。

        ``call_hooks`` 在上游表示「不要把参数解析结果缓存到 ``ctx``」；本 shim 每次都重新
        解析，因此该开关没有可观察差别（这里显式说明，而不是假装支持）。
        """
        del call_hooks, restart
        if self.command is None:
            raise _runtime.unsupported("Context.reinvoke", "当前上下文没有命令")
        from .view import StringView

        view = StringView(getattr(self.message, "content", ""))
        if self.prefix:
            view.skip_string(self.prefix)
        view.skip_ws()
        view.get_word()  # 吃掉命令名，回到参数部分
        self.view = view
        self.args = []
        self.kwargs = {}
        await self.command.invoke(self)

    # ---- 帮助 ------------------------------------------------------------ #

    async def send_help(self, entity: Any = None) -> Any:
        """发一条帮助文本。

        N.E.K.O. 侧没有 ``HelpCommand`` 子类（那需要复刻整棵帮助命令体系），这里给一个
        **只用真实注册数据**生成的最小帮助：命令名 + 用法 + 简介。
        """
        lines: list[str] = []
        from .core import Group

        target = entity
        if target is None and isinstance(self.command, Group):
            target = self.command
        commands = list(getattr(target, "commands", {}) or ()) if target is not None else []
        if commands:
            label = getattr(target, "qualified_name", "") or "本机器人"
            lines.append(f"「{label}」的子命令：")
            lines.extend(self._help_lines(commands))
            return await self.send("\n".join(lines))
        all_commands = list(getattr(self.bot, "all_commands", {}).values())
        if all_commands:
            seen: set[str] = set()
            unique = []
            for command in all_commands:
                if command.name in seen:
                    continue
                seen.add(command.name)
                unique.append(command)
            lines.extend(self._help_lines(unique))
            return await self.send("\n".join(lines))
        return await self.send("没有可显示的命令。")

    def _help_lines(self, commands: Any) -> list[str]:
        lines: list[str] = []
        for command in commands:
            if getattr(command, "hidden", False):
                continue
            usage = command.usage or f"{self.clean_prefix}{command.qualified_name} {command.signature}".strip()
            lines.append(f"  {usage}  —  {command.short_doc or command.name}")
        return lines

    # ---- 其它 ------------------------------------------------------------ #

    def typing(self) -> Any:
        return Messageable.typing(self)

    def __repr__(self) -> str:
        return (
            f"<Context prefix={self.prefix!r} command={self.command!r} "
            f"author={getattr(self.author, 'name', None)!r}>"
        )
