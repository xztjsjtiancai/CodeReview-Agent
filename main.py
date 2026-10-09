"""程序入口:终端里的交互式界面。

这个文件只干「读输入、认命令、把事件丢给渲染层」这几件事。
任何分析、判断、编排都不写在这里 —— 那些属于 agent/agent.py。

启动方式:
    python main.py
    python main.py --model deepseek-v4-pro --max-steps 12
"""

from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path
import sys


# 允许直接 `python main.py` 运行,同时把 vendor/ 里的离线依赖也纳进来
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from console_compat import setup_console  # noqa: E402


setup_console()

from agent.agent import Agent  # noqa: E402
from agent.errors import AgentError  # noqa: E402
from agent.memory import Memory  # noqa: E402
from agent.prompts import REVIEW_TEMPLATE  # noqa: E402
from agent.renderer import PROMPT  # noqa: E402
from agent.renderer import Renderer  # noqa: E402
from config import CONFIG  # noqa: E402
from config import is_placeholder_key  # noqa: E402
from config import mask_secret  # noqa: E402
from llm.client import LLMClient  # noqa: E402
from tools import build_default_registry  # noqa: E402


# ======================================================================
# 命令行参数
# ======================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="xztcode",
        description="xztcode — 基于 LLM 与静态分析的终端代码审查 Agent",
    )
    parser.add_argument("--model", help="覆盖模型名，例如 deepseek-v4-pro")
    parser.add_argument("--max-steps", type=int, help="单轮对话的最大工具调用步数")
    parser.add_argument(
        "--ascii",
        action="store_true",
        help="纯 ASCII 输出模式（CMD 出现乱码时使用，等价于设置 CRA_ASCII=1）",
    )
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="打印生效配置后退出，用于排查环境问题",
    )
    return parser.parse_args()


def apply_overrides(args: argparse.Namespace):
    """命令行参数盖过 .env 里的值。"""
    changed = {}

    if args.model:
        changed["model"] = args.model

    if args.max_steps:
        changed["max_steps"] = args.max_steps

    if args.ascii:
        changed["ascii_only"] = True

    if not changed:
        return CONFIG

    return dataclasses.replace(CONFIG, **changed)


def print_config(config) -> None:
    """把当前生效的配置逐行打出来,排查环境问题时用。"""
    print(f"model          : {config.model}")
    print(f"base_url       : {config.base_url}")
    print(f"thinking       : {config.thinking}")
    print(f"max_steps      : {config.max_steps}")
    print(f"max_file_bytes : {config.max_file_bytes}")
    print(f"workspace_root : {config.workspace_root}")
    print(f"ascii_only     : {config.ascii_only}")
    print(f"api_key        : {mask_secret(config.api_key)}")


# ======================================================================
# 命令解析
# ======================================================================

CMD_HELP = "help"
CMD_REVIEW = "review"
CMD_TOOLS = "tools"
CMD_HISTORY = "history"
CMD_CLEAR = "clear"
CMD_EXIT = "exit"

# 允许的退出别名
EXIT_ALIASES = {"quit", "q", "exit"}


def parse_command(raw: str) -> tuple[str, str]:
    """把一行输入拆成 (命令, 参数)。

    不以 "/" 开头的东西都当作自然语言,原样返回。
    """
    text = raw.strip()

    if not text.startswith("/"):
        return "", raw

    head, _, rest = text[1:].partition(" ")
    word = head.strip().lower()

    if word in EXIT_ALIASES:
        word = CMD_EXIT

    return word, rest.strip()


# ======================================================================
# 主流程
# ======================================================================


def main() -> int:
    args = parse_args()
    config = apply_overrides(args)

    if args.show_config:
        print_config(config)
        return 0

    renderer = Renderer(config)

    if is_placeholder_key(config.api_key):
        renderer.error(
            "未配置有效的 DEEPSEEK_API_KEY（.env 里仍是占位内容）。",
            "打开 .env，把 DEEPSEEK_API_KEY 一行换成真实 Key："
            "https://platform.deepseek.com/api_keys",
        )
        return 1

    def on_retry(attempt: int, total: int, reason: str, delay: float) -> None:
        # 重试过程必须看得见,否则用户会以为程序卡死了
        renderer.warn(f"模型请求失败（第 {attempt}/{total} 次）：{reason} → {delay:.1f} 秒后重试")

    client = LLMClient(config, on_retry=on_retry)
    registry = build_default_registry()
    memory = Memory(max_context_chars=config.max_context_chars)

    def new_agent() -> Agent:
        """每轮重建 Agent,让事件回调始终指向当前渲染层。

        memory 和 registry 是跨轮复用的,所以上下文与工具缓存不会断。
        """
        return Agent(
            client=client,
            registry=registry,
            memory=memory,
            max_steps=config.max_steps,
            on_event=renderer.render_event,
        )

    renderer.banner()

    while True:
        try:
            raw = input(PROMPT)
        except EOFError:
            renderer.info("\n输入结束，已退出。")
            return 0
        except KeyboardInterrupt:
            renderer.info("\n已退出。")
            return 0

        if not raw.strip():
            continue

        command, argument = parse_command(raw)

        if command == CMD_EXIT:
            renderer.info("Goodbye!")
            return 0

        if command == CMD_HELP:
            renderer.help()
            continue

        if command == CMD_TOOLS:
            renderer.tools(registry.describe())
            continue

        if command == CMD_HISTORY:
            renderer.history(memory.trail)
            continue

        if command == CMD_CLEAR:
            memory.clear()
            renderer.info("上下文已清空。")
            continue

        if command == CMD_REVIEW:
            if not argument:
                renderer.warn("用法：/review <文件路径>，例如 /review examples/bad_code.py")
                continue
            # 命令本身没有特殊逻辑,只是把输入改写成一句固定的话
            prompt = REVIEW_TEMPLATE.format(path=argument)

        elif command:
            renderer.warn(f"未知命令 /{command}，输入 /help 查看可用命令。")
            continue

        else:
            prompt = raw

        try:
            answer = new_agent().run(prompt)
        except AgentError as exc:
            renderer.error(exc.message, exc.hint)
            continue
        except KeyboardInterrupt:
            renderer.warn("已中断本轮请求。")
            continue

        renderer.render_final(answer)


if __name__ == "__main__":
    raise SystemExit(main())
