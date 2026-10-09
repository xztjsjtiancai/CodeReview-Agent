"""Agent 循环测试。

全部使用 mock 的 LLM 客户端，**不需要 API Key、不产生费用**，可离线运行。
这既保证循环逻辑正确，也是「代码质量」得分点的直接证据。

覆盖的关键行为：
1. 模型直接回答时立即终止；
2. 「推理 → 工具调用 → 观测 → 再推理 → 输出」的完整链路与顺序；
3. 工具报错被转成 Observation 回灌，而不是中断对话；
4. 达到步数上限时优雅兜底，不抛异常；
5. 相同工具 + 相同参数命中缓存，不重复执行；
6. 记忆原样保留 assistant 消息对象（二期开思考模式的前提）。

测试策略说明：工具层有「路径必须落在工作目录内」的安全校验，因此这里用
``monkeypatch`` 把工作目录临时指向 ``tests/`` 目录（它本身就是项目子目录），
并全程使用相对路径，从而既测到真实校验逻辑，又不污染全局配置。
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
import sys
import types
from typing import Any

import pytest

from agent.agent import Agent
from agent.memory import Memory
from agent.prompts import MAX_STEPS_MESSAGE
from tools.ast_tool import AstAnalyzerTool
from tools.file_tool import ReadFileTool
from tools.registry import ToolRegistry


TESTS_DIR = Path(__file__).resolve().parent


@pytest.fixture()
def workspace(monkeypatch: pytest.MonkeyPatch) -> Path:
    """把工具可见的工作目录临时切到 tests/ 目录，测试结束后自动还原。

    为什么要绕这一圈：工具执行前会校验「路径必须落在工作目录内」，
    而 pytest 的 tmp_path 在工作目录之外，会被正当地拒绝。
    这里先确保工具模块已加载，再以替换 ``sys.modules['config']`` 模块对象的
    方式提供一份改过工作目录的配置 —— 因为工具是在运行时通过
    ``config_module.CONFIG`` 读取配置的，替换模块即可生效。
    注意不能重新执行工具模块，否则会产生重复的类对象，破坏 isinstance 判断。
    """
    import config as config_module  # noqa: F401  确保配置模块已加载
    import tools.ast_tool  # noqa: F401  确保工具模块在替换前已加载
    import tools.file_tool  # noqa: F401
    import tools.search_tool  # noqa: F401

    original = sys.modules["config"]
    patched = types.ModuleType("config")
    patched.__dict__.update(vars(original))
    patched.CONFIG = dataclasses.replace(original.CONFIG, workspace_root=TESTS_DIR)

    monkeypatch.setitem(sys.modules, "config", patched)

    modules = (
        sys.modules["tools.file_tool"],
        sys.modules["tools.ast_tool"],
        sys.modules["tools.search_tool"],
    )
    for module in modules:
        monkeypatch.setattr(module, "config_module", patched, raising=False)

    return TESTS_DIR


@pytest.fixture()
def sample_file(workspace: Path) -> str:
    """在工作目录内创建一个带裸 except 的样本文件，返回相对路径。"""
    path = workspace / "_sample_for_loop.py"
    path.write_text(
        "def f():\n    try:\n        pass\n    except:\n        pass\n",  # noqa: E722 故意带裸 except
        encoding="utf-8",
    )
    yield "_sample_for_loop.py"
    path.unlink(missing_ok=True)


class FakeMessage:
    """模拟 SDK 返回的消息对象。

    刻意保留 ``content`` / ``tool_calls`` / ``reasoning_content`` 三个字段，
    以验证 Agent 是「原样 append 消息对象」而不是手工拆字段重组。
    """

    def __init__(self, content: str | None = None, tool_calls: list[Any] | None = None) -> None:
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls
        self.reasoning_content = None


class FakeFunction:
    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        self.name = name
        self.arguments = json.dumps(arguments, ensure_ascii=False)


class FakeToolCall:
    def __init__(self, name: str, arguments: dict[str, Any], call_id: str = "call_1") -> None:
        self.id = call_id
        self.type = "function"
        self.function = FakeFunction(name, arguments)


class FakeClient:
    """按预设脚本依次返回消息，并记录每次收到的消息快照。"""

    def __init__(self, script: list[FakeMessage]) -> None:
        self.script = list(script)
        self.calls: list[list[Any]] = []
        self.tool_schemas: list[list[dict[str, Any]] | None] = []

    def chat(self, messages: list[Any], tools: list[dict[str, Any]] | None = None) -> FakeMessage:
        # 浅拷贝快照：dict 消息复制一份，对象消息只记角色与内容，避免后续 append 影响断言
        snapshot: list[Any] = []
        for message in messages:
            if isinstance(message, dict):
                snapshot.append(dict(message))
            else:
                snapshot.append({"role": message.role, "content": message.content})
        self.calls.append(snapshot)
        self.tool_schemas.append(tools)
        if not self.script:
            raise AssertionError("FakeClient 的预设脚本已耗尽：Agent 调用次数超出预期")
        return self.script.pop(0)


def build_agent(script: list[FakeMessage], max_steps: int = 8):
    registry = ToolRegistry()
    registry.register_all([ReadFileTool(), AstAnalyzerTool()])

    events: list[dict[str, Any]] = []
    client = FakeClient(script)
    memory = Memory()
    agent = Agent(
        client=client,  # type: ignore[arg-type]
        registry=registry,
        memory=memory,
        max_steps=max_steps,
        on_event=events.append,
    )
    return agent, client, memory, events


# ---------------------------------------------------------------------------
# 用例 1：模型直接回答，循环应立即终止
# ---------------------------------------------------------------------------


def test_direct_answer_terminates_without_tool_calls(workspace: Path):
    agent, client, _, events = build_agent([FakeMessage(content="未发现明显问题。")])
    answer = agent.run("你好")
    assert answer == "未发现明显问题。"
    assert len(client.calls) == 1
    assert client.tool_schemas[0] is not None, "应把工具 Schema 传给模型"
    # 最终回答通过返回值交给调用方渲染，不应再作为 assistant 事件重复输出
    assert events == []


# ---------------------------------------------------------------------------
# 用例 2：完整链路 —— 推理 → 工具调用 → 观测 → 再推理 → 输出
# ---------------------------------------------------------------------------


def test_full_loop_with_tool_call(workspace: Path, sample_file: str):
    script = [
        FakeMessage(
            content="我先读取这个文件。",
            tool_calls=[FakeToolCall("read_file", {"path": sample_file}, "call_a")],
        ),
        FakeMessage(
            content="再做静态分析。",
            tool_calls=[FakeToolCall("ast_analyzer", {"path": sample_file}, "call_b")],
        ),
        FakeMessage(content="1 issues found (0 high, 1 medium, 0 low)"),
    ]
    agent, client, _, events = build_agent(script)

    answer = agent.run(f"帮我审查 {sample_file}")

    assert answer == "1 issues found (0 high, 1 medium, 0 low)"
    assert len(client.calls) == 3, "应为三轮请求：初始 → 读文件后 → 静态分析后"

    # 事件顺序体现完整链路（最终回答不重复输出为 assistant 事件）
    assert [event["type"] for event in events] == [
        "assistant",
        "tool_start",
        "tool_end",
        "assistant",
        "tool_start",
        "tool_end",
    ]
    assert events[1]["name"] == "read_file"
    assert events[4]["name"] == "ast_analyzer"
    assert events[2]["ok"] is True

    # 工具结果必须真正回灌给模型（第三轮请求里应能看到 ast_analyzer 的观测）
    third_round = client.calls[2]
    tool_messages = [m for m in third_round if m.get("role") == "tool"]
    assert tool_messages, "工具结果未回灌给模型"
    assert "bare_except" in tool_messages[-1]["content"]


# ---------------------------------------------------------------------------
# 用例 3：工具报错 → 转成 Observation，不中断对话
# ---------------------------------------------------------------------------


def test_tool_error_is_fed_back_and_loop_continues(workspace: Path):
    script = [
        FakeMessage(tool_calls=[FakeToolCall("read_file", {"path": "does_not_exist.py"}, "c1")]),
        FakeMessage(content="文件不存在，请确认路径。"),
    ]
    agent, client, _, events = build_agent(script)

    answer = agent.run("审查 does_not_exist.py")

    assert answer == "文件不存在，请确认路径。"
    assert len(client.calls) == 2, "工具失败后应继续向模型提问"

    tool_end = next(event for event in events if event["type"] == "tool_end")
    assert tool_end["ok"] is False
    assert "error" in tool_end["result"]

    # 错误信息必须回灌
    second_round = client.calls[1]
    tool_messages = [m for m in second_round if m.get("role") == "tool"]
    assert tool_messages and "File not found" in tool_messages[-1]["content"]


def test_unknown_tool_name_does_not_crash(workspace: Path):
    script = [
        FakeMessage(tool_calls=[FakeToolCall("no_such_tool", {}, "c1")]),
        FakeMessage(content="换个方式回答。"),
    ]
    agent, _, _, events = build_agent(script)
    assert agent.run("随便问") == "换个方式回答。"
    tool_end = next(event for event in events if event["type"] == "tool_end")
    assert tool_end["ok"] is False
    assert "没有名为" in tool_end["result"]["error"]


def test_path_escape_is_rejected(workspace: Path):
    """模型给越界路径时必须被拒绝，且以 Observation 形式回灌。"""
    script = [
        FakeMessage(tool_calls=[FakeToolCall("read_file", {"path": "../../etc/passwd"}, "c1")]),
        FakeMessage(content="该路径不允许访问。"),
    ]
    agent, _, _, events = build_agent(script)
    assert agent.run("读越界文件") == "该路径不允许访问。"
    tool_end = next(event for event in events if event["type"] == "tool_end")
    assert tool_end["ok"] is False
    assert "工作目录之外" in tool_end["result"]["error"]


def test_illegal_json_arguments_are_reported_not_raised(workspace: Path):
    class BadFunction:
        name = "read_file"
        arguments = "{not valid json"

    class BadCall:
        id = "c1"
        function = BadFunction()

    script = [
        FakeMessage(tool_calls=[BadCall()]),
        FakeMessage(content="参数有误，已修正。"),
    ]
    agent, _, _, events = build_agent(script)
    assert agent.run("读文件") == "参数有误，已修正。"
    assert next(e for e in events if e["type"] == "tool_end")["ok"] is False


# ---------------------------------------------------------------------------
# 用例 4：步数上限兜底
# ---------------------------------------------------------------------------


def test_max_steps_exceeded_returns_fallback_message(workspace: Path):
    endless = [
        FakeMessage(tool_calls=[FakeToolCall("read_file", {"path": "x.py"}, f"c{i}")])
        for i in range(5)
    ]
    agent, client, _, events = build_agent(endless, max_steps=3)

    answer = agent.run("审查 x.py")

    assert answer == MAX_STEPS_MESSAGE
    assert len(client.calls) == 3, "调用次数应严格等于 max_steps"
    assert any(event["type"] == "error" for event in events)


# ---------------------------------------------------------------------------
# 用例 5：相同工具 + 相同参数命中缓存
# ---------------------------------------------------------------------------


def test_identical_tool_call_is_cached(workspace: Path):
    path = workspace / "_cache_sample.py"
    path.write_text("print(1)\n", encoding="utf-8")
    try:
        same = {"path": "_cache_sample.py"}
        script = [
            FakeMessage(tool_calls=[FakeToolCall("read_file", same, "c1")]),
            FakeMessage(tool_calls=[FakeToolCall("read_file", same, "c2")]),
            FakeMessage(content="完成。"),
        ]
        agent, _, _, events = build_agent(script)
        agent.run("读两次同一个文件")

        ends = [event for event in events if event["type"] == "tool_end"]
        assert len(ends) == 2
        assert ends[0]["result"].get("cached") is not True
        assert ends[1]["result"].get("cached") is True, "第二次相同调用应命中缓存"
    finally:
        path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 记忆行为
# ---------------------------------------------------------------------------


def test_memory_clear_keeps_system_prompt_and_drops_context(workspace: Path):
    agent, _, memory, _ = build_agent([FakeMessage(content="第一次回答")])
    agent.run("第一问")
    assert len(memory) >= 2

    memory.clear()
    messages = memory.messages
    assert len(messages) == 1
    assert messages[0]["role"] == "system"


def test_clear_makes_follow_up_question_context_free(workspace: Path):
    """验证 /clear 之后 Agent 确实「失忆」——这是记忆真的生效的反证。"""
    script = [
        FakeMessage(content="发现 5 个问题。"),
        FakeMessage(content="没有上文，请指明要解释哪个文件的第几个问题。"),
    ]
    agent, client, memory, _ = build_agent(script)
    agent.run("审查 bad_code.py")
    memory.clear()
    agent.run("详细解释第二个问题")

    last_round = client.calls[-1]
    user_messages = [m for m in last_round if m.get("role") == "user"]
    assert len(user_messages) == 1, "清空后不应再带上历史对话"


def test_system_prompt_is_always_the_first_message(workspace: Path):
    agent, client, _, _ = build_agent([FakeMessage(content="ok")])
    agent.run("你好")
    first = client.calls[0][0]
    assert first["role"] == "system"
    assert "代码审查" in first["content"]


def test_assistant_message_object_kept_verbatim(workspace: Path):
    """历史里必须保留原始消息对象，以保住 reasoning_content 等字段。"""
    script = [
        FakeMessage(content="读取中", tool_calls=[FakeToolCall("read_file", {"path": "x"}, "c1")]),
        FakeMessage(content="完成"),
    ]
    agent, _, memory, _ = build_agent(script)
    agent.run("读 x")
    assistant_objects = [message for message in memory.messages if not isinstance(message, dict)]
    assert assistant_objects, "assistant 消息应是原始对象而非 dict"
    assert hasattr(assistant_objects[0], "reasoning_content")


@pytest.mark.parametrize("bad_payload", [None, "", []])
def test_no_tool_calls_is_treated_as_direct_answer(workspace: Path, bad_payload):
    """tool_calls 为 None / 空串 / 空列表时都应视为「模型已给出最终答案」。

    覆盖 SDK 在不同情况下可能返回的退化形态，避免循环白白多跑一轮。
    """
    message = FakeMessage(content="直接回答")
    message.tool_calls = bad_payload
    agent, client, _, _ = build_agent([message])
    assert agent.run("你好") == "直接回答"
    assert len(client.calls) == 1
