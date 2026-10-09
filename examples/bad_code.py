"""反面示例：故意埋入多种典型代码问题，用于演示 CodeReview Agent。

AST 工具可稳定检出的确定性问题：
    L21  裸 except
    L24  调试用 print
    L26  控制流嵌套过深（4 层 if）
    L36  eval() 调用，可执行任意代码

需要 LLM 结合语义判断的问题：
    L20  a / b 未校验 b 是否为 0，可能抛 ZeroDivisionError
    L24  result 在异常分支下可能未定义即被使用（UnboundLocalError）
    L32  函数存在多条返回路径，返回值语义不清晰

对应演示命令：/review examples/bad_code.py
"""


def calculate(a, b):
    try:
        result = a / b
    except:  # noqa: E722 —— 故意保留裸 except：这是本示例要演示的问题代码
        pass

    print(result)

    if a > 0:
        if b > 0:
            if a > b:
                if a % 2 == 0:
                    return a

    return None


def parse_expression(expression):
    return eval(expression)
