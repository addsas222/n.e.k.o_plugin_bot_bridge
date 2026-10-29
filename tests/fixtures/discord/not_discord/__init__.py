"""不是 discord.py 插件：另一个框架风格的目录（``detect`` 必须返回 ``None``）。

用 ``@app.command`` 这类**非** discord 装饰器，验证兼容层不会把「别的框架的命令」
误判成 discord.py 插件（装饰器前缀必须属于 discord 的命令客户端才算信号）。
"""

from __future__ import annotations


class Router:
    """假装是第三方框架的路由器。"""

    def command(self, *args: object, **kwargs: object) -> object:
        def decorator(func: object) -> object:
            return func

        return decorator


app = Router()


@app.command("hello")
async def hello(ctx: object) -> None:
    """别的框架的命令。"""
