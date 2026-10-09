"""真实端到端验证：用真实 DeepSeek API 跑完整 Agent 流程。

这是《项目构建指导文档》阶段十三的验收脚本，覆盖 mock 测试无法验证的部分：
真实模型的工具选择、多轮推理、报告质量、Token 消耗。

用法：
    python tools_dev/real_check.py            # 审查 bad_code.py
    python tools_dev/real_check.py --path examples/good_code.py
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from console_compat import setup_console  # noqa: E402


setup_console()

from agent.agent import Agent  # noqa: E402
from agent.errors import AgentError  # noqa: E402
from agent.memory import Memory  # noqa: E402
from agent.prompts import REVIEW_TEMPLATE  # noqa: E402
from agent.renderer import Renderer  # noqa: E402
from config import CONFIG  # noqa: E402
from config import is_placeholder_key  # noqa: E402
from llm.client import LLMClient  # noqa: E402
from tools import build_default_registry  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default="examples/bad_code.py")
    parser.add_argument("--follow-up", default="", help="追加一轮追问，验证上下文记忆")
    args = parser.parse_args()

    if is_placeholder_key(CONFIG.api_key):
        print("[错误] 未配置有效的 DEEPSEEK_API_KEY")
        return 1

    renderer = Renderer(CONFIG)

    def on_retry(attempt: int, total: int, reason: str, delay: float) -> None:
        renderer.warn(f"LLM 请求失败（第 {attempt}/{total} 次）：{reason} → {delay:.1f}s 后重试")

    client = LLMClient(CONFIG, on_retry=on_retry)
    registry = build_default_registry()
    memory = Memory(max_context_chars=CONFIG.max_context_chars)

    tool_calls: list[str] = []

    def on_event(event: dict) -> None:
        if event.get("type") == "tool_start":
            tool_calls.append(event["name"])
        renderer.render_event(event)

    def make_agent() -> Agent:
        return Agent(
            client=client,
            registry=registry,
            memory=memory,
            max_steps=CONFIG.max_steps,
            on_event=on_event,
        )

    prompt = REVIEW_TEMPLATE.format(path=args.path)
    print(f"You: {prompt}\n")

    started = time.perf_counter()
    try:
        answer = make_agent().run(prompt)
    except AgentError as exc:
        renderer.error(exc.message, exc.hint)
        return 1
    elapsed = time.perf_counter() - started

    renderer.render_final(answer)

    print(f"[1] 审查完成，用时 {elapsed:.1f}s")
    print(f"[2] Agent 调用的工具序列：{tool_calls}")
    print(f"[3] 报告长度：{len(answer)} 字符")

    used_read = "read_file" in tool_calls
    used_ast = "ast_analyzer" in tool_calls
    extra_explore = [name for name in tool_calls if name in {"list_dir", "search_code"}]

    print(f"[4] 是否读取了文件：{'是' if used_read else '否'}")
    print(f"[5] 是否做了静态分析：{'是' if used_ast else '否'}")
    print(f"[6] 是否有多余探查（list_dir / search_code）：{extra_explore or '无'}")
    # 刻意不断言先后顺序：系统提示同时要求「先读后评」和「先静态后语义」，
    # 模型怎么取舍都合理，只要两个工具都用到、且没有多余探查即可。
    print(f"[7] 工具调用序列：{tool_calls}")

    if args.follow_up:
        print(f"\nYou: {args.follow_up}\n")
        started = time.perf_counter()
        follow = make_agent().run(args.follow_up)
        elapsed = time.perf_counter() - started
        renderer.render_final(follow)
        print(f"[8] 追问完成，用时 {elapsed:.1f}s，长度 {len(follow)} 字符")

        user_turns = sum(
            1
            for message in memory.messages
            if isinstance(message, dict) and message.get("role") == "user"
        )
        print(f"[9] 记忆中的 user 轮次：{user_turns}（2 表示首轮请求仍在上下文中）")

    print("\n== 结论 ==")
    if used_read and used_ast and not extra_explore:
        print("真实端到端验证通过：需要的两个工具都调用了，且没有多余的工具调用。")
        return 0
    if used_read and used_ast:
        print("基本通过：文件读取与静态分析均已完成，但存在多余的工具调用（见 [6]）。")
        return 0
    print("警告：模型未完整执行预期的工具序列，请检查 System Prompt 或工具描述。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
