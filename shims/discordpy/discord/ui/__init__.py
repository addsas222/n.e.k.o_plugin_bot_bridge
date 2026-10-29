"""``discord.ui`` —— 交互组件（View / Button / Select / Modal / TextInput）。

N.E.K.O. 的对话通道是**纯文本**：没有「组件被点击 → 回调」这条链路，也没有
interaction 端点。所以这里的定位是：

* **能构造、能挂载、能取属性** —— cog 在模块级定义 View 子类、在 ``setup`` 里
  实例化、在命令里 ``await ctx.send(view=...)``，这些都不会炸（``Messageable.send``
  会把 view 记成一条 component 备注，文本照常投递）；
* **不能真的等交互** —— ``View.wait()`` 与 ``InteractionResponse.*`` 显式抛
  ``UnsupportedCapabilityError``，不返回假结果、不假装「用户点了取消」。

字段名与取值对齐上游 discord.py 2.x（``ButtonStyle`` / ``TextStyle`` 见
:mod:`discord.enums`，值与上游一致）。
"""

from __future__ import annotations

import copy as _copy
from typing import Any, Callable

from .._runtime import unsupported
from ..enums import ButtonStyle, ComponentType, TextStyle

__all__ = [
    "ActionRow",
    "Button",
    "ChannelSelect",
    "Item",
    "MentionableSelect",
    "Modal",
    "RoleSelect",
    "Select",
    "SelectOption",
    "TextInput",
    "TextInputStyle",
    "UserSelect",
    "View",
    "button",
    "channel_select",
    "mentionable_select",
    "role_select",
    "select",
    "user_select",
]

#: 上游 ``discord.ui.TextInputStyle`` 就是 ``discord.TextStyle`` 的别名。
TextInputStyle = TextStyle


class Item:
    """所有交互组件的基类（对应上游 ``discord.ui.Item``）。"""

    def __init__(self, *, row: int | None = None) -> None:
        self._row = row
        self._view: View | None = None
        self.callback: Callable[..., Any] | None = None

    @property
    def row(self) -> int | None:
        return self._row

    @row.setter
    def row(self, value: int | None) -> None:
        self._row = value

    @property
    def view(self) -> "View | None":
        return self._view

    def copy(self) -> "Item":
        clone = _copy.copy(self)
        clone._view = None
        return clone


class _ItemDecorator:
    """把 ``@ui.button(...)`` / ``@ui.select(...)`` 的返回值落到类体里。"""

    def __init__(self, factory: Callable[..., Item], kwargs: dict[str, Any]) -> None:
        self._factory = factory
        self._kwargs = kwargs

    def __call__(self, func: Callable[..., Any]) -> Item:
        item = self._factory(**self._kwargs)
        item.callback = func
        item._decorated_name = getattr(func, "__name__", "")  # type: ignore[attr-defined]
        return item


class Button(Item):
    """按钮（对应上游 ``discord.ui.Button``）。"""

    def __init__(
        self,
        *,
        style: ButtonStyle = ButtonStyle.secondary,
        label: str | None = None,
        disabled: bool = False,
        custom_id: str | None = None,
        url: str | None = None,
        emoji: Any = None,
        row: int | None = None,
    ) -> None:
        super().__init__(row=row)
        self.style = style
        self.label = label
        self.disabled = bool(disabled)
        self.custom_id = custom_id
        self.url = url
        self.emoji = emoji
        self.type = ComponentType.button


def button(
    *,
    label: str | None = None,
    custom_id: str | None = None,
    style: ButtonStyle = ButtonStyle.secondary,
    emoji: Any = None,
    row: int | None = None,
    disabled: bool = False,
    url: str | None = None,
) -> _ItemDecorator:
    """``@discord.ui.button(label="确认", style=discord.ButtonStyle.green)``。"""
    return _ItemDecorator(
        Button,
        {
            "label": label,
            "custom_id": custom_id,
            "style": style,
            "emoji": emoji,
            "row": row,
            "disabled": disabled,
            "url": url,
        },
    )


