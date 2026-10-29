"""离线确定性策略（Mock）。

⚠️ 边界说明：这不是模型。它是一段按固定优先级拼出来的决策逻辑，
   只能证明"流程、护栏、报告校验是通的"，**不能证明真实模型的决策效果**。
   真实模型的效果证据在 examples/traces/live/ 下。

设计成"每轮只发一个工具调用"，让它也走同一个 ReAct 循环，从而复用全部护栏。
"""

from __future__ import annotations

import json

from . import guards
from .llm import BaseLLM, LLMReply, ToolCall
from .models import missing_fields, publish_date_of, publish_time_of


def _last_json(messages: list[dict]) -> tuple[str, dict] | tuple[None, None]:
    for m in reversed(messages):
        if m.get("role") == "tool":
            try:
                return m.get("name"), json.loads(m.get("content") or "{}")
            except json.JSONDecodeError:
                return m.get("name"), {}
    return None, None


class MockBrain(BaseLLM):
    name = "mock-deterministic"
    model = "mock-deterministic"

    def __init__(self, store, spec: dict) -> None:
        self.store = store
        self.spec = spec
        self.ids = list(spec.get("materials") or [])
        self._seq = 0

    # ------------------------------------------------------------------

    def _ingest(self, messages: list[dict]) -> dict:
        k = {
            "listed": False, "reads": {}, "supplements": {}, "checks": {},
            "slots": {}, "confirms": {}, "rejected": {}, "approved": set(),
            "saves": [], "finalized": False,
        }
        for m in messages:
            if m.get("role") != "tool":
                continue
            name, res = m.get("name"), json.loads(m.get("content") or "{}")
            if not isinstance(res, dict):
                continue
            if name == "list_pending_materials":
                k["listed"] = True
            elif name == "read_material":
                for mat in res.get("materials", []):
                    k["reads"][mat["material_id"]] = mat
                    # 外部已批准的确认单直接复用，不重复登记（闭环的关键）
                    for c in mat.get("confirmations") or []:
                        if c.get("answer") == "accept":
                            k["confirms"].setdefault(mat["material_id"], c["confirmation_id"])
                            k["approved"].add(mat["material_id"])
                        elif c.get("answer") == "reject":
                            k["rejected"][mat["material_id"]] = c["confirmation_id"]
            elif name == "lookup_supplement":
                k["supplements"].setdefault(res.get("material_id"), {}).update(
                    {r["field"]: r["found"] for r in res.get("results", [])}
                )
            elif name == "check_rules":
                for mat in res.get("materials", []):
                    k["checks"][mat["material_id"]] = mat
            elif name == "query_slots":
                k["slots"][res.get("date")] = res
            elif name == "request_confirmation":
                if res.get("ok"):
                    k["confirms"][res["material_id"]] = res["confirmation_id"]
            elif name == "save_schedule_draft":
                k["saves"].append(res)
            elif name == "finalize":
                k["finalized"] = True
        return k

    def _call(self, name: str, args: dict, why: str = "") -> LLMReply:
        self._seq += 1
        return LLMReply(
            content=why,
            tool_calls=[ToolCall(id=f"mock_{self._seq}", name=name, arguments=args)],
            usage={"total_tokens": 0},
            latency_ms=0,
            model=self.model,
        )

    def _plan(self, k: dict) -> dict:
        """按已知事实算出每个素材的目标时段与是否需确认。"""
        plans: dict[str, dict] = {}
        for mid in self.ids:
            mat = self.store.materials_by_id.get(mid)
            if mat is None:
                continue
            check = k["checks"].get(mid) or {}
            miss = missing_fields(mat, self.store.required_fields)
            if miss or check.get("blocking"):
                plans[mid] = {"decision": "block", "missing": miss, "check": check}
                continue
            date = publish_date_of(mat)
            want = publish_time_of(mat)
            slot = k["slots"].get(date) or {}
            free = slot.get("free") or []
            if want in free:
                time = want
            else:
                later = [t for t in free if t >= want]
                time = (later or free or [want])[0]
            needs_conf = bool(time != want) or bool(guards.warn_risks(check.get("risks") or []))
            plans[mid] = {
                "decision": "pending_confirmation" if needs_conf else "schedule",
                "date": date, "time": time, "want": want,
                "deviates": time != want, "needs_conf": needs_conf,
                "check": check, "missing": miss,
            }
        return plans

    # ------------------------------------------------------------------

    def complete(self, messages: list[dict], tools: list[dict]) -> LLMReply:
        k = self._ingest(messages)

        if not k["listed"]:
            return self._call("list_pending_materials", {"date": self.spec.get("focus_date")},
                              "先确认这批素材里哪些还在待处理状态")

        if any(mid not in k["reads"] for mid in self.ids):
            return self._call("read_material", {"material_ids": self.ids}, "读字段，看有没有必需字段缺口")

        # 缺字段先补查，每条素材只查一次
        for mid in self.ids:
            miss = (k["reads"].get(mid) or {}).get("missing_required") or []
            if miss and mid not in k["supplements"]:
                return self._call("lookup_supplement", {"material_id": mid, "fields": miss},
                                  f"{mid} 缺 {len(miss)} 个必需字段，先去外部素材库补查")

        if any(mid not in k["checks"] for mid in self.ids):
            return self._call("check_rules", {"material_ids": self.ids}, "字段齐了，按规则看风险等级")

        plans = self._plan(k)
        by_date: dict[str, list[str]] = {}
        for mid in self.ids:
            mat = self.store.materials_by_id.get(mid)
            if mat is not None:
                by_date.setdefault(publish_date_of(mat), []).append(mid)
        for date in sorted(set(by_date) - set(k["slots"])):
            # 传 for_material_ids：素材自己已有的草稿不算占用（改期场景）
            return self._call("query_slots", {"date": date, "for_material_ids": by_date[date]},
                              f"排期前先查 {date} 的真实占用")

        for mid, p in plans.items():
            if p.get("needs_conf") and mid not in k["confirms"] and mid not in k["rejected"]:
                if p["deviates"]:
                    q = f"{p['want']} 已被占用，是否改到 {p['time']}？"
                    reason = f"用户要求 {p['want']} 与已有排期冲突，需确认替代时段"
                else:
                    warns = guards.warn_risks(p["check"].get("risks") or [])
                    q = f"该素材存在 {len(warns)} 条警告级风险，是否仍按 {p['time']} 排期？"
                    reason = "；".join(r["reason"] for r in warns)
                return self._call(
                    "request_confirmation",
                    {"material_id": mid, "question": q, "reason": reason,
                     "options": [p["time"], "改天", "取消"]},
                    f"{mid} 与用户要求不一致或有警告级风险，先登记待确认",
                )

        # 人工否决了替代方案：不再写草稿，转人工重新定时段
        for mid, cid in k["rejected"].items():
            p = plans.get(mid)
            if p and p.get("deviates") and p.get("decision") != "block":
                p["decision"] = "block"
                p["block_reason"] = f"替代时段提案已被人为否决（{cid}），需人工重新指定时段"

        if not k["saves"]:
            items = []
            for mid, p in plans.items():
                if p.get("decision") == "block":
                    continue
                item = {"material_id": mid, "date": p["date"], "time": p["time"]}
                cid = k["confirms"].get(mid)
                if cid:
                    item["confirmation_id"] = cid
                items.append(item)
            if items:
                return self._call("save_schedule_draft", {"items": items}, "校验都过了，写入草稿（不发布）")
            return self._finalize(k, plans, {})

        last = k["saves"][-1]
        if last.get("ok") is False and last.get("retryable") and len(k["saves"]) < 2:
            items = [i for i in (self._last_save_items(messages) or [])]
            if items:
                return self._call("save_schedule_draft", {"items": items}, "校验都过了，写入草稿（不发布）")

        saved_map = {}
        for res in k["saves"]:
            for s in res.get("saved") or []:
                saved_map[s["material_id"]] = s
        return self._finalize(k, plans, saved_map)

    @staticmethod
    def _last_save_items(messages: list[dict]) -> list[dict]:
        for m in reversed(messages):
            if m.get("role") == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    if tc["function"]["name"] == "save_schedule_draft":
                        return json.loads(tc["function"]["arguments"]).get("items") or []
        return []

    def _finalize(self, k: dict, plans: dict, saved_map: dict) -> LLMReply:
        decisions = []
        for mid, p in plans.items():
            if p.get("decision") == "block":
                if p.get("block_reason"):
                    reason = p["block_reason"]
                    pending = ["等待人工指定新的发布时间", "确认后重新送审排期"]
                elif p.get("missing"):
                    reason = f"必需字段缺失（{', '.join(p['missing'])}），外部素材库补查不到，需补件后复审"
                    pending = [f"向创作者补件：{f}" for f in p["missing"]] + ["补齐后重新送审"]
                else:
                    blocks = guards.blocking_risks(p["check"].get("risks") or [])
                    rules = [r["rule_id"] for r in blocks]
                    reason = f"存在阻断级风险 {', '.join(rules)}，不予排期：" + "；".join(r["reason"] for r in blocks)
                    pending = [f"[{r['rule_id']}] {r['name']}：需人工复审或修改后重排" for r in blocks]
                decisions.append({"material_id": mid, "decision": "block", "reason": reason,
                                  "pending_items": pending})
                continue

            s = saved_map.get(mid)
            if s is None:
                decisions.append({
                    "material_id": mid, "decision": "save_failed",
                    "planned_date": p["date"], "planned_time": p["time"],
                    "reason": "草稿保存失败（STORE_WRITE_FAILED），已重试一次仍失败，需运维介入",
                })
                continue

            reason = f"无阻断风险，{p['date']} {p['time']} 空闲并已写入草稿"
            if p["deviates"]:
                if mid in k["approved"]:
                    reason = (f"用户要求 {p['want']} 已被占用，改用 {p['time']}；"
                              f"该偏离已获人工批准（{k['confirms'].get(mid)}）")
                    p["decision"] = "schedule"
                else:
                    reason = f"用户要求 {p['want']} 已被占用，改为 {p['time']} 并已登记待确认"
            elif p.get("needs_conf"):
                reason = f"存在警告级风险，已登记待确认后写入 {p['time']} 草稿"
            decisions.append({
                "material_id": mid,
                "decision": p["decision"],
                "planned_date": p["date"],
                "planned_time": p["time"],
                "reason": reason,
            })
        return self._call("finalize", {"decisions": decisions})
