"""离线演示：用脚本化的假 LLM 驱动真实 Agent，走通完整代码审查流程。

用途：在**不消耗 API 额度**的前提下复现终端演示效果，产出 README 所需的
示例输出，也便于在没有 API Key 的环境里验证渲染层。

真实性说明：Agent 循环、工具注册与派发、AST 分析、文件读取、记忆、
渲染层全部是生产代码；只有 LLM 的「回复内容」是预先写好的脚本。
这样既能稳定复现演示，又不需要联网。

用法：
    python tools_dev/offline_demo.py
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from console_compat import setup_console  # noqa: E402


setup_console()

import dataclasses  # noqa: E402

from agent.agent import Agent  # noqa: E402
from agent.memory import Memory  # noqa: E402
from agent.renderer import Renderer  # noqa: E402
from config import CONFIG  # noqa: E402
from tools import build_default_registry  # noqa: E402


TARGET = "examples/bad_code.py"


class ScriptedMessage:
    """模拟 SDK 的 assistant 消息对象。"""

    def __init__(self, content: str | None = None, tool_calls: list[Any] | None = None) -> None:
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None


class ScriptedFunction:
    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.name = name
        self.arguments = json.dumps(arguments, ensure_ascii=False)


class ScriptedToolCall:
    def __init__(self, name: str, arguments: dict[str, Any], call_id: str) -> None:
        self.id = call_id
        self.type = "function"
        self.function = ScriptedFunction(name, arguments)


def _call(name: str, arguments: dict[str, Any], call_id: str) -> ScriptedToolCall:
    return ScriptedToolCall(name, arguments, call_id)


_REVIEW_REPORT = """🔴 HIGH      L20
`a / b` 没有校验 b 是否为 0。当 b == 0 时抛出 ZeroDivisionError，而外层是裸 except，
异常被吞掉后 `result` 从未被赋值。
建议：除法前显式判断 `if b == 0: return None`，或捕获 `ZeroDivisionError` 并记录日志。

🟡 MEDIUM    L21
裸 except 会吞掉包括 KeyboardInterrupt、SystemExit 在内的所有异常，错误被静默掩盖。
建议：改为 `except ZeroDivisionError as exc:` 并 `logger.warning(...)`。

🟡 MEDIUM    L26
条件嵌套深度达到 4 层，可读性明显下降。
建议：用提前返回扁平化，例如 `if a <= 0: return None` 逐条排除。

🟢 LOW       L24
存在调试用的 print，且它打印的 `result` 在异常路径下可能是未定义变量，会抛 UnboundLocalError。
建议：改用 logging，或在函数开头初始化 `result = None`。

🔴 HIGH      L36
`eval(expression)` 会执行任意代码，是明确的注入风险。
建议：用 `ast.literal_eval` 或 `int()` 等受限解析替代。

5 issues found (2 high, 2 medium, 1 low)"""

_FOLLOW_UP = """第二个问题是 L21 的裸 except。

它的具体危害在于：`except:` 等价于 `except BaseException:`，不仅捕获
ZeroDivisionError，还会吞掉 KeyboardInterrupt（Ctrl+C 无法中断）和 SystemExit。
更糟的是这里的分支体是 `pass`，异常既不处理也不记录，于是 L20 的除零错误会
静默地让 `result` 保持未定义状态，直到 L24 的 print 才以 UnboundLocalError 的形式暴露，
排查时很难定位到真正的原因。

建议改为：

    try:
        result = a / b
    except ZeroDivisionError:
        logger.warning("除数为 0：a=%s, b=%s", a, b)
        return None"""


#: 一次典型的审查对话：读文件 → 静态分析 → 输出报告 → 追问
SCRIPT: dict[str, list[ScriptedMessage]] = {
    (
        f"请审查文件 {TARGET}。先读取文件内容，对 Python 文件同时运行静态分析，"
        "然后给出完整的代码审查报告。"
    ): [
        ScriptedMessage(
            content="我先读取这个文件。",
            tool_calls=[_call("read_file", {"path": TARGET}, "call_read")],
        ),
        ScriptedMessage(
            content="拿到源码了，现在做确定性静态分析。",
            tool_calls=[_call("ast_analyzer", {"path": TARGET}, "call_ast")],
        ),
        ScriptedMessage(content=_REVIEW_REPORT),
    ],
    "详细解释第二个问题": [
        ScriptedMessage(content=_FOLLOW_UP),
    ],
}


class ScriptedClient:
    """按预设脚本返回回复，并在脚本耗尽时复用最后一条，保证演示不中断。"""

    def __init__(self, script: dict[str, list[ScriptedMessage]]) -> None:
        self._script = script
        self._last_user: str | None = None
        self._queue: list[ScriptedMessage] = []
        self.calls = 0
        self.last_tools: list[dict[str, Any]] | None = None

    def chat(self, messages: list[Any], tools: list[dict[str, Any]] | None = None):
        self.calls += 1
        self.last_tools = tools

        user_inputs = [
            message["content"]
            for message in messages
            if isinstance(message, dict) and message.get("role") == "user"
        ]
        if user_inputs and user_inputs[-1] != self._last_user:
            self._last_user = user_inputs[-1]
            self._queue = list(self._script.pop(user_inputs[-1], []))

        if not self._queue:
            return ScriptedMessage(content="（脚本已耗尽）")
        return self._queue.pop(0)


def main() -> int:
    config = dataclasses.replace(CONFIG, ascii_only=False)
    renderer = Renderer(config)
    renderer.banner()

    client = ScriptedClient(SCRIPT)
    registry = build_default_registry()
    memory = Memory(max_context_chars=config.max_context_chars)

    def run(prompt: str) -> None:
        agent = Agent(
            client=client,  # type: ignore[arg-type]
            registry=registry,
            memory=memory,
            max_steps=config.max_steps,
            on_event=renderer.render_event,
        )
        renderer.render_final(agent.run(prompt))

    print("You: /review " + TARGET)
    run(
        f"请审查文件 {TARGET}。先读取文件内容，对 Python 文件同时运行静态分析，"
        "然后给出完整的代码审查报告。"
    )

    print("\nYou: 详细解释第二个问题")
    run("详细解释第二个问题")

    print(f"\n（本次演示共调用 LLM {client.calls} 次，全部为脚本化回复，未消耗 API 额度）")
    print(f"（传给模型的工具数量：{len(client.last_tools or [])}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