class SelectOption:
    """下拉选项（对应上游 ``discord.SelectOption``）。"""

    def __init__(
        self,
        label: str = "",
        value: Any = None,
        description: str | None = None,
        emoji: Any = None,
        default: bool = False,
    ) -> None:
        self.label = label
        self.value = label if value is None else value
        self.description = description
        self.emoji = emoji
        self.default = bool(default)

    def __repr__(self) -> str:
        return f"<SelectOption label={self.label!r} value={self.value!r}>"


class Select(Item):
    """字符串下拉（对应上游 ``discord.ui.Select``）。"""

    component_type = ComponentType.string_select

    def __init__(
        self,
        *,
        placeholder: str | None = None,
        min_values: int = 1,
        max_values: int = 1,
        options: list[SelectOption] | None = None,
        disabled: bool = False,
        custom_id: str | None = None,
        row: int | None = None,
    ) -> None:
        super().__init__(row=row)
        self.placeholder = placeholder
        self.min_values = int(min_values)
        self.max_values = int(max_values)
        self.options = list(options or [])
        self.disabled = bool(disabled)
        self.custom_id = custom_id
        self.values: list[str] = []
        self.type = self.component_type

    def add_option(self, option: SelectOption) -> None:
        self.options.append(option)


class UserSelect(Select):
    component_type = ComponentType.user_select


class RoleSelect(Select):
    component_type = ComponentType.role_select


class MentionableSelect(Select):
    component_type = ComponentType.mentionable_select


class ChannelSelect(Select):
    component_type = ComponentType.channel_select


def select(
    *,
    placeholder: str | None = None,
    min_values: int = 1,
    max_values: int = 1,
    options: list[SelectOption] | None = None,
    disabled: bool = False,
    custom_id: str | None = None,
    row: int | None = None,
) -> _ItemDecorator:
    """``@discord.ui.select(options=[...])``。"""
    return _ItemDecorator(
        Select,
        {
            "placeholder": placeholder,
            "min_values": min_values,
            "max_values": max_values,
            "options": options,
            "disabled": disabled,
            "custom_id": custom_id,
            "row": row,
        },
    )


def _select_decorator(factory: Callable[..., Select]) -> Callable[..., _ItemDecorator]:
    def outer(**kwargs: Any) -> _ItemDecorator:
        return _ItemDecorator(factory, kwargs)

    return outer


user_select = _select_decorator(UserSelect)
role_select = _select_decorator(RoleSelect)
mentionable_select = _select_decorator(MentionableSelect)
channel_select = _select_decorator(ChannelSelect)


class TextInput(Item):
    """弹窗里的输入框（对应上游 ``discord.ui.TextInput``）。"""

    def __init__(
        self,
        *,
        label: str = "",
        style: TextStyle = TextStyle.short,
        custom_id: str | None = None,
        placeholder: str | None = None,
        required: bool = True,
        max_length: int | None = None,
        min_length: int | None = None,
        value: str | None = None,
        row: int | None = None,
    ) -> None:
        super().__init__(row=row)
        self.label = label
        self.style = style
        self.custom_id = custom_id
        self.placeholder = placeholder
        self.required = bool(required)
        self.max_length = max_length
        self.min_length = min_length
        self.value = value
        self.type = ComponentType.text_input


