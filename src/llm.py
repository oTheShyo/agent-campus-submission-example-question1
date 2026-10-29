"""模型接入。

两点刻意设计：
1. **只取 content 与 tool_calls，丢弃 reasoning_content**。题目明确要求
   "无须提交模型内部思维过程"，所以思维链既不落 trace 也不进日志。
2. 内置最小调用间隔与 429 指数退避。USTC 端点的 key 有 20 RPM 上限，
   3.2s 间隔是实测换算的结果，不是拍脑袋。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

# 重试时的退避序列（秒）
BACKOFF = (2, 4, 8)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict

    def as_message(self) -> dict:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": json.dumps(self.arguments, ensure_ascii=False)},
        }


@dataclass
class LLMReply:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    latency_ms: int = 0
    model: str = ""
    finish_reason: str = ""


class BaseLLM:
    name = "base"

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply:  # pragma: no cover
        raise NotImplementedError


class OpenAICompatLLM(BaseLLM):
    """任意 OpenAI 兼容端点：USTC / 百炼 / DeepSeek / Ollama 都走这里。"""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        timeout: float = 60.0,
        min_interval: float = 3.2,
        max_retries: int = 3,
    ) -> None:
        from openai import OpenAI

        self.name = "openai-compatible"
        self.model = model
        self.min_interval = max(0.0, float(min_interval))
        self.max_retries = max_retries
        self._last_call_at = 0.0
        self.retry_events: list[dict] = []
        # SDK 自带的 retry 关掉，自己控制退避，便于把重试写进 trace
        self.client = OpenAI(base_url=base_url, api_key=api_key, timeout=timeout, max_retries=0)

    def _throttle(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle()
            started = time.monotonic()
            try:
                resp = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=tools,
                    tool_choice="auto",
                    temperature=0,
                    max_tokens=3000,
                )
                self._last_call_at = time.monotonic()
                return self._parse(resp, int((time.monotonic() - started) * 1000))
            except Exception as exc:  # 网络/限流/服务端错误统一退避
                self._last_call_at = time.monotonic()
                last_exc = exc
                if attempt >= self.max_retries or not _retryable(exc):
                    raise
                delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                self.retry_events.append(
                    {
                        "attempt": attempt + 1,
                        "delay_sec": delay,
                        "error": f"{type(exc).__name__}: {exc}"[:300],
                    }
                )
                time.sleep(delay)
        raise RuntimeError(f"模型调用失败：{last_exc}")

    def _parse(self, resp, latency_ms: int) -> LLMReply:
        choice = resp.choices[0]
        msg = choice.message
        # 只取 content；reasoning_content（思维链）在这里被有意丢弃
        content = getattr(msg, "content", None) or ""
        calls: list[ToolCall] = []
        for tc in getattr(msg, "tool_calls", None) or []:
            raw = tc.function.arguments or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                # 参数被长度截断时不要静默降级成空参数，明确标记出来让模型重发
                args = {"__parse_error__": raw[:300]}
            calls.append(ToolCall(id=tc.id, name=tc.function.name, arguments=args))
        usage = {}
        if getattr(resp, "usage", None) is not None:
            usage = {
                "prompt_tokens": getattr(resp.usage, "prompt_tokens", 0),
                "completion_tokens": getattr(resp.usage, "completion_tokens", 0),
                "total_tokens": getattr(resp.usage, "total_tokens", 0),
            }
        return LLMReply(content=content, tool_calls=calls, usage=usage, latency_ms=latency_ms,
                        model=self.model, finish_reason=str(getattr(choice, "finish_reason", "") or ""))


def _retryable(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(k in text for k in ("429", "rate limit", "ratelimit", "timeout", "timed out",
                                   "502", "503", "504", "connection", "overloaded"))
