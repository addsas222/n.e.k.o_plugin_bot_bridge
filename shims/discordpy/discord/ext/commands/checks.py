"""``discord.ext.commands.checks``：前置检查（``@commands.has_permissions`` 之类）。

**诚实原则**：N.E.K.O. 没有 Discord 的角色 / 权限模型。``guild_only`` / ``dm_only``
这类只看上下文的检查可以如实判断；``has_permissions`` / ``is_owner`` / 角色检查需要有
权限数据，所以：

* 载荷（``CommandContext.raw``）里带了 ``permissions`` / ``is_owner`` / ``roles`` 时按它判断；
* 没带数据时**抛 ``UnsupportedCapabilityError``**（登记为能力缺失），
  而不是假装「有权限」或「没权限」。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from typing import Any

from ... import _runtime
from ...errors import (
    BotMissingAnyRole,
    BotMissingPermissions,
    BotMissingRole,
    CheckAnyFailure,
    CheckFailure,
    MissingAnyRole,
    MissingPermissions,
    MissingRole,
    NoPrivateMessage,
    NotOwner,
    NSFWChannelRequired,
    PrivateMessageOnly,
)
from ...permissions import Permissions

__all__ = [
    "bot_has_any_role",
    "bot_has_permissions",
    "bot_has_role",
    "check",
    "check_any",
    "dm_only",
    "guild_only",
    "has_any_role",
    "has_guild_permissions",
    "has_permissions",
    "has_role",
    "is_nsfw",
    "is_owner",
]

#: 检查函数上挂的装饰器标记。
_CHECKS_ATTR = "__commands_checks__"


class _Predicate:
    """无参数检查的装饰器对象（``guild_only`` / ``dm_only`` / ``is_nsfw``）。

    三种用法都成立：

    * ``@commands.guild_only()``（上游推荐写法）→ 返回自身，再作为装饰器登记到函数上；
    * ``@commands.guild_only``（不带括号）→ 直接把自身登记到函数上；
    * 命令求值时由核心调用 ``predicate(ctx)`` → 真正求值。

    上游要求第一种，且不带括号的写法会被静默当成真值（检查形同不存在）；这里两种写法
    都按正确语义工作。
    """

    def __init__(self, func: Callable[..., Any]) -> None:
        self._func = func
        self.__name__ = getattr(func, "__name__", "check")
        self.__doc__ = getattr(func, "__doc__", None)

    def __call__(self, target: Any = None, *args: Any, **kwargs: Any) -> Any:
        if target is None or _looks_like_context(target):
            # 工厂写法：``@commands.guild_only()`` —— 把这个检查对象交给下一个装饰器。
            return self if target is None else self._func(target, *args, **kwargs)
        return self.register(target)

    def register(self, func: Any) -> Any:
        """把自己登记成 ``func`` 的前置检查（返回 ``func``，可继续被装饰）。"""
        checks = getattr(func, _CHECKS_ATTR, None)
        if checks is None:
            checks = []
            setattr(func, _CHECKS_ATTR, checks)
        checks.append(self)
        return func

    def __repr__(self) -> str:
        return f"<check {self.__name__}>"


def _looks_like_context(value: Any) -> bool:
    return hasattr(value, "bot") and hasattr(value, "message")


def check(predicate: Callable[..., Any]) -> Callable[[Any], Any]:
    """把一个谓词注册成检查（``@commands.check``）。谓词可以是协程。"""

    def decorator(func: Any) -> Any:
        checks = getattr(func, _CHECKS_ATTR, None)
        if checks is None:
            checks = []
            setattr(func, _CHECKS_ATTR, checks)
        checks.append(predicate)
        return func

    return decorator


def check_any(*checks_: Callable[..., Any]) -> Callable[[Any], Any]:
    """至少满足其中一个检查（``@commands.check_any``）。全部失败时抛出第一个错误。"""

    async def predicate(ctx: Any) -> bool:
        errors: list[CheckFailure] = []
        for item in checks_:
            try:
                result = item(ctx)
                if inspect.isawaitable(result):
                    result = await result
                if result:
                    return True
            except CheckFailure as exc:
                errors.append(exc)
        if len(errors) == 1:
            raise errors[0]
        raise CheckAnyFailure(checks_, errors)

    return check(predicate)


# --------------------------------------------------------------------------- #
# 只看上下文的检查（可以如实判断）
# --------------------------------------------------------------------------- #


def _guild_only(ctx: Any) -> bool:
    if ctx.guild is None:
        raise NoPrivateMessage()
    return True


def _dm_only(ctx: Any) -> bool:
    if ctx.guild is not None:
        raise PrivateMessageOnly()
    return True


def _is_nsfw(ctx: Any) -> bool:
    channel = ctx.channel
    if channel is not None and hasattr(channel, "is_nsfw") and channel.is_nsfw():
        return True
    raise NSFWChannelRequired(channel)


guild_only = _Predicate(_guild_only)
"""``@commands.guild_only()``：只在服务器上下文可用。"""

dm_only = _Predicate(_dm_only)
"""``@commands.dm_only()``：只在私聊可用。"""

is_nsfw = _Predicate(_is_nsfw)
"""``@commands.is_nsfw()``：只在不合适的频道里可用（N.E.K.O. 按载荷判断）。"""


# --------------------------------------------------------------------------- #
# 需要权限 / 身份数据的检查（没数据就如实报能力缺失）
# --------------------------------------------------------------------------- #


def _permissions_of(ctx: Any, *, bot: bool) -> Permissions | None:
    raw = getattr(ctx, "raw", None) or {}
    key = "bot_permissions" if bot else "permissions"
    value = raw.get(key)
    if value is None and not bot:
        author = getattr(ctx, "author", None)
        value = getattr(author, "_bridge_permissions", None)
    if value is None:
        return None
    if isinstance(value, Permissions):
        return value
    try:
        if isinstance(value, dict):
            return Permissions(**{str(name): bool(item) for name, item in value.items()})
        if isinstance(value, (list, tuple, set)):
            return Permissions(**{str(item): True for item in value})
    except TypeError:
        # 载荷里有本 shim 不认识的权限名：当成「没有可用数据」（上层会报能力缺失），
        # 而不是假装有权限 / 没权限。
        return None
    return None


def _has_permissions(required: dict[str, bool], *, bot: bool) -> Callable[[Any], bool]:
    def predicate(ctx: Any) -> bool:
        actual = _permissions_of(ctx, bot=bot)
        if actual is None:
            raise _runtime.unsupported(
                "commands.has_permissions",
                "N.E.K.O. 没有 Discord 权限模型；请在 CommandContext.raw 里提供 "
                f"{'bot_permissions' if bot else 'permissions'}（权限名 -> bool 的字典）才能判断",
            )
        missing = [name for name, wanted in required.items() if bool(wanted) and not getattr(actual, name, False)]
        if missing:
            raise (BotMissingPermissions(missing) if bot else MissingPermissions(missing))
        return True

    return predicate


def has_permissions(**perms: bool) -> Callable[[Any], Any]:
    """``@commands.has_permissions(manage_messages=True)``（缺权限数据时抛能力缺失）。"""
    invalid = [name for name in perms if name not in Permissions._VALUES]
    if invalid:
        raise TypeError(f"Invalid permission(s): {', '.join(invalid)}")
    return check(_has_permissions(dict(perms), bot=False))


def has_guild_permissions(**perms: bool) -> Callable[[Any], Any]:
    """同 ``has_permissions``（N.E.K.O. 里没有「服务器权限 vs 频道权限」之分）。"""
    return has_permissions(**perms)


def bot_has_permissions(**perms: bool) -> Callable[[Any], Any]:
    """``@commands.bot_has_permissions(...)``（同样需要 ``raw['bot_permissions']``）。"""
    invalid = [name for name in perms if name not in Permissions._VALUES]
    if invalid:
        raise TypeError(f"Invalid permission(s): {', '.join(invalid)}")
    return check(_has_permissions(dict(perms), bot=True))


def is_owner() -> Callable[[Any], Any]:
    """``@commands.is_owner()``：按 ``raw['is_owner']`` 或配置 ``discord_owner_ids`` 判断。"""

    def predicate(ctx: Any) -> bool:
        raw = getattr(ctx, "raw", None) or {}
        if "is_owner" in raw:
            if not bool(raw.get("is_owner")):
                raise NotOwner()
            return True
        bot = getattr(ctx, "bot", None)
        owner_ids = set(getattr(bot, "_bridge_owner_ids", ()) or ())
        if not owner_ids:
            raise _runtime.unsupported(
                "commands.is_owner",
                "N.E.K.O. 里没有「机器人所有者」概念；请在 CommandContext.raw 里提供 "
                "is_owner，或配置 bridge.discord_owner_ids",
            )
        if int(getattr(ctx.author, "id", 0)) not in owner_ids:
            raise NotOwner()
        return True

    return check(predicate)


def _role_names(ctx: Any, *, bot: bool) -> tuple[list[str], bool]:
    """返回（拥有的角色名, 是否有真实数据）。"""
    raw = getattr(ctx, "raw", None) or {}
    key = "bot_roles" if bot else "roles"
    if key in raw:
        value = raw[key] or []
        return [str(item) for item in value], True
    if not bot:
        roles = getattr(getattr(ctx, "author", None), "roles", None)
        if roles:
            return [getattr(role, "name", str(role)) for role in roles], True
    return [], False


def _has_role_check(role: Any, *, bot: bool) -> Callable[[Any], bool]:
    wanted = _role_name(role)

    def predicate(ctx: Any) -> bool:
        names, known = _role_names(ctx, bot=bot)
        if not known:
            raise _runtime.unsupported(
                "commands.has_role",
                "N.E.K.O. 没有 Discord 角色模型；请在 CommandContext.raw 里提供 "
                f"{'bot_roles' if bot else 'roles'}（角色名列表）才能判断",
            )
        if wanted not in names:
            raise (BotMissingRole(role) if bot else MissingRole(role))
        return True

    return predicate


def _has_any_role_check(roles: Sequence[Any], *, bot: bool) -> Callable[[Any], bool]:
    def predicate(ctx: Any) -> bool:
        names, known = _role_names(ctx, bot=bot)
        if not known:
            raise _runtime.unsupported(
                "commands.has_any_role",
                "N.E.K.O. 没有 Discord 角色模型；请在 CommandContext.raw 里提供 "
                f"{'bot_roles' if bot else 'roles'}（角色名列表）才能判断",
            )
        if any(_role_name(role) in names for role in roles):
            return True
        raise (BotMissingAnyRole(roles) if bot else MissingAnyRole(roles))

    return predicate


def _role_name(role: Any) -> str:
    return role if isinstance(role, str) else str(getattr(role, "name", role))


def has_role(role: Any) -> Callable[[Any], Any]:
    """``@commands.has_role('管理员')``。"""
    return check(_has_role_check(role, bot=False))


def bot_has_role(role: Any) -> Callable[[Any], Any]:
    """``@commands.bot_has_role('管理员')``。"""
    return check(_has_role_check(role, bot=True))


def has_any_role(*roles: Any) -> Callable[[Any], Any]:
    """``@commands.has_any_role('a', 'b')``。"""
    return check(_has_any_role_check(list(roles), bot=False))


def bot_has_any_role(*roles: Any) -> Callable[[Any], Any]:
    """``@commands.bot_has_any_role('a', 'b')``。"""
    return check(_has_any_role_check(list(roles), bot=True))
