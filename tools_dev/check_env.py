"""环境自检：验证依赖、配置与 LLM 连通性。

阶段二的验收脚本。分三步，逐步定位问题：

    python tools_dev/check_env.py            # 只查依赖与配置，不调用 API
    python tools_dev/check_env.py --call     # 额外发起一次真实 LLM 调用

在沙箱环境中需要把项目内的 vendor 目录加入 PYTHONPATH：

    set PYTHONPATH=%CD%\\vendor
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from console_compat import setup_console  # noqa: E402


setup_console()

OK = "[ OK ]"
FAIL = "[FAIL]"


def check_dependencies() -> bool:
    print("== 1. 依赖检查 ==")
    good = True
    for module, label in [
        ("openai", "OpenAI SDK（DeepSeek 兼容）"),
        ("dotenv", "python-dotenv"),
        ("rich", "rich"),
        ("httpx", "httpx"),
    ]:
        try:
            imported = __import__(module)
            version = getattr(imported, "__version__", "")
            if module == "rich":
                from importlib.metadata import version as meta_version

                version = meta_version("rich")
            print(f"  {OK} {label:<28} {version}")
        except ImportError as exc:
            good = False
            print(f"  {FAIL} {label:<28} 导入失败：{exc}")
    return good


def check_config() -> bool:
    print("\n== 2. 配置检查 ==")
    import config

    cfg = config.CONFIG
    print(f"  {OK} 模型        {cfg.model}")
    print(f"  {OK} base_url    {cfg.base_url}")
    print(f"  {OK} 思考模式    {cfg.thinking}（'disabled' 为预期值）")
    print(f"  {OK} 最大步数    {cfg.max_steps}")
    print(f"  {OK} 文件大小上限 {cfg.max_file_bytes} bytes")
    print(f"  {OK} 工作目录    {cfg.workspace_root}")

    if config.is_placeholder_key(cfg.api_key):
        print(f"  {FAIL} API Key     未设置或仍是占位内容")
        print("       ↳ 打开 .env，把 DEEPSEEK_API_KEY 换成真实 Key")
        print("       ↳ 获取地址：https://platform.deepseek.com/api_keys")
        return False
    print(f"  {OK} API Key     {config.mask_secret(cfg.api_key)}")
    return True


def check_llm_call() -> bool:
    print("\n== 3. LLM 真实调用 ==")
    from agent.errors import AgentError
    from config import CONFIG
    from llm.client import LLMClient

    def on_retry(attempt: int, total: int, reason: str, delay: float) -> None:
        print(f"  [重试 {attempt}/{total}] {reason} → {delay:.1f}s 后重试")

    client = LLMClient(CONFIG, on_retry=on_retry)
    messages = [
        {"role": "system", "content": "你是一个简洁的助手，只回答被问到的内容。"},
        {"role": "user", "content": "用一句话说明什么是代码审查，不要超过 30 个字。"},
    ]
    try:
        message = client.chat(messages)
    except AgentError as exc:
        print(f"  {FAIL} 调用失败：{exc}")
        return False

    content = (message.content or "").strip()
    print(f"  {OK} 模型回复：{content}")
    usage = getattr(message, "usage", None)
    if usage:
        print(f"       token 用量：{usage}")
    return bool(content)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--call", action="store_true", help="额外发起一次真实 LLM 调用")
    args = parser.parse_args()

    deps_ok = check_dependencies()
    config_ok = check_config()

    if not deps_ok:
        print("\n结论：依赖不完整，请先安装 requirements.txt")
        return 1
    if not config_ok:
        print("\n结论：配置不完整，无法进行 API 调用")
        return 1
    if not args.call:
        print("\n结论：依赖与配置均正常。加 --call 可验证真实 API 调用。")
        return 0

    if check_llm_call():
        print("\n结论：阶段二验收通过 —— LLM 链路已打通。")
        return 0
    print("\n结论：API 调用失败，请根据上方提示排查。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
