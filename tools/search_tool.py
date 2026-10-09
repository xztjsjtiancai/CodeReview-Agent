"""按文本或正则搜代码。

回答的是「这个函数在哪儿被调的」「某个名字是在哪个文件里定义的」这类
跨文件问题。少了它,Agent 只能一个个猜路径去 read_file,又慢又容易漏。

为什么是逐行文本匹配而不是建 AST 符号索引:文本匹配零依赖,对语法有错的
文件照样能用,而且吐出来的 `文件:行号: 内容` 正好是模型最爱直接引用的格式。

跟 file_tool 一样,配置是运行时读的。
"""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from agent.errors import ToolError
import config as config_module
from tools.base import Tool
from tools.file_tool import resolve_safe


# 这些目录不用搜,搜了也是浪费时间
SKIP_PARTS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "vendor",
    ".tmp",
    "node_modules",
    "dist",
    "build",
}

# 超过这个大小的文件不当源码看
SCAN_SIZE_LIMIT = 2000000

# 单条匹配展示的文本长度上限
TEXT_LIMIT = 200

# max_results 的硬上限,防止模型一次要一万条
RESULT_CEILING = 500

# 默认返回条数
DEFAULT_RESULTS = 100


class SearchCodeTool(Tool):
    name = "search_code"

    description = (
        "在工作目录内的代码文件中搜索文本或正则表达式，返回匹配的"
        "「文件:行号: 内容」列表。用于查找某个函数/变量在哪里被定义或调用、"
        "定位具体实现位置。默认只搜索 .py 文件。"
    )

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "要搜索的关键词或正则表达式。",
            },
            "path": {
                "type": "string",
                "description": "搜索的起点，文件或目录；省略则为项目根目录。",
            },
            "file_glob": {
                "type": "string",
                "description": "文件名通配符，默认 *.py；设为 * 可搜索所有文本文件。",
            },
            "regex": {
                "type": "boolean",
                "description": "是否把 pattern 当作正则表达式，默认 false（按普通文本匹配）。",
            },
            "max_results": {
                "type": "integer",
                "description": "最多返回多少条匹配，默认 100。",
            },
        },
        "required": ["pattern"],
    }

    def run(
        self,
        pattern: str,
        path: str = ".",
        file_glob: str = "*.py",
        regex: bool = False,
        max_results: int = DEFAULT_RESULTS,
        **_: Any,
    ) -> dict[str, Any]:
        if not pattern:
            raise ToolError("搜索关键词不能为空", "给一个 pattern 参数")

        target = resolve_safe(path)
        if not target.exists():
            raise ToolError(f"Path not found: {path}", "确认一下路径写对没有")

        hit = self._make_matcher(pattern, regex)
        limit = max(1, min(int(max_results), RESULT_CEILING))

        if target.is_file():
            candidates = [target]
        else:
            candidates = self._walk(target, file_glob)

        found: list[dict[str, Any]] = []
        scanned = 0
        clipped = False

        for file_path in candidates:
            if len(found) >= limit:
                clipped = True
                break

            text = self._read_source(file_path)
            if text is None:
                continue

            scanned += 1

            for lineno, line in enumerate(text.splitlines(), start=1):
                if not hit(line):
                    continue

                found.append(self._make_match(file_path, lineno, line))

                if len(found) >= limit:
                    clipped = True
                    break

        return {
            "pattern": pattern,
            "regex": regex,
            "files_scanned": scanned,
            "match_count": len(found),
            "matches": found,
            "truncated": clipped,
        }

    # ==================================================================
    # 内部步骤

    @staticmethod
    def _make_match(file_path: Path, lineno: int, line: str) -> dict[str, Any]:
        try:
            shown = str(file_path.relative_to(config_module.CONFIG.workspace_root))
        except ValueError:
            shown = str(file_path)

        return {
            "file": shown,
            "line": lineno,
            "text": line.strip()[:TEXT_LIMIT],
        }

    @staticmethod
    def _read_source(path: Path) -> str | None:
        """读一个文件准备搜索。读不了就跳过,不让单个坏文件中断整轮搜索。"""
        try:
            if path.stat().st_size > SCAN_SIZE_LIMIT:
                return None
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    @staticmethod
    def _make_matcher(pattern: str, regex: bool):
        """造一个「这一行算不算命中」的判断函数。"""
        if not regex:
            return lambda line: pattern in line

        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            raise ToolError(
                f"正则表达式不合法:{exc}",
                "把 pattern 改对,或者把 regex 设成 false 按普通文本搜",
            ) from None

        return lambda line: compiled.search(line) is not None

    @staticmethod
    def _walk(root: Path, file_glob: str) -> list[Path]:
        """把目录下符合条件的文件都列出来。"""
        collected: list[Path] = []

        try:
            for item in sorted(root.rglob(file_glob)):
                if not item.is_file():
                    continue
                if _is_skipped(item):
                    continue
                collected.append(item)
        except OSError as exc:
            raise ToolError(f"遍历目录时出错:{exc}", "把搜索范围缩小一点试试") from None

        return collected


def _is_skipped(path: Path) -> bool:
    """路径里只要出现被跳过的目录名,就整条略过。"""
    for part in path.parts:
        if part in SKIP_PARTS:
            return True
    return False
