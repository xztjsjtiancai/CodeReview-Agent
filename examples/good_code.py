"""正面示例：与 bad_code.py 功能相近但写法规范的实现。

用途：演示对比。「审查规范代码时 Agent 不应该硬凑问题」，
这是证明审查结果可信、而非随机生成的关键证据。

预期结果：0 条 AST 问题（或仅极少数 low 级别提示）。

对应演示命令：/review examples/good_code.py
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MAX_SAFE_DIVISOR_CHECK = 0


def safe_divide(numerator: float, denominator: float) -> float | None:
    """安全除法：显式处理除零，不使用裸 except。

    Args:
        numerator: 被除数。
        denominator: 除数。

    Returns:
        商；当除数为 0 时返回 None。
    """
    if denominator == MAX_SAFE_DIVISOR_CHECK:
        logger.warning("除数为 0，返回 None：numerator=%s", numerator)
        return None
    return numerator / denominator


def pick_larger(a: int, b: int) -> int | None:
    """返回两个正数中较大的偶数。

    使用提前返回（guard clause）替代多层嵌套，逻辑一目了然。

    Args:
        a: 第一个整数。
        b: 第二个整数。

    Returns:
        满足条件的整数；没有满足条件的值时返回 None。
    """
    if a <= 0 or b <= 0:
        return None
    if a <= b:
        return None
    if a % 2 != 0:
        return None
    return a


def parse_number_literal(text: str) -> int | None:
    """把字符串解析为整数，不使用 eval。

    Args:
        text: 待解析的文本。

    Returns:
        解析出的整数；非法输入返回 None。
    """
    try:
        return int(text)
    except ValueError:
        logger.info("输入不是合法整数：%s", text)
        return None
