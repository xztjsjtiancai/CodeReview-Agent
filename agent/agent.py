"""Agent 主循环:输入 → 推理 → 工具调用 → 输出。

这个文件是唯一同时认识「模型」和「工具」的地方。llm/client.py 不知道什么是工具,
tools/* 也不知道什么是模型,把它们编起来的活全在这里。

几个刻意的设计选择:
  * 终止有两条路 —— 模型主动给出答案,或者步数耗尽。后者不走异常,而是把
    已经拿到的信息还给用户。
  * 工具报错不当成崩溃处理 —— 转成 Observation 喂回去,模型下一轮往往能自己绕开。
  * 历史里存的是原始消息对象,不是拆开重拼的 dict。这样 reasoning_content
    之类的字段不会丢,以后要开思考模式不用改这里。
  * 每一步都对外发事件,界面层想怎么画都行,这个文件不碰 print。
"""

from __future__ import annotations

from collections.abc import Callable
import json
from typing import Any

from agent.errors import AgentError
from agent.errors import MaxStepsExceeded
from agent.memory import Memory
from agent.memory import content_of
from agent.memory import make_tool_message
from agent.prompts import MAX_STEPS_MESSAGE
from agent.prompts import SYSTEM_PROMPT
from llm.client import LLMClient
from tools.registry import ToolRegistry


# 界面层收到的事件形如 {"type": "tool_start", "name": ..., ...}
EventCallback = Callable[[dict[str, Any]], None]

# 单条工具结果回灌时的字符上限,防止一次调用就把上下文撑满
OBSERVATION_LIMIT = 20000


# ----------------------------------------------------------------------
# 参数与结果的小工具


def decode_arguments(name: str, raw: str) -> dict[str, Any]:
    """把模型吐出来的参数串变成 dict。

    这里不抛异常:解析失败就塞一个 __parse_error__ 进去,
    后面注册表校验参数时会自然报错,错误信息也能一起回灌给模型。
    """
    if not raw:
        return {}

    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"__parse_error__": f"{exc.msg};原始内容片段:{raw[:200]}"}

    if isinstance(decoded, dict):
        return decoded

    return {"__parse_error__": f"参数应该是 JSON 对象,实际收到 {type(decoded).__name__}"}


def shrink_result(result: dict[str, Any]) -> dict[str, Any]:
    """结果太长就截断。

    宁可留一段预览,也不要让一条 Observation 直接把上下文顶爆。
    """
    text = json.dumps(result, ensure_ascii=False)
    if len(text) <= OBSERVATION_LIMIT:
        return result

    return {
        "truncated": True,
        "note": f"结果有 {len(text)} 字符,超过上限,已截断",
        "preview": text[:OBSERVATION_LIMIT],
    }


def read_tool_calls(message: Any) -> list[Any]:
    """从消息里取工具调用列表。取不到就当成空列表。"""
    calls = getattr(message, "tool_calls", None)
    if not calls:
        return []
    return list(calls)


# ----------------------------------------------------------------------
# Agent


class Agent:
    """代码审查 Agent。

    四个依赖都从构造函数传进来,而不是在里面自己 new。
    这样做有两个好处:单测可以把模型换成假的;工具注册表也能在
    多轮对话之间复用。
    """

    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        memory: Memory | None = None,
        max_steps: int = 8,
        on_event: EventCallback | None = None,
    ) -> None:
        self.client = client
        self.registry = registry
        self.max_steps = max_steps
        self.on_event = on_event

        # 这里千万不能写 memory or Memory()。
        # Memory 定义了 __len__,没有内容时它是假值,那么调用方传进来的
        # 内存对象会被整个丢掉,每轮对话都从零开始 —— 多轮追问和 /clear 就废了。
        if memory is None:
            self.memory = Memory()
        else:
            self.memory = memory

        self.memory.set_system(SYSTEM_PROMPT)

    # ------------------------------------------------------------------
    # 给外部用的入口

    def run(self, user_input: str) -> str:
        """跑完一轮完整对话,把最终回答交回去。"""
        self.memory.append({"role": "user", "content": user_input})
        self.memory.record("user", content=user_input)

        try:
            return self._loop()
        except MaxStepsExceeded:
            # 不往上抛。用户看到「我查到这儿了」比看到一串 traceback 有用得多。
            self._emit({"type": "error", "message": MAX_STEPS_MESSAGE})
            return MAX_STEPS_MESSAGE

    # ------------------------------------------------------------------
    # 主循环本体

    def _loop(self) -> str:
        step = 1
        while step <= self.max_steps:
            self.memory.maybe_trim()

            message = self.client.chat(
                self.memory.messages,
                tools=self.registry.schemas(),
            )
            self.memory.append(message)

            text = content_of(message)
            calls = read_tool_calls(message)

            if not calls:
                # 模型不再要工具,那这条就是最终答案。
                # 注意这里不对外发 assistant 事件 —— 最终答案由调用方统一渲染,
                # 否则同一段文字会先被当中间话术打印一次,再被包进报告面板一次。
                return text

            if text:
                self._emit({"type": "assistant", "step": step, "content": text})
                self.memory.record("assistant", step=step, content=text)

            for call in calls:
                self._handle_one_call(call, step)

            step += 1

        raise MaxStepsExceeded(
            f"转了 {self.max_steps} 步还是没收敛",
            "把范围缩小一点(比如只指定单个文件),或者调大 CRA_MAX_STEPS 再试",
        )

    # ------------------------------------------------------------------
    # 单个工具调用

    def _handle_one_call(self, call: Any, step: int) -> None:
        """处理一次工具调用:解析参数、执行、把结果写回历史。"""
        function = getattr(call, "function", None)
        name = getattr(function, "name", "") or ""
        raw_args = getattr(function, "arguments", "") or ""
        call_id = getattr(call, "id", "")

        arguments = decode_arguments(name, raw_args)

        self._emit(
            {
                "type": "tool_start",
                "step": step,
                "name": name,
                "arguments": arguments,
            }
        )

        result, ok = self._invoke_tool(name, arguments)

        self._emit(
            {
                "type": "tool_end",
                "step": step,
                "name": name,
                "ok": ok,
                "result": result,
            }
        )

        self.memory.record("tool", step=step, name=name, ok=ok, result=result)
        self.memory.append(make_tool_message(call_id, shrink_result(result)))

    def _invoke_tool(self, name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """执行工具,返回 (结果, 是否成功)。

        同一轮对话里,相同的工具加相同的参数只会真正跑一次 —— 第二次直接吃缓存。
        任何 AgentError 都会变成一条结构化的错误结果,而不是让循环断掉。
        """
        cache_key = self.memory.cache_key(name, arguments)

        hit = self.memory.get_cached(cache_key)
        if hit is not None:
            marked = dict(hit)
            marked["cached"] = True
            return marked, True

        try:
            fresh = self.registry.dispatch(name, arguments)
        except AgentError as exc:
            return {"error": exc.message, "hint": exc.hint or None}, False
        except Exception as exc:  # noqa: BLE001 - 工具实现里的意外 bug 也不该炸掉循环
            return {"error": f"工具内部出错:{type(exc).__name__}: {exc}"}, False

        self.memory.set_cached(cache_key, fresh)
        return fresh, True

    # ------------------------------------------------------------------
    # 事件

    def _emit(self, event: dict[str, Any]) -> None:
        if self.on_event is None:
            return
        self.on_event(event)
