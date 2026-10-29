"""报告层：只认工具回执。

这是整套设计里最关键的一段。模型可以随便说"我已排好"，但报告里的
`review_status` 与 `draft.status` 全部由**规则事实**和**工具回执**推导，
模型只贡献 `decision` 与 `reason` 文字。

于是"虚报保存成功"会被交叉校验抓住并降级为 failed，
"越权发布"则连字段都不存在。
"""

from __future__ import annotations

import re

from . import guards
from .models import missing_fields, publish_date_of, publish_time_of
from .trace import now_iso

_CONF_ID_RE = re.compile(r"C\d{3}")

# 触发运行级 incomplete 的一致性缺陷
CRITICAL_FLAGS = {
    "CLAIMS_SCHEDULED_WITHOUT_RECEIPT",
    "ILLEGAL_DRAFT_FOR_BLOCKED_MATERIAL",
    "DEVIATION_WITHOUT_CONFIRMATION",
    "MISSING_DECISION",
    "SAVE_FAILED_WITHOUT_ATTEMPT",
    "FORGED_CONSENT",
}


def _draft_index(store) -> dict[str, dict]:
    return {d["draft_id"]: d for d in store.state["drafts"]}


def _save_receipts(ledger: list[dict]) -> dict[str, dict]:
    """回放 ledger 里的 save_schedule_draft，得到每个素材**最后一次**保存结果。"""
    out: dict[str, dict] = {}
    for rec in ledger:
        if rec.get("tool") != "save_schedule_draft":
            continue
        result = rec.get("result") or {}
        items = (rec.get("args") or {}).get("items") or []
        saved = {s["material_id"]: s for s in (result.get("saved") or [])}
        rejected = {r["material_id"]: r for r in (result.get("rejected") or [])}

        for item in items:
            if not isinstance(item, dict):
                continue          # 畸形参数不会写入成功，账本回放时跳过
            mid = str(item.get("material_id"))
            if mid in saved:
                s = saved[mid]
                out[mid] = {
                    "status": "deduplicated" if s.get("deduplicated") else "saved",
                    "draft_id": s.get("draft_id"),
                    "revision": s.get("revision"),
                    "date": s.get("date"),
                    "time": s.get("time"),
                    "error_code": None,
                    "message": None,
                }
            elif mid in rejected:
                r = rejected[mid]
                out[mid] = {
                    "status": "failed",
                    "draft_id": None,
                    "revision": None,
                    "date": item.get("date"),
                    "time": item.get("time"),
                    "error_code": r.get("error_code"),
                    "message": r.get("reason"),
                }
            elif result.get("ok") is False:
                out[mid] = {
                    "status": "failed",
                    "draft_id": None,
                    "revision": None,
                    "date": item.get("date"),
                    "time": item.get("time"),
                    "error_code": result.get("error_code", "SAVE_FAILED"),
                    "message": result.get("message"),
                }
    return out


