"""确定性静态分析 —— 本项目的核心。

分工是这样的:

    标准库 ast 负责「事实」:哪一行、什么问题、什么严重级别
    模型只负责「判断」:这个问题在业务上意味着什么、该怎么改

所以这里的检查全部基于语法树,不用正则去猜。AST 有个别人替代不了的好处:
字符串里写了个 "print(x)" 不会被误报,注释里的 except: 也不会。
代价是每条规则都得写一个 visitor,但换来的是零误报,而「审查结果可信」
恰恰是这个项目相对「把代码贴给 ChatGPT」最大的区别。

语法错误一律当成正常输入处理:用 issue 的形式返回,不往上抛异常。
用户给一个写坏的文件是很常见的事,不该让程序崩。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from agent.errors import ToolError
import config as config_module
from tools.base import Tool
from tools.file_tool import read_text_with_fallback
from tools.file_tool import resolve_safe


# 函数超过这么多行,算太长
FUNCTION_LINE_LIMIT = 50

# 控制流嵌套超过这么多层,算太深
NESTING_LIMIT = 3

# 一次最多报这么多条,防止一个大文件把上下文刷满
ISSUE_LIMIT = 200

# 这几个函数被调用,基本可以直接判高危
RISKY_FUNCTIONS = {"eval", "exec", "compile", "__import__"}

# 参与嵌套深度统计的语句
NESTING_TYPES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.With,
    ast.AsyncWith,
    ast.Try,
)

# 取代码片段时最多带几行
SNIPPET_ROWS = 3


# ======================================================================
# 拼装 issue
# ======================================================================


def grab_snippet(lines: list[str], start: int | None, end: int | None = None) -> str:
    """抓一小段源码,让模型能直接引用。"""
    if not start:
        return ""

    if start < 1 or start > len(lines):
        return ""

    if end is None:
        end = start

    last = min(end, len(lines), start + SNIPPET_ROWS - 1)
    chunk = lines[start - 1 : last]
    return "\n".join(chunk).strip()


def build_issue(
    lines: list[str],
    lineno: int | None,
    kind: str,
    severity: str,
    message: str,
    end_lineno: int | None = None,
) -> dict[str, Any]:
    """造一条问题记录。"""
    item: dict[str, Any] = {
        "line": lineno,
        "type": kind,
        "severity": severity,
        "message": message,
    }

    if end_lineno and end_lineno != lineno:
        item["end_line"] = end_lineno

    snippet = grab_snippet(lines, lineno, end_lineno)
    if snippet:
        item["snippet"] = snippet

    return item


# ======================================================================
# 检查器
# ======================================================================


class IssueCollector(ast.NodeVisitor):
    """遍历语法树,把发现的问题收进 issues。"""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines
        self.issues: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 规则一、二:异常处理

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is None:
            self._flag_bare_except(node)
        elif self._is_plain_exception(node.type):
            self._flag_broad_except(node)

        self.generic_visit(node)

    def _flag_bare_except(self, node: ast.ExceptHandler) -> None:
        self.issues.append(
            build_issue(
                self.lines,
                node.lineno,
                "bare_except",
                "medium",
                "这里用了裸 except。它会把 KeyboardInterrupt、SystemExit "
                "也一起吞掉,错误被静默掩盖,线上很难定位。",
            )
        )

    @staticmethod
    def _is_plain_exception(node: ast.expr) -> bool:
        """判断是不是直接写了 Exception。"""
        if not isinstance(node, ast.Name):
            return False
        return node.id == "Exception"

    def _flag_broad_except(self, node: ast.ExceptHandler) -> None:
        self.issues.append(
            build_issue(
                self.lines,
                node.lineno,
                "broad_except",
                "low",
                "捕获的是宽泛的 Exception,建议收窄到具体异常类型。",
            )
        )

    # ------------------------------------------------------------------
    # 规则三、四:函数调用

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func

        if isinstance(func, ast.Name):
            if func.id == "print":
                self._flag_print(node)
            elif func.id in RISKY_FUNCTIONS:
                self._flag_risky_name(node, func.id)

        elif isinstance(func, ast.Attribute):
            self._maybe_flag_builtins_call(node, func)

        self.generic_visit(node)

    def _flag_print(self, node: ast.Call) -> None:
        self.issues.append(
            build_issue(
                self.lines,
                node.lineno,
                "debug_print",
                "low",
                "这里有一个调试用的 print,正式代码里建议换成 logging。",
            )
        )

    def _flag_risky_name(self, node: ast.Call, name: str) -> None:
        self.issues.append(
            build_issue(
                self.lines,
                node.lineno,
                "dangerous_call",
                "high",
                f"调用了 {name}(),它可以执行任意代码,是明确的注入风险。",
            )
        )

    def _maybe_flag_builtins_call(self, node: ast.Call, func: ast.Attribute) -> None:
        """看看是不是 builtins.eval(...) 这种绕路写法。"""
        if func.attr not in RISKY_FUNCTIONS:
            return

        owner = func.value
        if not isinstance(owner, ast.Name):
            return

        if owner.id != "builtins":
            return

        self.issues.append(
            build_issue(
                self.lines,
                node.lineno,
                "dangerous_call",
                "high",
                f"通过 builtins 调用了 {func.attr}(),同样是执行任意代码的风险。",
            )
        )

    # ------------------------------------------------------------------
    # 规则五:函数体太长

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_length(node)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_length(node)
        self.generic_visit(node)

    def _check_length(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        end = getattr(node, "end_lineno", None)
        if end is None:
            return

        span = end - node.lineno + 1
        if span <= FUNCTION_LINE_LIMIT:
            return

        self.issues.append(
            build_issue(
                self.lines,
                node.lineno,
                "long_function",
                "medium",
                f"函数 {node.name}() 有 {span} 行,超过 {FUNCTION_LINE_LIMIT} 行的建议上限,"
                "职责可能不止一个,考虑拆开。",
                end_lineno=end,
            )
        )

    # ------------------------------------------------------------------
    # 规则六:控制流嵌套太深

    def visit_If(self, node: ast.If) -> None:
        self._check_nesting(node)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._check_nesting(node)
        self.generic_visit(node)

    def visit_While(self, node: ast.While) -> None:
        self._check_nesting(node)
        self.generic_visit(node)

    def visit_With(self, node: ast.With) -> None:
        self._check_nesting(node)
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        self._check_nesting(node)
        self.generic_visit(node)

    def _check_nesting(self, node: ast.AST) -> None:
        depth = measure_depth(node)
        if depth <= NESTING_LIMIT:
            return

        self.issues.append(
            build_issue(
                self.lines,
                node.lineno,
                "deep_nesting",
                "medium",
                f"控制流嵌套到了 {depth} 层,超过建议的 {NESTING_LIMIT} 层,读起来费劲。"
                "可以用提前返回(guard clause)或者把内层提成单独的函数。",
            )
        )


def measure_depth(node: ast.AST) -> int:
    """量一个节点往下最多套了几层控制流。

    这里只沿着嵌套语句往下走,而且取最深的那条分支。
    如果不这么做,四个平级的 if 会被算成四层嵌套 —— 那是错的。
    """
    deepest = 0

    for child in ast.iter_child_nodes(node):
        if not isinstance(child, NESTING_TYPES):
            continue
        deepest = max(deepest, measure_depth(child))

    return deepest + 1


# ======================================================================
# 分析入口
# ======================================================================


def analyze_source(source: str, filename: str = "<unknown>") -> dict[str, Any]:
    """分析一段源码。语法错误也会变成一条 issue 返回,不抛异常。"""
    lines = source.splitlines()

    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError as exc:
        return syntax_error_result(lines, filename, exc)

    collector = IssueCollector(lines)
    collector.visit(tree)

    issues = sort_and_cap(collector.issues)

    return {
        "syntax_ok": True,
        "issue_count": len(issues),
        "issues": issues,
        "summary": count_by_severity(issues),
    }


def syntax_error_result(
    lines: list[str],
    filename: str,
    exc: SyntaxError,
) -> dict[str, Any]:
    """把语法错误包成标准的结果结构。

    这在业务上不算「失败」,而是审查对象本身有问题,所以照样给一条 issue。
    """
    issue = {
        "line": exc.lineno,
        "type": "syntax_error",
        "severity": "high",
        "message": f"文件有语法错误:{exc.msg}({filename} 第 {exc.lineno} 行)",
    }

    snippet = grab_snippet(lines, exc.lineno)
    if snippet:
        issue["snippet"] = snippet

    # 语法都没过,后面所有检查都没意义,直接返回这一条
    return {
        "syntax_ok": False,
        "issue_count": 1,
        "issues": [issue],
        "summary": {"high": 1, "medium": 0, "low": 0},
    }


def sort_and_cap(issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按行号排一下,再截到上限。"""
    ordered = sorted(issues, key=_sort_key)

    if len(ordered) > ISSUE_LIMIT:
        return ordered[:ISSUE_LIMIT]

    return ordered


