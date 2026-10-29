"""测试替身：脚本化模型，按预定动作序列返回 tool_calls，不联网。"""

from __future__ import annotations

import itertools

from src.llm import BaseLLM, LLMReply, ToolCall

_ids = itertools.count(1)


def tc(name: str, args: dict | None = None) -> ToolCall:
    return ToolCall(id=f"call_{next(_ids)}", name=name, arguments=args or {})


class ScriptedLLM(BaseLLM):
    """按脚本逐轮返回；脚本用尽后默认结束（可设为无限重复以测试上限）。"""

    name = "scripted"
    model = "scripted"

    def __init__(self, script: list[list[ToolCall]], repeat_last: bool = False, text: str = ""):
        self.script = list(script)
        self.repeat_last = repeat_last
        self.text = text
        self.calls = 0

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        self.calls += 1
        if not self.script:
            return LLMReply(content=self.text or "done", tool_calls=[])
        if self.calls <= len(self.script):
            calls = self.script[self.calls - 1]
        elif self.repeat_last:
            calls = self.script[-1]
        else:
            return LLMReply(content=self.text or "done", tool_calls=[])
        return LLMReply(content="", tool_calls=list(calls), usage={"total_tokens": 10}, latency_ms=1)
