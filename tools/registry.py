"""工具注册表。

它站在「模型的意图」和「本机的能力」中间,是两者唯一的接口点。
往上,把注册过的工具整理成 JSON Schema 交给模型;往下,把模型报出的
工具名和参数路由到具体实现。

所有异常在这里被收成 ToolError 家族,这样 Agent 循环不会因为某个工具
内部的小毛病整个断掉。
"""

from __future__ import annotations

import json
from typing import Any

from agent.errors import ToolArgumentError
from agent.errors import ToolNotFound
from tools.base import Tool


# 报错时附带的原始内容最多留这么长
RAW_LIMIT = 200


class ToolRegistry:
    """管着所有工具的注册和调用。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    # ==================================================================
    # 注册

    def register(self, tool: Tool) -> None:
        if not tool.name:
            raise ValueError("工具必须有 name,否则模型没法引用它")
        self._tools[tool.name] = tool

    def register_all(self, tools: list[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    # ==================================================================
    # 查询

    def get(self, name: str) -> Tool:
        if name in self._tools:
            return self._tools[name]

        available = ", ".join(sorted(self._tools))
        if not available:
            available = "无"

        raise ToolNotFound(
            f"没有名为 {name!r} 的工具",
            f"当前可用的工具:{available}",
        )

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    # ==================================================================
    # 导出

    def schemas(self) -> list[dict[str, Any]]:
        """给模型的 tools 参数用。"""
        collected = []
        for tool in self._tools.values():
            collected.append(tool.schema())
        return collected

    def describe(self) -> list[dict[str, Any]]:
        """给 /tools 命令用。和 schemas 分开是因为面向对象不同:
        一个给机器读,一个给人读。"""
        collected = []
        for tool in self._tools.values():
            collected.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
            )
        return collected

    # ==================================================================
    # 派发

    def dispatch(self, name: str, arguments: dict[str, Any] | str | None) -> dict[str, Any]:
        """执行一次工具调用。

        参数可以是 dict,也可以是模型吐出来的原始 JSON 字符串。
        可能抛出的异常:ToolNotFound(没这个工具)、ToolArgumentError
        (参数不合法)、ToolError(工具内部失败)。
        """
        tool = self.get(name)
        raw = self._parse_arguments(tool, arguments)
        checked = tool.validate(raw)
        return tool.run(**checked)

    @staticmethod
    def _parse_arguments(
        tool: Tool,
        arguments: dict[str, Any] | str | None,
    ) -> dict[str, Any]:
        """把参数整理成 dict。"""
        if arguments is None:
            return {}

        if arguments == "":
            return {}

        if isinstance(arguments, dict):
            return arguments

        if isinstance(arguments, str):
            return _decode_json_object(tool, arguments)

        raise ToolArgumentError(f"工具 {tool.name} 收到的参数类型不支持:{type(arguments).__name__}")


def _decode_json_object(tool: Tool, text: str) -> dict[str, Any]:
    """解析模型给的 JSON 字符串。

    单独拆出来是因为这里的报错信息要尽量有用 —— 模型看到「不是合法 JSON」
    加上原始片段,下一轮通常就能改对。
    """
    try:
        decoded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ToolArgumentError(
            f"工具 {tool.name} 的参数不是合法 JSON:{exc.msg}",
            f"原始内容:{text[:RAW_LIMIT]}",
        ) from None

    if isinstance(decoded, dict):
        return decoded

    raise ToolArgumentError(
        f"工具 {tool.name} 的参数应该是 JSON 对象,实际收到 {type(decoded).__name__}"
    )
