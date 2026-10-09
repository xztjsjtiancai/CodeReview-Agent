"""工具的基类。

工具这边只干纯计算:给一组参数,还一个 dict。它不知道模型的存在,
也不该知道。参数格式用 JSON Schema 描述,直接交给模型当 tools 用。

有一件事值得强调:description 写得好不好,直接决定模型会不会用对工具。
所以每个工具的 description 都尽量把「干什么、什么时候用、返回什么」说全。
"""

from __future__ import annotations

from abc import ABC
from abc import abstractmethod
from typing import Any

from agent.errors import ToolArgumentError
from agent.errors import ToolError


__all__ = ["Tool", "ToolError", "ToolArgumentError"]

# 参数类型转换时,字符串形式里哪些算「真」
TRUE_WORDS = {"1", "true", "yes", "on"}


class Tool(ABC):
    """所有工具的父类。"""

    # 模型靠这个名字来调用
    name: str = ""

    # 给模型看的说明。这段文字就是工具的「用户手册」。
    description: str = ""

    # 参数的 JSON Schema
    parameters: dict[str, Any] = {"type": "object", "properties": {}}

    @abstractmethod
    def run(self, **kwargs: Any) -> dict[str, Any]:
        """干活的方法。

        必须返回一个能 JSON 序列化的 dict。出错时不要抛裸异常,
        抛 ToolError 的子类 —— 上层会把它变成一条 Observation 喂回模型,
        模型下一轮往往就能自己调整过来。
        """

    # ==================================================================
    # 参数检查

    def validate(self, arguments: dict[str, Any]) -> dict[str, Any]:
        """按 parameters 做一轮轻量校验,并把类型理顺。

        做三件事:挡住没声明过的参数名、查必填项、把明显能转的类型转掉
        (模型经常把数字写成字符串)。
        """
        if not isinstance(arguments, dict):
            raise ToolArgumentError(f"工具 {self.name} 的参数必须是 JSON 对象")

        spec = self.parameters.get("properties", {})
        required = self.parameters.get("required", [])

        self._reject_unknown(arguments, spec)
        self._reject_missing(arguments, required)
        self._coerce_types(arguments, spec)

        return arguments

    def _reject_unknown(self, arguments: dict[str, Any], spec: dict[str, Any]) -> None:
        """参数名拼错时必须报错。

        如果静默忽略,模型会以为自己调对了,然后在错误的路上越走越远。
        """
        extra = set(arguments) - set(spec)
        if not extra:
            return

        allowed = ", ".join(sorted(spec)) or "无"
        raise ToolArgumentError(
            f"工具 {self.name} 收到未声明的参数:{', '.join(sorted(extra))};可用参数:{allowed}"
        )

    def _reject_missing(self, arguments: dict[str, Any], required: list[str]) -> None:
        missing = []
        for key in required:
            if key not in arguments:
                missing.append(key)
            elif arguments[key] is None:
                missing.append(key)

        if missing:
            raise ToolArgumentError(f"工具 {self.name} 缺少必填参数:{', '.join(missing)}")

    def _coerce_types(self, arguments: dict[str, Any], spec: dict[str, Any]) -> None:
        """把能救回来的类型救回来。

        模型把 3 写成 "3" 是家常便饭,这种直接转掉比报错友好得多。
        """
        for key, value in arguments.items():
            wanted = spec.get(key, {}).get("type")

            if wanted == "integer":
                arguments[key] = self._as_int(key, value)
            elif wanted == "string":
                if not isinstance(value, str):
                    arguments[key] = str(value)
            elif wanted == "boolean":
                arguments[key] = self._as_bool(value)

    def _as_int(self, key: str, value: Any) -> Any:
        if isinstance(value, bool):
            # bool 是 int 的子类,但当成整数传给工具几乎肯定是模型搞错了
            return int(value)

        if isinstance(value, int):
            return value

        try:
            return int(value)
        except (TypeError, ValueError):
            raise ToolArgumentError(
                f"工具 {self.name} 的参数 {key} 需要整数,实际收到 {value!r}"
            ) from None

    @staticmethod
    def _as_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value

        if isinstance(value, str):
            return value.strip().lower() in TRUE_WORDS

        return bool(value)

    # ==================================================================
    # 导出

    def schema(self) -> dict[str, Any]:
        """转成 OpenAI 兼容的工具定义。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
