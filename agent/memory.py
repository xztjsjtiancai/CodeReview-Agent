"""对话记忆:历史消息、工具结果缓存、轨迹、超长裁剪。

三件要说清楚的事:

1. 历史里放的是**原始消息对象**,不做拆字段重组。SDK 返回的 message 上带着
   content、tool_calls、reasoning_content,原样存下来才不会丢东西。
   以后要是开启思考模式,reasoning_content 必须完整回传,否则服务端直接 400。

2. 同一轮对话里,同样的工具加同样的参数只跑一次,结果记在缓存里。

3. 上下文太长要裁。裁的原则是「保住最近的原文,把最早的整段扔掉」。
   曾经试过只压缩中间轮次的工具结果而保留所有消息,结果发现总量反而更大 ——
   因为只有 tool 消息压得掉,assistant 和 user 消息一个字都少不了。
"""

from __future__ import annotations

import json
from typing import Any


# 裁剪说明本身也要占字符,预算里先留出来,否则裁完还会略微超标
NOTE_BUDGET = 80

# 每条消息的结构开销(role、分隔符等)的粗略估计
PER_MESSAGE_OVERHEAD = 16


class Memory:
    """装对话历史和工具结果的容器。"""

    def __init__(
        self,
        max_context_chars: int = 120000,
        keep_recent_messages: int = 12,
    ) -> None:
        self._system_message: dict[str, Any] | None = None
        self._messages: list[Any] = []

        # 缓存单独存两个 dict:一个记参数对应的结果,一个记结果来自哪个工具。
        # 拆开存是为了将来想按工具名清理缓存时不用遍历解析 key。
        self._cache_values: dict[str, Any] = {}
        self._cache_owners: dict[str, str] = {}

        self._trail: list[dict[str, Any]] = []

        self.max_context_chars = max_context_chars
        self.keep_recent_messages = keep_recent_messages

    # ==================================================================
    # 消息进出

    def set_system(self, content: str) -> None:
        """设置 System Prompt。重复调用只会覆盖,不会塞进去两条。"""
        self._system_message = {"role": "system", "content": content}
        if len(self._messages) == 0:
            self._messages.append(self._system_message)

    def append(self, message: Any) -> None:
        """追加一条消息。可以是 dict,也可以是 SDK 返回的消息对象。"""
        self._messages.append(message)

    def clear(self) -> None:
        """清掉对话、缓存和轨迹,只留 System Prompt。"""
        if self._system_message is None:
            self._messages = []
        else:
            self._messages = [self._system_message]

        self._cache_values.clear()
        self._cache_owners.clear()
        self._trail.clear()

    def __len__(self) -> int:
        return len(self._messages)

    @property
    def messages(self) -> list[Any]:
        """发给模型用的完整消息列表,保证第一条是 System Prompt。"""
        if self._system_message is None:
            return list(self._messages)

        if len(self._messages) == 0:
            return [self._system_message]

        if self._messages[0] is self._system_message:
            return list(self._messages)

        return [self._system_message, *self._messages]

    # ==================================================================
    # 工具结果缓存

    @staticmethod
    def cache_key(tool_name: str, arguments: dict[str, Any] | str | None) -> str:
        """把工具名和参数拼成一个稳定的键。

        参数用 sort_keys 序列化,这样 {"a":1,"b":2} 和 {"b":2,"a":1}
        会命中同一条缓存。
        """
        if isinstance(arguments, str):
            body = arguments
        else:
            body = json.dumps(arguments or {}, sort_keys=True, ensure_ascii=False)
        return tool_name + ":" + body

    def get_cached(self, key: str) -> Any | None:
        return self._cache_values.get(key)

    def set_cached(self, key: str, value: Any) -> None:
        self._cache_values[key] = value
        # 顺手记下归属,便于调试时回答「这条缓存是谁留下的」
        owner = key.split(":", 1)[0]
        self._cache_owners[key] = owner

    def cached_tool_of(self, key: str) -> str:
        """返回这条缓存是哪个工具产生的,没有则空串。"""
        return self._cache_owners.get(key, "")

    # ==================================================================
    # 轨迹(给 /history 用)

    def record(self, kind: str, **payload: Any) -> None:
        entry = {"kind": kind}
        entry.update(payload)
        self._trail.append(entry)

    @property
    def trail(self) -> list[dict[str, Any]]:
        # 返回副本,外面乱改不会污染内部状态
        return list(self._trail)

    # ==================================================================
    # 长度与裁剪

    def estimated_chars(self) -> int:
        """粗略估算当前上下文有多少字符。"""
        total = 0
        for message in self.messages:
            total += len(content_of(message))
            calls = tool_calls_of(message)
            if calls:
                total += len(str(calls))
        return total

    def maybe_trim(self) -> bool:
        """太长就裁。返回这次有没有真的动手。

        做法是从末尾往前累加,装不下就停,停下之前的那些消息全部丢弃。
        注意两个不能碰的约束:

          * 尾巴不能以 tool 消息开头。tool 消息必须紧跟着它对应的
            assistant(tool_calls),中间被切掉的话 tool_call_id 就悬空了,
            服务端会直接返回 400。
          * 第一条 user 消息永远留着,它写着这轮任务是什么。
        """
        if self.estimated_chars() <= self.max_context_chars:
            return False

        if len(self._messages) <= 2:
            return False

        head = self._messages[:1]
        budget = self.max_context_chars - NOTE_BUDGET

        split = self._find_split(len(self._messages), head, budget)
        if split >= len(self._messages):
            return False

        # 把切分点往前挪到一条非 tool 消息上
        while split > 1 and role_of(self._messages[split]) == "tool":
            split -= 1

        dropped = split - 1
        if dropped <= 0:
            return False

        note = {
            "role": "user",
            "content": (
                f"[这里原有 {dropped} 条较早的消息,因为上下文太长已被裁掉。"
                "如果需要重新查看某段内容,请告诉我具体的文件路径。]"
            ),
        }
        self._messages = [*head, note, *self._messages[split:]]
        return True

    def _find_split(self, total: int, head: list[Any], budget: int) -> int:
        """从末尾往前累加,算出最靠前的可保留位置。"""
        used = len(content_of(head[0]))
        split = total

        index = total - 1
        while index > 0:
            cost = len(content_of(self._messages[index])) + PER_MESSAGE_OVERHEAD
            if used + cost > budget:
                break
            used += cost
            split = index
            index -= 1

        return split


# ======================================================================
# 消息形态适配
#
# 历史里混着两种东西:我们自己造的 dict(比如 user 消息、tool 消息),
# 和 SDK 返回的消息对象。下面这几个函数把差异挡在外面。
# ======================================================================


def role_of(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("role", ""))
    return str(getattr(message, "role", ""))


def content_of(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(getattr(message, "content", None) or "")


def tool_calls_of(message: Any) -> Any:
    if isinstance(message, dict):
        return message.get("tool_calls")
    return getattr(message, "tool_calls", None)


def tool_call_id_of(message: Any) -> str:
    if isinstance(message, dict):
        return str(message.get("tool_call_id") or "")
    return str(getattr(message, "tool_call_id", "") or "")


def make_tool_message(tool_call_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """拼一条回灌给模型的 tool 消息。

    内容用 JSON 字符串承载:模型对 JSON 的解析最稳,而且 ensure_ascii=False
    能让中文保持可读,排查日志时省事。
    """
    return {
        "role": "tool",
        "tool_call_id": tool_call_id,
        "content": json.dumps(payload, ensure_ascii=False),
    }
