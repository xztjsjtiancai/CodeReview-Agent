"""记忆与工具注册表的单元测试。

记忆裁剪是「Agent 架构」得分点之一，也是最容易出错的地方：
裁剪必须成对处理 ``assistant(tool_calls)`` 与对应的 ``tool`` 消息，
否则会出现悬空的 tool_call_id，API 会直接报 400。这里把这条规则固化下来。
"""

from __future__ import annotations

import json

import pytest

from agent.errors import ToolArgumentError
from agent.errors import ToolError
from agent.errors import ToolNotFound
from agent.memory import Memory
from agent.memory import make_tool_message
from tools.base import Tool
from tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# 基础行为
# ---------------------------------------------------------------------------


def test_set_system_puts_prompt_first():
    memory = Memory()
    memory.set_system("你是审查员")
    memory.append({"role": "user", "content": "你好"})
    assert memory.messages[0]["role"] == "system"
    assert memory.messages[1]["content"] == "你好"


def test_set_system_is_idempotent():
    memory = Memory()
    memory.set_system("A")
    memory.set_system("A")
    assert len(memory) == 1


def test_clear_keeps_system_prompt_only():
    memory = Memory()
    memory.set_system("sys")
    memory.append({"role": "user", "content": "q"})
    memory.set_cached("k", {"v": 1})
    memory.record("user", content="q")

    memory.clear()

    assert len(memory) == 1
    assert memory.messages[0]["role"] == "system"
    assert memory.get_cached("k") is None
    assert memory.trail == []


def test_cache_key_is_stable_regardless_of_dict_order():
    a = Memory.cache_key("read_file", {"path": "a.py", "start_line": 1})
    b = Memory.cache_key("read_file", {"start_line": 1, "path": "a.py"})
    assert a == b


def test_cache_key_handles_raw_json_string():
    key = Memory.cache_key("read_file", '{"path": "a.py"}')
    assert key.startswith("read_file:")


def test_cache_roundtrip():
    memory = Memory()
    memory.set_cached("read_file:{'path': 'a.py'}", {"total_lines": 3})
    assert memory.get_cached("read_file:{'path': 'a.py'}") == {"total_lines": 3}


# ---------------------------------------------------------------------------
# 轨迹记录
# ---------------------------------------------------------------------------


def test_record_and_trail_returns_copy():
    memory = Memory()
    memory.record("user", content="hi")
    trail = memory.trail
    trail.append({"kind": "tampered"})
    assert len(memory.trail) == 1, "trail 应返回副本，外部修改不能污染内部状态"


# ---------------------------------------------------------------------------
# 超长裁剪
# ---------------------------------------------------------------------------


def test_no_trim_when_under_limit():
    memory = Memory(max_context_chars=10_000)
    for index in range(5):
        memory.append({"role": "user", "content": f"第{index}问"})
    assert memory.maybe_trim() is False
    assert len(memory) == 5


def test_trim_reduces_context_and_keeps_recent_turns():
    memory = Memory(max_context_chars=2_000, keep_recent_messages=10)
    memory.append({"role": "user", "content": "第一个问题"})
    for index in range(40):
        memory.append({"role": "assistant", "content": f"第{index}轮回答" + "A" * 200})
        memory.append(
            {"role": "tool", "tool_call_id": f"c{index}", "content": f"OBS{index}" + "T" * 200}
        )

    before = memory.estimated_chars()
    assert before > memory.max_context_chars
    assert memory.maybe_trim() is True

    after = memory.estimated_chars()
    assert after < before, "裁剪后上下文长度必须下降"
    assert after <= memory.max_context_chars, "裁剪后应回到阈值以内"

    contents = [message.get("content") for message in memory.messages if isinstance(message, dict)]
    assert any("第39轮回答" in str(c) for c in contents), "最近一轮必须保留原文"
    assert any("已被裁掉" in str(c) for c in contents), "应留下裁剪说明，让模型知道历史被截断"

    # 最旧一轮的内容应当已被移出上下文
    assert not any(str(c).startswith("OBS0") for c in contents), "最旧的观测应被丢弃"


def test_trim_keeps_newest_observations_verbatim():
    memory = Memory(max_context_chars=1_000, keep_recent_messages=4)
    memory.append({"role": "user", "content": "开始"})
    for index in range(20):
        memory.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{index}"}]})
        memory.append(
            {"role": "tool", "tool_call_id": f"c{index}", "content": f"OLD{index}" + "Z" * 300}
        )

    assert memory.maybe_trim() is True

    tool_messages = [
        message
        for message in memory.messages
        if isinstance(message, dict) and message.get("role") == "tool"
    ]
    # 最近的观测必须保持原文，模型才能基于最新事实推理
    assert any(m["content"].startswith("OLD19") for m in tool_messages)
    # 更早的观测应已被整体丢弃
    assert not any(m["content"].startswith("OLD0") for m in tool_messages)


def test_trim_preserves_tool_call_id_pairing():
    """裁剪后每条 tool 消息都必须能找到对应的 assistant tool_call。"""
    memory = Memory(max_context_chars=150, keep_recent_messages=3)
    memory.append({"role": "user", "content": "开始"})
    for index in range(20):
        memory.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": f"call_{index}", "type": "function"}],
            }
        )
        memory.append({"role": "tool", "tool_call_id": f"call_{index}", "content": "d" * 80})

    memory.maybe_trim()

    assistant_ids = set()
    for message in memory.messages:
        if isinstance(message, dict) and message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                assistant_ids.add(call["id"])
    tool_ids = {
        message["tool_call_id"]
        for message in memory.messages
        if isinstance(message, dict) and message.get("role") == "tool"
    }
    assert tool_ids <= assistant_ids, f"存在悬空的 tool 消息：{tool_ids - assistant_ids}"