def build_report(store, toolbox, spec: dict, meta) -> dict:
    expected: list[str] = list(spec.get("materials") or [])
    decisions: dict[str, dict] = {}
    for d in toolbox.final_decisions or []:
        decisions[d["material_id"]] = d  # 同一素材多次 finalize，以最后一条为准

    receipts = _save_receipts(toolbox.ledger)
    drafts_by_id = _draft_index(store)

    # 本次运行里真正产生的确认单（避免把上一轮遗留的待确认项算进来）
    run_conf_ids = {
        rec["result"]["confirmation_id"]
        for rec in toolbox.ledger
        if rec.get("tool") == "request_confirmation"
        and (rec.get("result") or {}).get("ok")
        and rec["result"].get("confirmation_id")
    }
    confs: dict[str, list[dict]] = {}
    for c in store.state["confirmations"]:
        confs.setdefault(c["material_id"], []).append(c)

    items: list[dict] = []

    for mid in expected:
        mat = store.materials_by_id.get(mid)
        if mat is None:
            items.append(
                {
                    "material_id": mid,
                    "title": None,
                    "review_status": "blocked",
                    "risk_reasons": [f"素材 {mid} 不存在"],
                    "risks": [],
                    "requested_slot": None,
                    "suggested_slot": None,
                    "draft": {"status": "not_attempted", "draft_id": None, "revision": None,
                              "deduplicated": False, "error_code": "MATERIAL_NOT_FOUND", "message": None},
                    "consent_status": "not_required",
                    "effective_status": "not_scheduled",
                    "confirmations": [],
                    "pending_confirmations": [],
                    "decision": None,
                    "decision_reason": None,
                    "consistency_flags": ["MISSING_DECISION"],
                }
            )
            continue

        req_date = publish_date_of(mat)
        req_time = publish_time_of(mat)
        req_at = mat.get("requested_publish_at")
        risks = guards.evaluate_rules(store, mat, req_date, req_time)
        missing = missing_fields(mat, store.required_fields)

        if missing:
            review_status = "missing_info"
        elif guards.blocking_risks(risks):
            review_status = "blocked"
        elif guards.warn_risks(risks):
            review_status = "risk"
        else:
            review_status = "ready"

        decision = decisions.get(mid)
        rec = receipts.get(mid)
        draft = dict(rec) if rec else {
            "status": "not_attempted", "draft_id": None, "revision": None,
            "date": None, "time": None, "error_code": None, "message": None,
        }
        draft["deduplicated"] = draft["status"] == "deduplicated"

        # 建议时段：真存过就以草稿为准，否则用模型 decision 里的计划
        suggested = None
        sug_dt: tuple[str, str] | None = None
        if draft["status"] in ("saved", "deduplicated") and draft.get("date"):
            sug_dt = (draft["date"], draft["time"])
        elif decision and decision.get("planned_time"):
            sug_dt = (decision.get("planned_date") or req_date, decision["planned_time"])
        if sug_dt:
            suggested = f"{sug_dt[0]} {sug_dt[1]}"
        slot_changed = bool(sug_dt and sug_dt != (req_date, req_time))

        flags: list[str] = []
        if decision is None:
            flags.append("MISSING_DECISION")
        elif decision["decision"] == "schedule" and draft["status"] not in ("saved", "deduplicated"):
            # 模型声称已排期，但没有任何成功回执 → 降级为失败，不允许虚报
            flags.append("CLAIMS_SCHEDULED_WITHOUT_RECEIPT")
            draft["status"] = "failed"
            draft["error_code"] = draft.get("error_code") or "NO_SAVE_RECEIPT"
            draft["message"] = draft.get("message") or "模型声称已排期，但工具账本中没有成功保存记录。"
        elif decision["decision"] == "no_action":
            if draft["status"] in ("saved", "deduplicated"):
                flags.append("CONTRADICTORY_DECISION")
        elif decision["decision"] == "save_failed":
            if draft["status"] == "not_attempted":
                # 一次都没尝试保存却宣称保存失败，同样是编造
                flags.append("SAVE_FAILED_WITHOUT_ATTEMPT")
                draft["status"] = "failed"
                draft["error_code"] = "NO_SAVE_ATTEMPT"
            elif draft["status"] in ("saved", "deduplicated"):
                flags.append("CONTRADICTORY_DECISION")

        if review_status in ("blocked", "missing_info") and draft["status"] in ("saved", "deduplicated"):
            flags.append("ILLEGAL_DRAFT_FOR_BLOCKED_MATERIAL")

        draft_rec = drafts_by_id.get(draft.get("draft_id") or "")
        if draft_rec and draft_rec.get("deviates_from_request") and not draft_rec.get("confirmation_id"):
            flags.append("DEVIATION_WITHOUT_CONFIRMATION")

        own_conf = (draft_rec or {}).get("confirmation_id")
        pending = [
            {"confirmation_id": c["confirmation_id"], "question": c["question"],
             "reason": c["reason"], "status": c["status"], "options": c.get("options", []),
             "source": "confirmation"}
            for c in confs.get(mid, [])
            if c.get("status") == "pending"
            and (c["confirmation_id"] in run_conf_ids or c["confirmation_id"] == own_conf)
        ]
        if decision:
            for text in decision.get("pending_items") or []:
                if not text or text in [p["question"] for p in pending]:
                    continue
                # 模型常把已有的确认单复述一遍，指到同一个 confirmation_id 就视为重复
                if set(_CONF_ID_RE.findall(text)) & {p["confirmation_id"] for p in pending if p["confirmation_id"]}:
                    continue
                pending.append({"confirmation_id": None, "question": text, "reason": "模型提出的待确认项",
                                "status": "pending", "options": [], "source": "model"})

        # 交付要求「待确认项」不能空着：模型没写时，从规则事实派生一条可执行的待办，
        # 并标注来源，避免与模型自述混淆。
        if not pending and review_status == "missing_info":
            pending.append({
                "confirmation_id": None,
                "question": "需补齐字段：" + "、".join(missing),
                "reason": "必需字段缺失，无法判定合规性（R7），补齐后重新送审",
                "status": "pending", "options": [], "source": "derived_from_rules",
            })
        elif not pending and review_status == "blocked":
            blocks = guards.blocking_risks(risks)
            pending.append({
                "confirmation_id": None,
                "question": "需人工复审：" + "；".join(f"[{r['rule_id']}] {r['name']}" for r in blocks),
                "reason": "存在阻断级风险，须人工处置后才能重排",
                "status": "pending", "options": [], "source": "derived_from_rules",
            })

        # ---- 同意状态：只从确认单的**人工答复**推导，模型口述不参与 ----
        rel_confs = [c for c in confs.get(mid, [])
                     if c["confirmation_id"] in run_conf_ids or c["confirmation_id"] == own_conf]
        own_rec = store.get_confirmation(own_conf) if own_conf else None
        # 该素材最近一次**有人答复过**的确认单：跨轮次看，否则"人已否决"这件事会丢
        latest_answered = next(
            (c for c in reversed(confs.get(mid, [])) if c.get("answer")), None)
        if latest_answered and latest_answered.get("answer") == "reject":
            # 人否决过（哪怕本轮没写草稿），同意状态就是 rejected，不能报成 not_required
            consent_status = "rejected"
            if not rel_confs:
                rel_confs = [latest_answered]
        elif not slot_changed:
            consent_status = "not_required"
        elif own_rec is not None and own_rec.get("answer") == "accept":
            consent_status = "granted"
        elif own_rec is not None and own_rec.get("answer") == "reject":
            consent_status = "rejected"
        else:
            consent_status = "pending"
        if own_rec and own_rec.get("answer") == "accept" and not own_rec.get("answered_by"):
            # answer 只有 apply_answers 能写，正常不会走到这里；留着防未来改动把口子放开
            flags.append("FORGED_CONSENT")
            consent_status = "pending"

        # 事实推导的结论状态，与模型自述的 decision 并列输出，两者不一致即暴露出来。
        # 注意：consent_status 只描述"时段偏离是否获准"，警告级风险也需要人确认，
        # 所以这里还要看是否仍有未答复的待确认项。
        if draft["status"] in ("saved", "deduplicated"):
            if consent_status == "rejected":
                effective_status = "blocked"
            elif consent_status == "granted":
                effective_status = "scheduled"
            elif consent_status == "pending" or pending:
                effective_status = "pending_confirmation"
            else:
                effective_status = "scheduled"
        elif draft["status"] == "failed":
            effective_status = "save_failed"
        elif review_status in ("blocked", "missing_info") or consent_status == "rejected":
            effective_status = "blocked"
        else:
            effective_status = "not_scheduled"
        if decision and decision["decision"] == "pending_confirmation" and effective_status == "scheduled":
            flags.append("STALE_PENDING_DECISION")

        conf_view = [
            {
                "confirmation_id": c["confirmation_id"],
                "question": c["question"],
                "status": c["status"],
                "issued_by": c.get("issued_by", "agent"),
                "answer": c.get("answer"),
                "answered_by": c.get("answered_by"),
                "answered_at": c.get("answered_at"),
                "answer_note": c.get("answer_note"),
            }
            for c in rel_confs
        ]

        items.append(
            {
                "material_id": mid,
                "title": mat.get("title"),
                "review_status": review_status,
                "risk_reasons": [f"[{r['rule_id']}] {r['reason']}" for r in risks],
                "risks": risks,
                "requested_slot": req_at,
                "suggested_slot": suggested,
                "slot_changed": slot_changed,
                "consent_status": consent_status,
                "effective_status": effective_status,
                "confirmations": conf_view,
                "draft": {
                    "status": draft["status"],
                    "draft_id": draft.get("draft_id"),
                    "revision": draft.get("revision"),
                    "deduplicated": draft.get("deduplicated", False),
                    "error_code": draft.get("error_code"),
                    "message": draft.get("message"),
                },
                "pending_confirmations": pending,
                "decision": decision["decision"] if decision else None,
                "decision_reason": decision.get("reason") if decision else None,
                "consistency_flags": flags,
            }
        )

    all_flags = [f for item in items for f in item["consistency_flags"]]
    critical = sorted({f for f in all_flags if f in CRITICAL_FLAGS})
    completed = meta.stop_reason == "finalized" and not critical

    counts = {k: sum(1 for i in items if i["review_status"] == k)
              for k in ("ready", "risk", "missing_info", "blocked")}
    draft_counts = {k: sum(1 for i in items if i["draft"]["status"] == k)
                    for k in ("saved", "deduplicated", "failed", "not_attempted")}

    return {
        "run_id": meta.run_id,
        "mode": meta.mode,
        "model": meta.model,
        "status": "completed" if completed else "incomplete",
        "stop_reason": meta.stop_reason,
        "started_at": meta.started_at,
        "finished_at": now_iso(),
        "publish_state": "draft_only",
        "batch": {
            "request_id": spec.get("request_id"),
            "operator": spec.get("operator"),
            "focus_date": spec.get("focus_date"),
            "confirm_answers": (spec.get("confirm_answers") or {}).get("applied") or [],
        },
        "budget": {
            "steps_used": meta.steps,
            "max_steps": meta.max_steps,
            "tool_calls_used": meta.tool_calls,
            "max_tool_calls": meta.max_tool_calls,
            "tokens": meta.tokens,
        },
        "materials": items,
        "summary": {
            "total": len(items),
            **counts,
            "drafts_saved": draft_counts["saved"],
            "drafts_deduplicated": draft_counts["deduplicated"],
            "drafts_failed": draft_counts["failed"],
            "drafts_not_attempted": draft_counts["not_attempted"],
            "pending_confirmations": sum(len(i["pending_confirmations"]) for i in items),
            "consent_granted": sum(1 for i in items if i["consent_status"] == "granted"),
            "consent_pending": sum(1 for i in items if i["consent_status"] == "pending"),
            "consent_rejected": sum(1 for i in items if i["consent_status"] == "rejected"),
            "critical_flags": critical,
        },
    }
