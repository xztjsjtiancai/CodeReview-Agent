"""CLI 命令层测试。

`main.py` 的命令解析与分发此前没有测试覆盖，而「输入 → 命令 → Agent」之间
最容易出现低级错误（命令名拼错、参数丢失、`/clear` 没真的清空）。

这里通过替换 ``main`` 模块里的依赖（LLM 客户端、工具注册表）并用 stdin 喂入
模拟输入，完整走一遍 REPL 循环，因此覆盖了真实的命令分发路径。

全程不需要 API Key。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.agent import Agent
from agent.renderer import APP_NAME
import main as main_module


class _FakeMessage:
    def __init__(self, content: str) -> None:
        self.role = "assistant"
        self.content = content
        self.tool_calls = None
        self.reasoning_content = None


class _FakeClient:
    """记录每次请求的完整消息，用于验证 /clear 是否真的清空了上下文。"""

    def __init__(self) -> None:
        self.requests: list[list[Any]] = []

    def chat(self, messages: list[Any], tools: list[dict[str, Any]] | None = None):
        # 快照里保留 content：Agent 会把消息对象原样入历史（为了 reasoning_content），
        # 断言时需要能看到内容，否则无法验证「上一轮回答进入了下一轮上下文」。
        self.requests.append(
            [
                {
                    "role": m.get("role") if isinstance(m, dict) else m.role,
                    "content": m.get("content") if isinstance(m, dict) else m.content,
                }
                for m in messages
            ]
        )
        return _FakeMessage(f"回复第 {len(self.requests)} 次")


class _FakeRegistry:
    """只提供 CLI 需要的接口，不引入真实工具。"""

    def schemas(self) -> list[dict[str, Any]]:
        return []

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "read_file",
                "description": "读取文件",
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "路径"}},
                    "required": ["path"],
                },
            }
        ]

    def names(self) -> list[str]:
        return ["read_file"]


@pytest.fixture()
def cli(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """准备好被替换依赖的 main 模块，返回 (运行函数, 假客户端, 创建的 Agent)。

    注意必须替换 ``sys.argv``：``main()`` 会调用 argparse 解析命令行，
    否则 pytest 自己的参数（如 `tests/test_cli.py -x`）会被当成未知选项。
    """
    monkeypatch.setattr(main_module.sys, "argv", ["main.py"])
    monkeypatch.setattr(
        main_module,
        "CONFIG",
        main_module.dataclasses.replace(
            main_module.CONFIG, api_key="sk-test-key-for-unit-tests", ascii_only=True
        ),
    )
    fake_client = _FakeClient()
    monkeypatch.setattr(main_module, "LLMClient", lambda *a, **k: fake_client)
    monkeypatch.setattr(main_module, "build_default_registry", lambda: _FakeRegistry())

    created: list[Agent] = []
    original_agent = main_module.Agent

    def spy_agent(**kwargs):
        agent = original_agent(**kwargs)
        created.append(agent)
        return agent

    monkeypatch.setattr(main_module, "Agent", spy_agent)

    def run(lines: list[str]) -> tuple[int, str]:
        monkeypatch.setattr("builtins.input", lambda prompt="": lines.pop(0))
        code = main_module.main()
        return code, capsys.readouterr().out

    return run, fake_client, created


# ---------------------------------------------------------------------------
# 基本流程
# ---------------------------------------------------------------------------


def test_exit_command_terminates_with_zero(cli):
    run, _, _ = cli
    code, output = run(["/exit"])
    assert code == 0
    assert "Goodbye" in output


def test_quit_alias_also_exits(cli):
    run, _, _ = cli
    code, _ = run(["/quit"])
    assert code == 0


def test_eof_exits_gracefully(cli, monkeypatch: pytest.MonkeyPatch):
    run, _, _ = cli

    def raise_eof(prompt: str = ""):
        raise EOFError

    monkeypatch.setattr("builtins.input", raise_eof)
    assert main_module.main() == 0


def test_blank_input_is_ignored(cli):
    run, fake_client, _ = cli
    code, _ = run(["", "   ", "/exit"])
    assert code == 0
    assert fake_client.requests == [], "空输入不应触发 LLM 调用"


def test_help_lists_all_commands(cli):
    run, _, _ = cli
    _, output = run(["/help", "/exit"])
    for command in ("/help", "/review", "/tools", "/history", "/clear", "/exit"):
        assert command in output


def test_tools_command_prints_registered_tool(cli):
    run, _, _ = cli
    _, output = run(["/tools", "/exit"])
    assert "read_file" in output
    assert "读取文件" in output


def test_unknown_command_warns_without_calling_llm(cli):
    run, fake_client, _ = cli
    _, output = run(["/nope", "/exit"])
    assert "未知命令" in output
    assert fake_client.requests == []


# ---------------------------------------------------------------------------
# /review
# ---------------------------------------------------------------------------


def test_review_without_path_warns_and_skips_agent(cli):
    run, fake_client, created = cli
    _, output = run(["/review", "/exit"])
    assert "用法" in output
    assert created == [], "缺少路径时不应创建 Agent"
    assert fake_client.requests == []


def test_review_rewrites_to_natural_language_prompt(cli):
    run, fake_client, _ = cli
    _, output = run(["/review examples/bad_code.py", "/exit"])
    # 最终回答由调用方渲染成报告面板，Agent 不再重复抛 assistant 事件。
    # 断言引用品牌常量而不是写死标题，改品牌名时不至于又要改测试。
    assert APP_NAME.upper() in output
    assert "REVIEW REPORT" in output
    assert len(fake_client.requests) == 1

    user_messages = [m["content"] for m in fake_client.requests[0] if m.get("role") == "user"]
    assert user_messages, "应把改写后的 prompt 作为 user 消息发送"
    assert "审查" in user_messages[-1]
    assert "examples/bad_code.py" in user_messages[-1]


def test_review_via_natural_language_uses_same_loop(cli):
    """/review 只是语法糖：自然语言输入走同一条链路。"""
    run, fake_client, _ = cli
    _, output = run(["/review examples/bad_code.py", "/exit"])
    assert APP_NAME.upper() in output
    assert fake_client.requests, "自然语言命令应触发 LLM 调用"


# ---------------------------------------------------------------------------
# 记忆与 /clear
# ---------------------------------------------------------------------------


def test_conversation_history_accumulates_across_turns(cli):
    run, fake_client, _ = cli
    run(["第一问", "第二问", "/exit"])
    assert len(fake_client.requests) == 2
    # 第二轮请求应带上第一轮的对话
    second = fake_client.requests[1]
    assert any(m.get("content") == "第一问" for m in second)
    assert any(m.get("content") == "回复第 1 次" for m in second)


def test_clear_drops_context_but_keeps_system_prompt(cli):
    run, fake_client, _ = cli
    _, output = run(["第一问", "/clear", "第二问", "/exit"])

    assert "上下文已清空" in output
    assert len(fake_client.requests) == 2

    after_clear = fake_client.requests[1]
    roles = [m.get("role") for m in after_clear]
    contents = [m.get("content") for m in after_clear]

    assert roles[0] == "system", "System Prompt 必须保留"
    assert "第一问" not in contents, "/clear 后不应再带上历史对话"
    assert "回复第 1 次" not in contents, "/clear 后不应再带上历史回答"


def test_history_command_shows_trail(cli):
    run, _, _ = cli
    _, output = run(["审查一下", "/history", "/exit"])
    assert "会话记录" in output
    assert "审查一下" in output


def test_history_is_empty_before_any_dialogue(cli):
    run, _, _ = cli
    _, output = run(["/history", "/exit"])
    assert "还没有记录" in output


# ---------------------------------------------------------------------------
# Agent 重建与状态复用
# ---------------------------------------------------------------------------


def test_agent_is_rebuilt_per_turn_but_memory_is_shared(cli):
    run, _, created = cli
    run(["第一问", "第二问", "/exit"])
    assert len(created) == 2, "每轮重建 Agent，以便事件回调始终指向最新渲染层"
    assert created[0].memory is created[1].memory, "记忆必须在多轮之间共享"
    assert created[0].registry is created[1].registry, "工具注册表应复用"


def test_agent_receives_max_steps_from_config(cli):
    run, _, created = cli
    run(["问一句", "/exit"])
    assert created[0].max_steps == main_module.CONFIG.max_steps


# ---------------------------------------------------------------------------
# 命令解析单元测试
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_command", "expected_argument"),
    [
        ("/help", "help", ""),
        ("/HELP", "help", ""),
        ("  /help  ", "help", ""),
        ("/review a.py", "review", "a.py"),
        ("/review   a b.py  ", "review", "a b.py"),
        ("/exit", "exit", ""),
        ("/quit", "exit", ""),
        ("/q", "exit", ""),
        ("自然语言提问", "", "自然语言提问"),
        ("带 / 的路径 a/b.py", "", "带 / 的路径 a/b.py"),
    ],
)
def test_parse_command(raw, expected_command, expected_argument):
    command, argument = main_module.parse_command(raw)
    assert command == expected_command
    assert argument == expected_argument


@pytest.mark.parametrize(
    ("key", "is_placeholder"),
    [
        ("", True),
        ("short", True),
        ("<在这里粘贴你的-DeepSeek-API-Key>", True),
        ("sk-your-key-here", True),
        ("sk-abcdefghijklmnopqrstuvwxyz", False),
    ],
)
def test_is_placeholder_key(key, is_placeholder):
    """占位符判定统一实现在 config 里，避免 CLI 与自检脚本各写一份而漏判。"""
    from config import is_placeholder_key

    assert is_placeholder_key(key) is is_placeholder
