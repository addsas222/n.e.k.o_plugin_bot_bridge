"""``hoshino.priv`` 的兼容实现：用户权限等级与黑名单。

等级数值与上游一致（``BLACK=-999`` … ``SUPERUSER=999``），``get_user_priv`` 的判定也照抄
上游：``SUPERUSERS`` → ``SUPERUSER``；黑名单 → ``BLACK``；白名单 → ``WHITE``；群里按
``sender.role``（``member`` / ``admin`` / ``administrator`` / ``owner``）映射；私聊 →
``PRIVATE``。

**一处有意的偏差**：上游 ``check_priv`` 对非群聊直接 ``return False``（Hoshino 是群聊机器人，
私聊命令一律不响应）。N.E.K.O. 的对话基本就是"私聊"，照抄会让所有命令失效，因此桥接默认
放开私聊（``hoshino_allow_private = true``，可关）：私聊发送者按 ``priv.PRIVATE`` 参与比较，
``use_priv`` 高于 ``PRIVATE`` 的服务（例如 ``manage_priv=SUPERUSER``）仍然只有超管能用。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from hoshino import _runtime

__all__ = [
    "ADMIN",
    "BLACK",
    "DEFAULT",
    "NORMAL",
    "OWNER",
    "PRIVATE",
    "SU",
    "SUPERUSER",
    "WHITE",
    "check_block_group",
    "check_block_user",
    "check_priv",
    "get_user_priv",
    "set_block_group",
    "set_block_user",
]

BLACK = -999
DEFAULT = 0
NORMAL = 1
PRIVATE = 10
ADMIN = 21
OWNER = 22
WHITE = 51
SUPERUSER = 999
SU = SUPERUSER

#: 群 / 用户的临时拉黑（``group_id -> 解封时间``）。
_black_group: dict[Any, datetime] = {}
_black_user: dict[Any, datetime] = {}


def set_block_group(group_id: Any, time: timedelta) -> None:
    """临时拉黑一个群。"""
    _black_group[group_id] = datetime.now() + time


def set_block_user(user_id: Any, time: timedelta) -> None:
    """临时拉黑一个用户（超管除外）。"""
    if str(user_id) not in _superusers():
        _black_user[user_id] = datetime.now() + time


def check_block_group(group_id: Any) -> bool:
    """这个群是否在拉黑期内（过期自动解除，上游语义）。"""
    if group_id in _black_group and datetime.now() > _black_group[group_id]:
        del _black_group[group_id]
        return False
    return bool(group_id in _black_group)


def check_block_user(user_id: Any) -> bool:
    """这个用户是否被拉黑（配置里的 ``BLACK_LIST`` 永久生效，上游语义）。"""
    if str(user_id) in _blacklist():
        return True
    if user_id in _black_user and datetime.now() > _black_user[user_id]:
        del _black_user[user_id]
        return False
    return bool(user_id in _black_user)


def get_user_priv(ev: Any) -> int:
    """这条消息的发送者权限等级（上游语义）。"""
    uid = ev.user_id
    if str(uid) in _superusers():
        return SUPERUSER
    if check_block_user(uid):
        return BLACK
    if str(uid) in _whitelist():
        return WHITE
    if ev.get("message_type") == "group":
        if not ev.get("anonymous"):
            sender = ev.get("sender") or {}
            role = sender.get("role") if hasattr(sender, "get") else None
            if role == "member":
                return NORMAL
            if role in ("admin", "administrator"):
                return ADMIN
            if role == "owner":
                return OWNER
        return NORMAL
    if ev.get("message_type") == "private":
        return PRIVATE
    return NORMAL


def check_priv(ev: Any, require: int) -> bool:
    """发送者权限是否达到 ``require``。"""
    if ev.get("message_type") == "group":
        return bool(get_user_priv(ev) >= require)
    if _allow_private():
        return bool(get_user_priv(ev) >= require)
    return False


def _allow_private() -> bool:
    return bool(_runtime.settings().get("allow_private", True))


def _snapshot_list(key: str) -> set[str]:
    values = _runtime.config_snapshot().get(key) or ()
    if isinstance(values, (str, bytes)):
        values = (values,)
    return {str(item) for item in values}


def _superusers() -> set[str]:
    return _snapshot_list("SUPERUSERS")


def _blacklist() -> set[str]:
    return _snapshot_list("BLACK_LIST")


def _whitelist() -> set[str]:
    return _snapshot_list("WHITE_LIST")
