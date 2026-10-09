"""错误类型定义。

本项目的错误分两大类:一类是「等一会儿再试可能就好了」(网络抖动、限流),
另一类是「再试一百次也没用」(Key 错了、余额没了)。把这两类的判断固定在
异常类上,调用方只读一个属性就知道该不该重试,不必散落一堆 isinstance 判断。
"""

from __future__ import annotations


# ======================================================================
# 基础类型
# ======================================================================


class AgentError(Exception):
    """本项目所有「可预期」错误的根类型。

    构造时需要给两样东西:一句给用户看的话,以及一句可选的排查建议。
    UI 层只捕获这一个类型就够了,不会让 traceback 直接糊到用户脸上。
    """

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        if self.hint:
            return f"{self.message}\n  ↳ {self.hint}"
        return self.message


# ======================================================================
# 模型调用相关
# ======================================================================


class LLMError(AgentError):
    """模型调用失败的根类型。默认认为不值得重试。"""

    retryable: bool = False


class LLMRetryableError(LLMError):
    """瞬时故障。超时、连接断开、限流、服务端 5xx 都归到这里。"""

    retryable = True


class LLMAuthError(LLMError):
    """身份没通过。401 或 403。重试没有任何意义,必须换 Key。"""

    retryable = False


class LLMBalanceError(LLMError):
    """余额不足或已欠费。对应 402。"""

    retryable = False


class LLMBadRequestError(LLMError):
    """请求本身不合法。对应 400。"""

    retryable = False


class LLMResponseError(LLMError):
    """HTTP 状态是 200,但返回的结构不是我们期待的。"""

    retryable = False


# ======================================================================
# 工具执行相关
# ======================================================================


class ToolError(AgentError):
    """工具执行失败的根类型。

    这类错误在 Agent 循环里不会向上冒泡,而是被转成一条 Observation
    重新喂给模型,让模型自己换个做法(比如路径写错了就改正)。
    """


class ToolNotFound(ToolError):
    """模型点名了一个不存在或没注册的工具。"""


class ToolArgumentError(ToolError):
    """参数缺了、类型不对,或者干脆不是合法 JSON。"""


# ======================================================================
# 循环控制相关
# ======================================================================


class MaxStepsExceeded(AgentError):
    """转满最大步数还是没得出结论。"""


# ======================================================================
# 状态码归类
# ======================================================================


def classify_http_status(status_code: int, detail: str = "") -> LLMError:
    """按 HTTP 状态码造出对应的异常实例。

    注意 402:这是本项目服务端用来表示「余额不足」的约定,
    OpenAI SDK 没有为它准备专门的异常类,所以只能在这一层兜住。
    """
    if status_code == 401 or status_code == 403:
        return LLMAuthError(
            f"API Key 无效或没有权限:HTTP {status_code}",
            "确认 .env 里的 DEEPSEEK_API_KEY 写对了,而且前后没有多余空格",
        )

    if status_code == 402:
        return LLMBalanceError(
            "账户余额不足:HTTP 402",
            "去 https://platform.deepseek.com/top_up 充值后重试",
        )

    if status_code == 400:
        if detail:
            return LLMBadRequestError(
                f"请求被服务端拒绝:HTTP 400,{detail}",
                "常见原因是思考模式下传了 temperature,或者 messages 结构不合法",
            )
        return LLMBadRequestError(
            "请求被服务端拒绝:HTTP 400",
            "常见原因是思考模式下传了 temperature,或者 messages 结构不合法",
        )

    if status_code == 429:
        return LLMRetryableError(
            "请求太频繁被限流:HTTP 429",
            "程序会自动退避重试",
        )

    if 500 <= status_code < 600:
        return LLMRetryableError(
            f"服务端出错:HTTP {status_code}",
            "程序会自动退避重试",
        )

    return LLMError(f"遇到没预料到的 HTTP 状态:{status_code},{detail}")
