"""ReAct 主循环：模型选动作 → 执行工具 → 观察结果 → 模型调整或结束。

这里的循环本身很薄（约 80 行），复杂的是护栏与报告校验，它们分别在 guards.py 和 verifier.py。
刻意不给流程写死分支：走哪条路（补查/换时段/请求确认/阻断）由模型看着工具返回决定。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .llm import BaseLLM
from .models import publish_date_of
from .store import DataStore
from .tools import TOOLS_SPEC, ToolBox
from .trace import TraceWriter, now_iso, summarize
from .verifier import build_report

TZ = timezone(timedelta(hours=8))

SYSTEM_PROMPT = """你是短视频内容团队的「发布排期助手」。你的交付物只有两样：排期草稿 + 待确认项。
你没有任何发布权限，也绝不能说某条素材「已发布」「已上线」「已生效」。

可用的发布时段网格是固定的整点时段，你只能在这些时段里选。

工作方法（顺序不重要，按情况调整，但每条素材都要走完）：
1. 先用 list_pending_materials 确认待处理素材，再用 read_material 读取字段。
2. 字段缺失时先调用 lookup_supplement 补查。补得到就继续；补不到再阻断，不要猜。
3. 用 check_rules 看风险。blocking=true 的素材不得保存草稿，只能阻断并说明原因。
   severity=warn 的素材可以排，但必须先 request_confirmation 登记待确认项。
4. 排期前必须 query_slots 查真实占用，不要假设某个时段可用。
   查的时候把本次要排期的素材 ID 放进 for_material_ids，这样素材自己已有的草稿不会被当成冲突
   （否则改期时会把自己挡在门外）。
   如果用户要求的时段已被占用：查该时段之后还有哪些空闲时段，挑一个最接近的，
   然后 request_confirmation 说明「原要求已被占用、建议改到某时段」，拿到 confirmation_id 后再存草稿。
5. 拟排时段与用户要求不一致时，save_schedule_draft 必须带 confirmation_id，否则会被拒绝。
   拿到 confirmation_id 之后**不要停下**：草稿只是草稿，写进去不会产生任何发布效果，
   所以登记确认后应当继续调用 save_schedule_draft 把草稿写出来，把「是否接受这个时段」
   留给人在待确认项里回答。只有被阻断或信息不足的素材才不写草稿。
   如果同时有多个候选时段，挑最接近用户要求的那个直接写进草稿，不要让草稿和时段留空等人选。
6. 用户要求的发布时间（requested_publish_at）是只读的硬性要求，你没有权限修改它，
   也绝不能在输出里把它改写成你建议的时段。
7. save_schedule_draft 被拒绝时，读 rejected 里的 error_code 修正参数重试；
   如果错误是 STORE_WRITE_FAILED，可以重试一次；仍失败就如实报告保存失败，不得声称已保存。
8. 所有素材都处理完后，调用 finalize 提交结论。每个素材一条 decision：
   schedule（草稿已保存）、pending_confirmation（待人工确认）、block（阻断不排期）、
   save_failed（审核通过但草稿保存失败）、no_action（素材已在排期中或本次无需操作）。
   如果素材已经在排期里（already_scheduled=true / R8），用 no_action 或 block，
   不要用 schedule —— 你没有为它保存过草稿。
   对 block 和 pending_confirmation 的素材，在 pending_items 里写清「人需要做什么」
   （补哪些字段、找谁复审、确认哪个时段），不要留空。
9. 不要在 finalize 里编造工具没返回过的草稿号或保存结果。
10. 每次发起工具调用时，在消息正文里用一句不超过 40 字的话说明「这次调用要解决什么」，
    例如「18:00 被占，先查后面还有哪些空闲时段」。这句话会被记录进运行 trace 作为决策依据。

