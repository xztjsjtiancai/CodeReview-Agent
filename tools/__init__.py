"""工具包。

这里是唯一把「具体工具」和「注册表」绑在一起的地方。上层代码只认
ToolRegistry,以后想加工具,在这一个文件里添一行就够了。
"""

from __future__ import annotations

from tools.ast_tool import AstAnalyzerTool
from tools.base import Tool
from tools.file_tool import ListDirTool
from tools.file_tool import ReadFileTool
from tools.registry import ToolRegistry
from tools.search_tool import SearchCodeTool


__all__ = [
    "Tool",
    "ToolRegistry",
    "ReadFileTool",
    "ListDirTool",
    "AstAnalyzerTool",
    "SearchCodeTool",
    "build_default_registry",
]


def build_default_registry() -> ToolRegistry:
    """把当前版本提供的四个工具都装上。"""
    registry = ToolRegistry()

    registry.register(ReadFileTool())
    registry.register(ListDirTool())
    registry.register(AstAnalyzerTool())
    registry.register(SearchCodeTool())

    return registry
