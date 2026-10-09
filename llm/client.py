"""与模型通信。

这一层只做一件事:把 messages 发出去,把 assistant 消息收回来。
它不认识「工具」是什么,也不做任何业务判断 —— 那些都在 agent 那边。

几个和 DeepSeek 接口有关的坑,写在这里免得以后再踩:

  * thinking 参数必须塞进 extra_body。当成普通关键字参数传会被 SDK 拦下来。
  * 思考模式开了之后,temperature / presence_penalty / frequency_penalty
    传了也不生效(不报错,但也没用)。
  * 开了思考模式又带了 tools 的请求,历史里每一轮的 reasoning_content
    都要原样回传,漏一条就是 400。所以调用方必须用 append(msg) 存原始对象,
    不能拆开重拼。
"""

from __future__ import annotations

from collections.abc import Callable
import random
import time
from typing import Any

from openai import APIConnectionError
from openai import APIStatusError
from openai import APITimeoutError
from openai import AuthenticationError
from openai import BadRequestError
from openai import OpenAI
from openai import PermissionDeniedError
from openai import RateLimitError

from agent.errors import LLMAuthError
from agent.errors import LLMError
from agent.errors import LLMResponseError
from agent.errors import LLMRetryableError
from agent.errors import classify_http_status
from config import Config


# 重试回调:(第几次, 共几次, 原因, 等多少秒)
RetryCallback = Callable[[int, int, str, float], None]

# 退避基数。第 n 次重试等 BASE * 2^(n-1) 秒。
BASE_DELAY = 1.0

# 加一点随机抖动,避免多个请求同一时刻一起重试又把限流打满
JITTER = 0.3

# 异常文本截断长度,避免把整页错误塞进用户提示里
DETAIL_LIMIT = 300


class LLMClient:
    """对 OpenAI 兼容接口(DeepSeek)的一层薄封装。"""

    def __init__(self, config: Config, on_retry: RetryCallback | None = None) -> None:
        self._config = config
        self._on_retry = on_retry

        # 这里把 SDK 自带的重试关掉,改成自己控制。
        # 否则 SDK 在后台悄悄重试,用户在终端上看不到任何动静,
        # 而作业明确要求「重试机制」得看得见。
        self._client = OpenAI(
            api_key=config.api_key or "missing-api-key",
            base_url=config.base_url,
            timeout=config.request_timeout,
            max_retries=0,
        )

    # ==================================================================
    # 唯一对外的方法

    def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ):
        """发一轮请求,返回 assistant 消息。

        拿到的消息请**直接 append 进历史**,不要手工拆成 dict 再拼回去,
        否则 reasoning_content 会丢,以后开思考模式就会 400。

        抛出的都是 LLMError 的子类。其中 LLMRetryableError 表示
        重试次数已经用完还是没成功。
        """
        self._reject_missing_key()

        payload = self._build_payload(messages, tools)

        attempts = max(1, self._config.max_retries)
        attempt = 1

        while attempt <= attempts:
            try:
                response = self._client.chat.completions.create(**payload)
            except Exception as exc:  # noqa: BLE001 - 统一翻译成自家异常后再抛
                error = self._translate(exc)

                if not error.retryable:
                    raise error from exc

                if attempt >= attempts:
                    raise error from exc

                delay = self._delay_for(attempt)
                if self._on_retry is not None:
                    self._on_retry(attempt, attempts, error.message, delay)

                time.sleep(delay)
                attempt += 1
                continue

            return self._pick_message(response)

        # 走不到这里:上面要么 return,要么 raise
        raise LLMError("调用模型失败,但没有拿到具体原因")

    # ==================================================================
    # 请求构造

    def _reject_missing_key(self) -> None:
        if self._config.api_key:
            return

        raise LLMAuthError(
            "未配置 DEEPSEEK_API_KEY",
            "复制 .env.example 为 .env 并填入 Key:https://platform.deepseek.com/api_keys",
        )

    def _build_payload(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "stream": False,
        }

        if tools:
            payload["tools"] = tools

        # 思考模式开关只能走 extra_body
        payload["extra_body"] = {"thinking": {"type": self._config.thinking}}

        return payload

    @staticmethod
    def _delay_for(attempt: int) -> float:
        """第 attempt 次重试前该等多久。指数增长,再叠一点抖动。"""
        growth = BASE_DELAY * (2 ** (attempt - 1))
        return growth + random.uniform(0, JITTER)

    # ==================================================================
    # 异常翻译

    @staticmethod
    def _translate(exc: Exception) -> LLMError:
        """把 SDK 抛出来的东西翻译成自家的异常类型。"""
        if isinstance(exc, (AuthenticationError, PermissionDeniedError)):
            return classify_http_status(401)

        if isinstance(exc, RateLimitError):
            return LLMRetryableError(
                "请求被限流:HTTP 429",
                "程序会自动退避重试;如果一直失败,检查一下账户的并发额度",
            )

        if isinstance(exc, APITimeoutError):
            return LLMRetryableError(
                "请求超时",
                "检查网络连接或代理设置",
            )

        if isinstance(exc, APIConnectionError):
            return LLMRetryableError(
                "连不上服务端",
                "检查网络连接或代理设置",
            )

        if isinstance(exc, BadRequestError):
            status = getattr(exc, "status_code", 400)
            if not status:
                status = 400
            return classify_http_status(status, detail=str(exc)[:DETAIL_LIMIT])

        if isinstance(exc, APIStatusError):
            status = getattr(exc, "status_code", 0)
            if not status:
                status = 0
            return classify_http_status(status, detail=str(exc)[:DETAIL_LIMIT])

        # 剩下的都不是 HTTP 层的问题,多半是 SDK 自己的参数校验没过
        return LLMError(
            f"调用模型时出了没预料到的错:{type(exc).__name__}: {exc}",
            "如果怀疑是参数问题,检查 config.py 里的模型名和 base_url",
        )

    # ==================================================================
    # 响应解析

    @staticmethod
    def _pick_message(response: Any):
        """从响应里把 assistant 消息拎出来,顺便检查结构对不对。"""
        choices = getattr(response, "choices", None)

        if not choices:
            raise LLMResponseError(
                "模型返回的 choices 是空的",
                "稍后重试;如果一直这样,确认 base_url 指向的是 DeepSeek 接口",
            )

        first = choices[0]
        message = getattr(first, "message", None)

        if message is None:
            raise LLMResponseError("模型响应里没有 message 字段")

        return message