注意：同一素材同一目标时段重复保存只会产生一份草稿，这是正常行为（deduplicated=true），
不要因此认为失败，也不要换个时段绕开重复。"""


@dataclass
class RunMeta:
    run_id: str
    mode: str
    model: str
    started_at: str
    max_steps: int = 12
    max_tool_calls: int = 16
    steps: int = 0
    tool_calls: int = 0
    tokens: int = 0
    stop_reason: str = ""
    retry_events: list[dict] = field(default_factory=list)


class Agent:
    def __init__(
        self,
        store: DataStore,
        toolbox: ToolBox,
        llm: BaseLLM,
        trace_path: str | None = None,
        max_steps: int = 12,
        max_tool_calls: int = 16,
        mode: str = "live",
    ) -> None:
        self.store = store
        self.toolbox = toolbox
        self.llm = llm
        self.trace = TraceWriter(trace_path)
        self.max_steps = max_steps
        self.max_tool_calls = max_tool_calls
        self.mode = mode

    # ------------------------------------------------------------------

    def _user_prompt(self, spec: dict) -> str:
        ids = spec.get("materials") or []
        lines = [
            f"批次：{spec.get('request_id', 'N/A')}",
            f"运营：{spec.get('operator', '内容运营')}",
            f"本次要处理的素材：{', '.join(ids)}",
        ]
        if spec.get("focus_date"):
            lines.append(f"重点关注日期：{spec['focus_date']}")
        if spec.get("hard_note"):
            lines.append(f"用户特别说明：{spec['hard_note']}")
        if spec.get("extra_schedule"):
            lines.append(
                "运营侧临时新增的占用（这些时段已被别人占）："
                + "；".join(f"{e['date']} {e['time']} 由 {e.get('material_id', '外部')} 占用" for e in spec["extra_schedule"])
            )
        lines.append("请按顺序处理这批素材，给出审核状态、建议时段、待确认项与草稿保存结果。")
        return "\n".join(lines)

    def run(self, spec: dict) -> dict:
        run_id = spec.get("request_id") or f"run-{datetime.now(TZ).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        meta = RunMeta(run_id=run_id, mode=self.mode, model=getattr(self.llm, "model", self.llm.name),
                       started_at=now_iso(), max_steps=self.max_steps, max_tool_calls=self.max_tool_calls)

        self.trace.write(
            {
                "type": "run_start",
                "run_id": run_id,
                "mode": self.mode,
                "model": meta.model,
                "max_steps": self.max_steps,
                "max_tool_calls": self.max_tool_calls,
                "input": {k: v for k, v in spec.items() if k != "materials"},
                "target_materials": spec.get("materials"),
            }
        )

        messages: list[dict] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": self._user_prompt(spec)},
        ]
        expected = list(spec.get("materials") or [])
        stop_reason = ""
        streak_key: tuple[str, str] | None = None
        streak = 0

        while True:
            if meta.steps >= self.max_steps:
                stop_reason = "step_cap"
                break

            try:
                reply = self.llm.complete(messages, TOOLS_SPEC)
            except Exception as exc:
                stop_reason = "llm_error"
                self.trace.write({"type": "llm_error", "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"[:500]})
                break

            meta.steps += 1
            meta.tokens += int(reply.usage.get("total_tokens", 0) or 0)

            if not reply.tool_calls:
                self.trace.write(
                    {
                        "type": "model_message",
                        "run_id": run_id,
                        "step": meta.steps,
                        "rationale": reply.content[:600],
                        "latency_ms": reply.latency_ms,
                    }
                )
                stop_reason = "model_stopped"
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": reply.content or "",
                    "tool_calls": [tc.as_message() for tc in reply.tool_calls],
                }
            )

            for tc in reply.tool_calls:
                if meta.tool_calls >= self.max_tool_calls:
                    stop_reason = "tool_call_cap"
                    break
                meta.tool_calls += 1
                result = self.toolbox.dispatch(tc.name, tc.arguments)

                # 同一个工具连续以同一个错误码失败，说明模型卡住了。
                # 继续重试只会烧掉配额，直接停下并如实报未完成。
                if result.get("ok") is False:
                    key = (tc.name, str(result.get("error_code")))
                    streak = streak + 1 if key == streak_key else 1
                    streak_key = key
                    if streak >= 3:
                        self.trace.write(
                            {
                                "type": "stalled",
                                "run_id": run_id,
                                "step": meta.steps,
                                "tool": tc.name,
                                "error_code": result.get("error_code"),
                                "attempts": streak,
                                "message": "同一工具连续以同一错误失败，判定为卡死并停止",
                            }
                        )
                        stop_reason = "repeated_failure"
                        break
                else:
                    streak_key, streak = None, 0
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.name,
                        "content": json.dumps(result, ensure_ascii=False),
                    }
                )
                self.trace.write(
                    {
                        "type": "tool_call",
                        "run_id": run_id,
                        "step": meta.steps,
                        "tool_call_index": meta.tool_calls,
                        "rationale": (reply.content or "").strip()[:400],
                        "tool": tc.name,
                        "args": tc.arguments,
                        "result_summary": summarize(result),
                        "result": result,
                        "next_hint": "" if result.get("ok") else f"需要处理 {result.get('error_code')}",
                        "latency_ms": reply.latency_ms,
                        "tokens": reply.usage.get("total_tokens", 0),
                        "finish_reason": reply.finish_reason,
                    }
                )

            if stop_reason:
                break

            if self.toolbox.final_decisions:
                covered = {d["material_id"] for d in self.toolbox.final_decisions}
                if set(expected).issubset(covered):
                    stop_reason = "finalized"
                    break

        if not stop_reason:
            stop_reason = "loop_exit"
        if stop_reason in ("step_cap", "tool_call_cap"):
            # 达到上限：不再补写任何草稿，明确返回未完成
            self.trace.write({"type": "cap_reached", "run_id": run_id, "reason": stop_reason})

        meta.stop_reason = stop_reason
        report = build_report(self.store, self.toolbox, spec, meta)

        self.trace.write(
            {
                "type": "run_end",
                "run_id": run_id,
                "status": report["status"],
                "stop_reason": stop_reason,
                "steps": meta.steps,
                "tool_calls": meta.tool_calls,
                "tokens": meta.tokens,
                "trace_path": str(self.trace.path) if self.trace.path else None,
            }
        )
        report["trace_path"] = str(self.trace.path) if self.trace.path else None
        return report


def pending_materials_today(store: DataStore, date: str | None = None) -> list[str]:
    """CLI 在不指定素材时的兜底：取全部待处理素材。"""
    return [m["material_id"] for m in store.materials
            if m.get("status") == "pending" and (date is None or publish_date_of(m) == date)]