def test_trim_is_skipped_when_few_messages():
    """消息本来就不多时，宁可超限也不要裁到只剩骨架。"""
    memory = Memory(max_context_chars=10, keep_recent_messages=12)
    memory.append({"role": "user", "content": "很长的内容" * 50})
    assert memory.maybe_trim() is False


def test_estimated_chars_counts_tool_calls():
    memory = Memory()
    memory.append({"role": "assistant", "content": "x", "tool_calls": [{"id": "abcdef"}]})
    assert memory.estimated_chars() > 10


def test_make_tool_message_is_json_serializable_and_readable():
    message = make_tool_message("call_1", {"path": "示例.py", "count": 2})
    assert message["role"] == "tool"
    assert message["tool_call_id"] == "call_1"
    payload = json.loads(message["content"])
    assert payload["path"] == "示例.py", "中文不应被转义为 \\uXXXX"


# ---------------------------------------------------------------------------
# 空 Memory 的布尔值陷阱（回归测试）
# ---------------------------------------------------------------------------


def test_empty_memory_is_falsy_which_is_a_known_trap():
    """空 Memory 的布尔值为 False —— 这正是 Agent 必须显式判 None 的原因。

    若 Agent 里写成 ``memory or Memory()``，调用方传入的空内存会被丢弃，
    导致每轮对话都从零开始。本用例把这一陷阱固定下来，防止回归。
    """
    assert bool(Memory()) is False

    from agent.agent import Agent

    class _NoopClient:
        def chat(self, messages, tools=None):  # pragma: no cover - 不会被调用
            raise AssertionError("不应调用")

    memory = Memory()
    agent = Agent(client=_NoopClient(), registry=ToolRegistry(), memory=memory)
    assert agent.memory is memory, "Agent 必须复用传入的 Memory 实例"


# ---------------------------------------------------------------------------
# 工具注册表
# ---------------------------------------------------------------------------


class _EchoTool(Tool):
    name = "echo"
    description = "回显输入"
    parameters = {
        "type": "object",
        "properties": {"text": {"type": "string"}, "times": {"type": "integer"}},
        "required": ["text"],
    }

    def run(self, text: str, times: int = 1, **_: object) -> dict:
        return {"echo": text * times}


class _BoomTool(Tool):
    name = "boom"
    description = "总是失败"
    parameters = {"type": "object", "properties": {}}

    def run(self, **_: object) -> dict:
        raise ToolError("工具内部炸了", "这是预期的失败")


def test_registry_register_and_schemas():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    schemas = registry.schemas()
    assert len(schemas) == 1
    assert schemas[0]["function"]["name"] == "echo"
    assert schemas[0]["type"] == "function"


def test_registry_dispatch_success():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    assert registry.dispatch("echo", {"text": "ab", "times": 2}) == {"echo": "abab"}


def test_registry_dispatch_accepts_json_string_arguments():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    assert registry.dispatch("echo", '{"text": "hi"}') == {"echo": "hi"}


def test_registry_unknown_tool_raises_tool_not_found():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    with pytest.raises(ToolNotFound) as exc:
        registry.dispatch("nope", {})
    assert "echo" in (exc.value.hint or ""), "提示里应列出可用工具，便于模型自我纠正"


def test_registry_missing_required_argument():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    with pytest.raises(ToolArgumentError) as exc:
        registry.dispatch("echo", {})
    assert "text" in exc.value.message


def test_registry_unknown_argument_is_rejected():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    with pytest.raises(ToolArgumentError):
        registry.dispatch("echo", {"text": "a", "typo": 1})


def test_registry_coerces_string_to_int():
    """模型常把数字写成字符串，这里应自动转换而不是报错。"""
    registry = ToolRegistry()
    registry.register(_EchoTool())
    assert registry.dispatch("echo", {"text": "a", "times": "3"}) == {"echo": "aaa"}


def test_registry_bad_json_reports_argument_error():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    with pytest.raises(ToolArgumentError) as exc:
        registry.dispatch("echo", "{not json")
    assert "JSON" in exc.value.message


def test_registry_non_object_json_is_rejected():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    with pytest.raises(ToolArgumentError):
        registry.dispatch("echo", "[1, 2]")


def test_registry_empty_arguments_becomes_empty_dict():
    registry = ToolRegistry()
    registry.register(_BoomTool())
    with pytest.raises(ToolError):
        registry.dispatch("boom", None)


def test_registry_propagates_tool_error_with_hint():
    registry = ToolRegistry()
    registry.register(_BoomTool())
    with pytest.raises(ToolError) as exc:
        registry.dispatch("boom", {})
    assert exc.value.hint == "这是预期的失败"


def test_registry_describe_for_cli():
    registry = ToolRegistry()
    registry.register_all([_EchoTool(), _BoomTool()])
    described = registry.describe()
    assert {item["name"] for item in described} == {"echo", "boom"}
    assert all("description" in item for item in described)


def test_registry_len_and_contains():
    registry = ToolRegistry()
    registry.register(_EchoTool())
    assert len(registry) == 1
    assert "echo" in registry
    assert "nope" not in registry
    assert registry.names() == ["echo"]


def test_register_requires_name():
    class _NoName(Tool):
        parameters = {"type": "object", "properties": {}}

        def run(self, **_: object) -> dict:
            return {}

    with pytest.raises(ValueError):
        ToolRegistry().register(_NoName())


def test_default_registry_contains_all_four_tools():
    from tools import build_default_registry

    registry = build_default_registry()
    assert set(registry.names()) == {"read_file", "list_dir", "ast_analyzer", "search_code"}