def _sort_key(item: dict[str, Any]) -> tuple[int, str]:
    line = item.get("line")
    if line is None:
        line = 0
    return (line, str(item.get("type", "")))


def count_by_severity(issues: list[dict[str, Any]]) -> dict[str, int]:
    """数一数各级别各有多少条。"""
    counter = {"high": 0, "medium": 0, "low": 0}

    for item in issues:
        level = item.get("severity")
        if level in counter:
            counter[level] += 1

    return counter


# ======================================================================
# 对外工具
# ======================================================================


class AstAnalyzerTool(Tool):
    name = "ast_analyzer"

    description = (
        "用 Python 标准库 AST 对指定 Python 文件做确定性静态分析，返回结构化问题列表，"
        "每条包含行号、类型、严重等级（high/medium/low）、说明与源码片段。"
        "检查项：裸 except、过宽的 Exception 捕获、调试 print、危险函数调用"
        "（eval/exec 等）、函数过长、控制流嵌套过深、语法错误。"
        "审查代码时应先调用本工具获取确定性问题，再结合 read_file 的完整代码给出解释与建议。"
    )

    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "待分析的 Python 文件路径，相对项目根目录，例如 examples/bad_code.py"
                ),
            }
        },
        "required": ["path"],
    }

    def run(self, path: str, **_: Any) -> dict[str, Any]:
        target = resolve_safe(path)
        self._reject_bad_target(target, path)

        source, encoding = read_text_with_fallback(target)
        result = analyze_source(source, filename=str(target))

        result["path"] = self._display_path(target)
        result["encoding"] = encoding
        result["total_lines"] = len(source.splitlines())

        return result

    @staticmethod
    def _reject_bad_target(target: Path, shown: str) -> None:
        if not target.exists():
            raise ToolError(
                f"File not found: {shown}",
                "确认路径;不确定的话先用 list_dir 看看目录里有什么",
            )

        if target.is_dir():
            raise ToolError(
                f"{shown} 是个目录,ast_analyzer 只吃单个文件",
                "指定目录下的某个 .py 文件",
            )

        if target.suffix != ".py":
            suffix = target.suffix or "无"
            raise ToolError(
                f"{shown} 不是 Python 文件(扩展名 {suffix})",
                "ast_analyzer 只分析 .py 文件",
            )

        limit = config_module.CONFIG.max_file_bytes
        size = target.stat().st_size
        if size > limit:
            raise ToolError(
                f"文件太大({size / 1024:.1f} KB),超过了分析上限 {limit / 1024:.0f} KB",
                "把文件拆小一点,或者换个小目标",
            )

    @staticmethod
    def _display_path(target: Path) -> str:
        try:
            return str(target.relative_to(config_module.CONFIG.workspace_root))
        except ValueError:
            return str(target)


def analyze_file(path: str | Path) -> dict[str, Any]:
    """直接按路径分析。

    这个函数故意不做工作目录校验,方便测试拿临时目录里的文件来跑。
    真正给模型用的是 AstAnalyzerTool,那条路会校验。
    """
    file_path = Path(path)
    source, _ = read_text_with_fallback(file_path)
    return analyze_source(source, filename=str(file_path))