class Modal:
    """弹窗（对应上游 ``discord.ui.Modal``）。

    支持两种写法：

    * ``class MyModal(discord.ui.Modal, title="反馈"): ...``（类声明里给 title）
    * ``MyModal(title="反馈")``（构造时给 title）
    """

    title: str = ""
    timeout: float | None = None
    custom_id: str | None = None
    __modal_children__: dict[str, Item] = {}

    def __init_subclass__(
        cls,
        *,
        title: Any = None,
        timeout: float | None = None,
        custom_id: str | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init_subclass__(**kwargs)
        if title is not None:
            cls.title = str(title)
        if timeout is not None:
            cls.timeout = timeout
        if custom_id is not None:
            cls.custom_id = custom_id
        # 类体里声明的 TextInput / Select 会被收集（与 View 同一套规则）
        children: dict[str, Item] = {}
        for base in reversed(cls.__mro__):
            for name, value in vars(base).items():
                if isinstance(value, Item):
                    children[name] = value
        cls.__modal_children__ = children

    def __init__(
        self,
        *,
        title: Any = None,
        timeout: float | None = None,
        custom_id: str | None = None,
    ) -> None:
        if title is not None:
            self.title = str(title)
        if timeout is not None:
            self.timeout = timeout
        if custom_id is not None:
            self.custom_id = custom_id
        self._children: list[Item] = [item.copy() for item in type(self).__modal_children__.values()]

    @property
    def children(self) -> list[Item]:
        return self._children

    def add_item(self, item: Item) -> None:
        self._children.append(item)

    def remove_item(self, item: Item) -> None:
        self._children.remove(item)

    def __len__(self) -> int:
        return len(self._children)

    def __iter__(self):
        return iter(self._children)

    async def on_submit(self, interaction: Any) -> None:
        """上游默认空实现；插件覆盖它。"""

    async def on_error(self, interaction: Any, error: Exception) -> None:
        """上游默认空实现；插件覆盖它。"""

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "custom_id": self.custom_id,
            "components": [type(item).__name__ for item in self._children],
        }


class ActionRow(Item):
    """组件行容器（对应上游 ``discord.ui.ActionRow``，简化为纯数据持有）。"""

    def __init__(self, *items: Item, row: int | None = None) -> None:
        super().__init__(row=row)
        self.children: list[Item] = list(items)

    def add_item(self, item: Item) -> None:
        self.children.append(item)


class View:
    """视图容器（对应上游 ``discord.ui.View``）。

    子类里用 ``@discord.ui.button(...)`` / ``@discord.ui.select(...)`` 声明组件时，
    装饰结果是 :class:`Item` 实例；``__init_subclass__`` 会把它们收集进
    ``__view_children__``（与上游同名），实例化时挂到 ``children`` 上。
    """

    __view_children__: dict[str, Item] = {}

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        children: dict[str, Item] = {}
        for base in reversed(cls.__mro__):
            for name, value in vars(base).items():
                if isinstance(value, Item):
                    children[name] = value
        cls.__view_children__ = children

    def __init__(self, *, timeout: float | None = 180.0) -> None:
        self.timeout = timeout
        self._children: list[Item] = []
        for item in type(self).__view_children__.values():
            self.add_item(item.copy())
        self._finished = False

    # -- 组件管理 ---------------------------------------------------------- #
    @property
    def children(self) -> list[Item]:
        return self._children

    def add_item(self, item: Item) -> "View":
        if item._view is not None:
            item = item.copy()
        item._view = self
        self._children.append(item)
        return self

    def remove_item(self, item: Item) -> "View":
        self._children = [child for child in self._children if child is not item]
        item._view = None
        return self

    def clear_items(self) -> "View":
        for item in self._children:
            item._view = None
        self._children.clear()
        return self

    def __len__(self) -> int:
        return len(self._children)

    def __iter__(self):
        return iter(self._children)

    def __getitem__(self, index: int) -> Item:
        return self._children[index]

    # -- 生命周期 ---------------------------------------------------------- #
    @property
    def is_finished(self) -> bool:
        return self._finished

    def is_dispatching(self) -> bool:
        return False

    def stop(self) -> None:
        self._finished = True

    async def interaction_check(self, interaction: Any) -> bool:
        return True

    async def on_timeout(self) -> None:
        """上游默认空实现；插件覆盖它。"""

    async def on_error(self, interaction: Any, error: Exception, item: Item) -> None:
        """上游默认空实现；插件覆盖它。"""

    async def wait(self) -> bool:
        """等待一次交互 —— N.E.K.O. 没有交互来源，显式失败。

        上游用它做「点按钮确认」这类流程。桥接层没有组件回调通道，所以这里不返回
        ``True/False``（那会让插件以为用户点了取消），而是抛能力缺失。
        """
        raise unsupported(
            "discord.ui.View.wait",
            "N.E.K.O. 没有 Discord 交互（组件点击）通道；请把确认流程改成文本命令",
        )
