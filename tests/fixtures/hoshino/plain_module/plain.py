"""不是 Hoshino 插件的目录：detect 必须返回 ``None``。

里面的代码看起来像插件（有 Service 风格的名字、有命令函数），但**没有**任何 hoshino /
aiocqhttp 的导入或调用，所以不该被 Hoshino 兼容层认领。
"""

from __future__ import annotations


class LocalService:
    """自己写的一个类，名字像 Service 但不是 Hoshino 的。"""

    def __init__(self, name: str) -> None:
        self.name = name

    def on_command(self, name: str):
        def deco(func):
            return func

        return deco


service = LocalService("plain_module")


@service.on_command("ping")
async def ping() -> str:
    return "pong"
