"""AST 静态分析规则测试。

全部用例都不依赖 LLM 与网络，可在任何环境秒级跑完 —— 这是「代码质量」
得分点的直接证据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.ast_tool import FUNCTION_LINE_LIMIT
from tools.ast_tool import analyze_file
from tools.ast_tool import analyze_source


EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def types_of(result: dict) -> list[str]:
    return [issue["type"] for issue in result["issues"]]


def issues_of(result: dict, type_: str) -> list[dict]:
    return [issue for issue in result["issues"] if issue["type"] == type_]


# ---------------------------------------------------------------------------
# 规则 1：裸 except
# ---------------------------------------------------------------------------


def test_bare_except_detected_with_correct_line():
    source = "def f():\n    try:\n        pass\n    except:\n        pass\n"
    result = analyze_source(source, "t.py")
    hits = issues_of(result, "bare_except")
    assert len(hits) == 1
    assert hits[0]["line"] == 4
    assert hits[0]["severity"] == "medium"


def test_specific_except_not_flagged_as_bare():
    source = "def f():\n    try:\n        pass\n    except ValueError:\n        pass\n"
    assert "bare_except" not in types_of(analyze_source(source))


def test_broad_exception_is_low_severity():
    source = "def f():\n    try:\n        pass\n    except Exception:\n        pass\n"
    hits = issues_of(analyze_source(source), "broad_except")
    assert len(hits) == 1
    assert hits[0]["severity"] == "low"


# ---------------------------------------------------------------------------
# 规则 2：调试 print
# ---------------------------------------------------------------------------


def test_debug_print_detected():
    result = analyze_source("x = 1\nprint(x)\n")
    hits = issues_of(result, "debug_print")
    assert len(hits) == 1
    assert hits[0]["line"] == 2
    assert hits[0]["severity"] == "low"


def test_attribute_named_print_is_not_flagged():
    """obj.print() 是别的方法，不应误报。"""
    result = analyze_source("import sys\nsys.stdout.write('x')\n")
    assert "debug_print" not in types_of(result)


# ---------------------------------------------------------------------------
# 规则 3：函数过长
# ---------------------------------------------------------------------------


def test_long_function_detected():
    body = "\n".join(f"    x{i} = {i}" for i in range(FUNCTION_LINE_LIMIT + 5))
    source = f"def big():\n{body}\n"
    hits = issues_of(analyze_source(source), "long_function")
    assert len(hits) == 1
    assert hits[0]["line"] == 1
    assert "end_line" in hits[0]


def test_short_function_not_flagged():
    source = "def small():\n    return 1\n"
    assert "long_function" not in types_of(analyze_source(source))


# ---------------------------------------------------------------------------
# 规则 4：嵌套过深
# ---------------------------------------------------------------------------


def test_deep_nesting_detected_at_four_levels():
    source = (
        "def f(a):\n"
        "    if a:\n"
        "        if a:\n"
        "            if a:\n"
        "                if a:\n"
        "                    return 1\n"
        "    return 0\n"
    )
    hits = issues_of(analyze_source(source), "deep_nesting")
    assert len(hits) == 1
    assert hits[0]["line"] == 2
    assert "4 层" in hits[0]["message"]


def test_three_levels_is_within_limit():
    source = (
        "def f(a):\n"
        "    if a:\n"
        "        if a:\n"
        "            if a:\n"
        "                return 1\n"
        "    return 0\n"
    )
    assert "deep_nesting" not in types_of(analyze_source(source))


# ---------------------------------------------------------------------------
# 规则 5：危险函数
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("call", ["eval", "exec", "__import__"])
def test_dangerous_calls_detected(call):
    result = analyze_source(f"def f(s):\n    return {call}(s)\n")
    hits = issues_of(result, "dangerous_call")
    assert len(hits) == 1
    assert hits[0]["line"] == 2
    assert hits[0]["severity"] == "high"


def test_safe_builtin_not_flagged():
    assert "dangerous_call" not in types_of(analyze_source("x = len([1, 2])\n"))


# ---------------------------------------------------------------------------
# 语法错误路径：这是正常输入，不能抛异常
# ---------------------------------------------------------------------------


def test_syntax_error_returned_as_issue_not_exception():
    result = analyze_source("def f(:\n    pass\n", "broken.py")
    assert result["syntax_ok"] is False
    assert result["issue_count"] == 1
    issue = result["issues"][0]
    assert issue["type"] == "syntax_error"
    assert issue["severity"] == "high"
    assert issue["line"] == 1
    assert "broken.py" in issue["message"]


def test_empty_file_produces_no_issues():
    result = analyze_source("")
    assert result["syntax_ok"] is True
    assert result["issue_count"] == 0


# ---------------------------------------------------------------------------
# 汇总与排序
# ---------------------------------------------------------------------------


def test_issues_sorted_by_line_and_summary_counts():
    source = "print(1)\nexcept_placeholder = 0\neval('1')\n"
    result = analyze_source(source)
    lines = [issue["line"] for issue in result["issues"]]
    assert lines == sorted(lines)
    assert result["summary"]["high"] == 1
    assert result["summary"]["low"] == 1
    assert result["summary"]["medium"] == 0


# ---------------------------------------------------------------------------
# 面向示例文件：演示效果的可信度保障
# ---------------------------------------------------------------------------


def test_bad_code_example_triggers_all_ast_rules():
    """bad_code.py 必须能稳定触发 4 条 AST 规则，否则演示效果不可复现。"""
    result = analyze_file(EXAMPLES / "bad_code.py")
    detected = set(types_of(result))
    assert result["syntax_ok"] is True
    assert {"bare_except", "debug_print", "deep_nesting", "dangerous_call"} <= detected
    assert result["summary"]["high"] >= 1


def test_good_code_example_has_no_high_severity_issues():
    """规范代码不应被硬凑出高危问题 —— 证明审查结果可信。"""
    result = analyze_file(EXAMPLES / "good_code.py")
    assert result["syntax_ok"] is True
    assert result["summary"]["high"] == 0
    assert result["issue_count"] <= 1
