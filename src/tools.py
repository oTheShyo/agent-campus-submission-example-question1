"""7 个业务工具 + OpenAI function schema + 调用账本（ledger）。

账本是「报告只认回执」的数据基础：每次工具调用的入参与返回都记进去，
最终报告的审核状态/草稿状态从账本推导，而不是从模型的话里推导。
"""

from __future__ import annotations

import json
from typing import Any

from . import guards
from .models import (
    ToolRejected,
    flatten_fields,
    missing_fields,
    norm_time,
    publish_date_of,
)
from .store import DataStore

DECISIONS = ("schedule", "pending_confirmation", "block", "save_failed", "no_action")


def _dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _as_list(value: Any) -> list:
    """模型偶尔把数组写成字符串，这里统一兜住。

    两种真实遇到过的情况：
    1. `["M03"]` 被写成 `"M03"` —— 单个值当数组；
    2. `[{...}]` 被整个 JSON 序列化成字符串 —— 二次编码，需要再解一次。
    """
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("[") or text.startswith("{"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list):
                    return parsed
                if isinstance(parsed, dict):
                    return [parsed]
            except json.JSONDecodeError:
                pass
        return [value]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


TOOLS_SPEC: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_pending_materials",
            "description": "列出待处理的素材（status=pending）。这是排期工作的入口。",
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "可选，只看某一天的素材，格式 YYYY-MM-DD"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_material",
            "description": (
                "读取素材元数据。返回 flat_fields（拍平后的字段）、missing_required（必需字段缺口）、"
                "missing_optional（可选字段缺口）、already_scheduled（是否已在排期中）。"
                "注意 requested_publish_at 是用户的发布时间要求，只读，任何情况下不得修改。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "material_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "素材 ID 列表，可一次读多条",
                    }
                },
                "required": ["material_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lookup_supplement",
            "description": (
                "到外部素材库补查缺失字段。素材信息缺失时必须先补查，补不到再决定阻断。"
                "返回逐字段的 found/value/source。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "material_id": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "要补查的字段路径，如 rights.license、duration_sec",
                    },
                },
                "required": ["material_id", "fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_rules",
            "description": (
                "按审核规则检查素材风险。返回 risks（rule_id/severity/reason）与 blocking。"
                "severity=block 表示不得自动排期；severity=warn 表示可排期但需运营确认。"
                "可选 planned_date：按拟发布日评估（授权到期、竞品竞排等规则与日期相关）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "material_ids": {"type": "array", "items": {"type": "string"}},
                    "planned_date": {"type": "string", "description": "可选，YYYY-MM-DD"},
                },
                "required": ["material_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_slots",
            "description": (
                "查询某天各时段的占用情况。返回 free（空闲时段）与 occupied（已占用时段及占用方）。"
                "排期前必须调用，不要凭假设选择时段。"
                "for_material_ids 传本次要排期的素材：这些素材自己已有的草稿不算占用（改期时需要）。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "from_time": {"type": "string", "description": "可选，只看该时间及之后的时段，HH:MM"},
                    "for_material_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选，本次要排期的素材 ID，用于排除它们自己的旧草稿",
                    },
                },
                "required": ["date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_confirmation",
            "description": (
                "创建一条待运营确认项。以下两种情况必须先调用它拿到 confirmation_id："
                "① 拟排时段与用户要求不一致；② 素材存在 warn 级风险仍需排期。"
                "本工具只登记待确认项，不代表用户已同意。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "material_id": {"type": "string"},
                    "question": {"type": "string", "description": "要问运营/用户的问题"},
                    "options": {"type": "array", "items": {"type": "string"}, "description": "可选方案"},
                    "reason": {"type": "string", "description": "为什么需要确认"},
                },
                "required": ["material_id", "question", "reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "save_schedule_draft",
            "description": (
                "保存排期草稿（只存草稿，不发布）。工具会二次校验：时段是否仍空闲、是否有阻断风险、"
                "与用户要求不一致时是否带 confirmation_id。被拒绝时返回 rejected 明细，"
                "你应修正参数后重试或改走阻断。同一素材同一目标时段的重复保存只会产生一份草稿。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "material_id": {"type": "string"},
                                "date": {"type": "string"},
                                "time": {"type": "string", "description": "HH:MM，必须在发布时段网格内"},
                                "confirmation_id": {
                                    "type": "string",
                                    "description": "与用户要求不一致时必填，来自 request_confirmation",
                                },
                            },
                            "required": ["material_id", "date", "time"],
                        },
                    }
                },
                "required": ["items"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize",
            "description": (
                "提交最终结论并结束本次运行。每项素材一条，decision 只能是 "
                "schedule（草稿已保存）/ pending_confirmation（待人工确认）/ "
                "block（阻断不排期）/ save_failed（审核通过但草稿保存失败）/ "
                "no_action（素材已在排期中或本次无需操作，未写草稿）。"
                "reason 写简要决策依据。不要在还有素材未处理时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "decisions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "material_id": {"type": "string"},
                                "decision": {"type": "string", "enum": list(DECISIONS)},
                                "planned_date": {"type": "string"},
                                "planned_time": {"type": "string"},
                                "reason": {"type": "string"},
                                "pending_items": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "还需要人工确认的事项",
                                },
                            },
                            "required": ["material_id", "decision", "reason"],
                        },
                    }
                },
                "required": ["decisions"],
            },
        },
    },
]

TOOL_NAMES = [t["function"]["name"] for t in TOOLS_SPEC]


class ToolBox:
    """工具集合。每次调用都写进 ledger，供 verifier 与 trace 使用。"""

    def __init__(self, store: DataStore) -> None:
        self.store = store
        self.ledger: list[dict] = []
        self.final_decisions: list[dict] | None = None
        self.call_count = 0

    # ---------- 分发 ----------

    def dispatch(self, name: str, args: dict) -> dict:
        self.call_count += 1
        if "__parse_error__" in (args or {}):
            result = {
                "ok": False,
                "error_code": "INVALID_JSON",
                "message": "工具参数不是合法 JSON（很可能被长度截断）。请重新输出一次完整、闭合的参数。",
            }
            self.ledger.append({"tool": name, "args": args or {}, "result": result})
            return result
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:
            result: dict = {"ok": False, "error_code": "UNKNOWN_TOOL", "message": f"未知工具 {name}"}
        else:
            try:
                result = handler(**(args or {}))
            except ToolRejected as exc:
                result = {"ok": False, "error_code": exc.code, "message": exc.message, **exc.detail}
            except TypeError as exc:
                result = {"ok": False, "error_code": "BAD_ARGUMENTS", "message": f"参数不合法：{exc}"}
            except Exception as exc:
                # 工具永远不应该把循环搞崩。畸形参数在这里变成可修正的观察结果，
                # 模型看到后可以改参数重试；同时保留异常类型便于排查。
                result = {
                    "ok": False,
                    "error_code": "TOOL_ERROR",
                    "message": f"工具内部错误（参数结构可能不合法）：{type(exc).__name__}: {exc}",
                }
        self.ledger.append({"tool": name, "args": args or {}, "result": result})
        return result

    # ---------- 各工具实现 ----------

    def _tool_list_pending_materials(self, date: str | None = None) -> dict:
        items = [m for m in self.store.materials if m.get("status") == "pending"]
        if date:
            items = [m for m in items if publish_date_of(m) == date]
        return {
            "ok": True,
            "count": len(items),
            "materials": [
                {
                    "material_id": m["material_id"],
                    "title": m.get("title"),
                    "requested_publish_at": m.get("requested_publish_at"),
                    "hard_requirement": m.get("hard_requirement", True),
                    "creator": m.get("creator"),
                }
                for m in items
            ],
        }

    def _tool_read_material(self, material_ids: list[str] | None = None) -> dict:
        ids = [str(x) for x in _as_list(material_ids)]
        if not ids:
            raise ToolRejected("BAD_ARGUMENTS", "material_ids 不能为空")
        out, unknown = [], []
        for mid in ids:
            mat = self.store.materials_by_id.get(str(mid))
            if mat is None:
                unknown.append(str(mid))
                continue
            scheduled = any(
                e["material_id"] == mat["material_id"] and e["source"] in ("baseline", "input")
                for e in self.store.schedule_entries()
            )
            out.append(
                {
                    "material_id": mat["material_id"],
                    "flat_fields": flatten_fields(mat),
                    "missing_required": missing_fields(mat, self.store.required_fields),
                    "missing_optional": [
                        f for f in self.store.optional_fields if flatten_fields(mat).get(f) in (None, "")
                    ],
                    "already_scheduled": scheduled or mat.get("status") == "scheduled",
                    # 让模型看得到历史确认单及其人工答复，重跑时才知道"已经有人同意了"
                    "confirmations": [
                        {
                            "confirmation_id": c["confirmation_id"],
                            "question": c["question"],
                            "status": c["status"],
                            "answer": c.get("answer"),
                            "answered_by": c.get("answered_by"),
                        }
                        for c in self.store.state["confirmations"]
                        if c["material_id"] == mat["material_id"]
                    ],
                }
            )
        return {"ok": True, "count": len(out), "materials": out, "unknown_ids": unknown}

    def _tool_lookup_supplement(self, material_id: str, fields: list[str] | None = None) -> dict:
        mat = self.store.materials_by_id.get(str(material_id))
        if mat is None:
            raise ToolRejected("MATERIAL_NOT_FOUND", f"素材 {material_id} 不存在")
        wanted = [str(x) for x in _as_list(fields)]
        if not wanted:
            raise ToolRejected("BAD_ARGUMENTS", "fields 不能为空")

        record = self.store.asset_index.get(str(material_id)) or {}
        available: dict = record.get("fields", {})
        results = []
        for field in wanted:
            if field in available and available[field] not in (None, ""):
                results.append(
                    {"field": field, "found": True, "value": available[field], "source": f"asset_index:{material_id}"}
                )
            else:
                results.append(
                    {
                        "field": field,
                        "found": False,
                        "value": None,
                        "source": None,
                        "note": "外部素材库无该字段记录" if available else "外部素材库无此素材记录",
                    }
                )
        return {
            "ok": True,
            "material_id": str(material_id),
            "results": results,
            "all_found": all(r["found"] for r in results),
        }

    def _tool_check_rules(self, material_ids: list[str] | None = None, planned_date: str | None = None) -> dict:
        ids = [str(x) for x in _as_list(material_ids)]
        if not ids:
            raise ToolRejected("BAD_ARGUMENTS", "material_ids 不能为空")
        out, unknown = [], []
        for mid in ids:
            mat = self.store.materials_by_id.get(str(mid))
            if mat is None:
                unknown.append(str(mid))
                continue
            risks = guards.evaluate_rules(self.store, mat, planned_date=planned_date)
            out.append(
                {
                    "material_id": mat["material_id"],
                    "planned_date": planned_date or publish_date_of(mat),
                    "risks": risks,
                    "blocking": bool(guards.blocking_risks(risks)),
                    "warn_count": len(guards.warn_risks(risks)),
                }
            )
        return {
            "ok": True,
            "materials": out,
            "any_blocking": any(m["blocking"] for m in out),
            "unknown_ids": unknown,
        }

    def _tool_query_slots(self, date: str, from_time: str | None = None,
                          for_material_ids: list[str] | None = None) -> dict:
        if not date:
            raise ToolRejected("BAD_ARGUMENTS", "date 必填")
        exclude = {str(x) for x in _as_list(for_material_ids)}
        occ = self.store.occupancy(date, exclude_ids=exclude)
        grid = self.store.slot_grid
        if from_time:
            start = norm_time(from_time)
            grid = [t for t in grid if t >= start] or list(self.store.slot_grid)
        free = [t for t in grid if t not in occ]
        return {
            "ok": True,
            "date": date,
            "excluded_own_drafts": sorted(exclude),
            "slot_grid": self.store.slot_grid,
            "free": free,
            "occupied": [
                {"time": t, "material_id": v["material_id"], "source": v["source"]}
                for t, v in sorted(occ.items())
            ],
        }

    def _tool_request_confirmation(
        self,
        material_id: str,
        question: str | None = None,
        reason: str | None = None,
        options: list[str] | None = None,
    ) -> dict:
        if str(material_id) not in self.store.materials_by_id:
            raise ToolRejected("MATERIAL_NOT_FOUND", f"素材 {material_id} 不存在")
        if not question or not reason:
            raise ToolRejected("BAD_ARGUMENTS", "question 与 reason 必填")
        rec = self.store.add_confirmation(str(material_id), question, [str(o) for o in _as_list(options)], reason)
        return {
            "ok": True,
            "confirmation_id": rec["confirmation_id"],
            "material_id": rec["material_id"],
            "status": "pending",
            "issued_by": "agent",
            "answer": None,
            "note": (
                "已登记待确认项。该凭证只允许你带着偏离写入草稿，不代表用户已同意——"
                "同意与否只能由运行之外的人类答复（--confirm-file）决定，你无法自行改变 answer。"
            ),
        }

    def _tool_save_schedule_draft(self, items: list[dict] | None = None) -> dict:
        items = _as_list(items)
        if not items:
            raise ToolRejected("BAD_ARGUMENTS", "items 不能为空")
        if any(not isinstance(it, dict) for it in items):
            raise ToolRejected(
                "BAD_ARGUMENTS",
                "items 必须是对象数组，每项形如 {material_id, date, time[, confirmation_id]}",
            )

        accepted, rejected = guards.validate_save(self.store, items)
        if not accepted:
            return {
                "ok": False,
                "error_code": "ALL_ITEMS_REJECTED",
                "message": "没有任何条目通过护栏校验，未写入草稿。",
                "saved": [],
                "rejected": rejected,
            }
        try:
            saved = self.store.save_drafts(accepted)
        except Exception as exc:  # StoreWriteError
            return {
                "ok": False,
                "error_code": getattr(exc, "code", "STORE_WRITE_FAILED"),
                "message": f"草稿保存失败：{exc}",
                "saved": [],
                "rejected": rejected,
                "retryable": True,
            }
        # 把人工答复状态回填进保存回执，让模型看得到"有没有人真的同意"
        amap = {a["material_id"]: a for a in accepted}
        for s in saved:
            a = amap.get(s.get("material_id"))
            s["confirmation_answer"] = a.get("confirmation_answer") if a else None
            s["confirmation_id"] = a.get("confirmation_id") if a else None
        return {
            "ok": True,
            "saved": saved,
            "rejected": rejected,
            "attempted": len(items),
            "note": "仅写入排期草稿，未发布，也未修改用户的发布时间要求。",
        }

    def _tool_finalize(self, decisions: list[dict] | None = None) -> dict:
        decisions = _as_list(decisions)
        if not decisions:
            raise ToolRejected("BAD_ARGUMENTS", "decisions 不能为空")
        cleaned, bad = [], []
        for d in decisions:
            if not isinstance(d, dict):
                bad.append({"material_id": None, "error": f"decision 必须是对象，收到 {type(d).__name__}"})
                continue
            mid = str(d.get("material_id") or "")
            decision = str(d.get("decision") or "")
            if mid not in self.store.materials_by_id:
                bad.append({"material_id": mid, "error": "MATERIAL_NOT_FOUND"})
                continue
            if decision not in DECISIONS:
                bad.append({"material_id": mid, "error": f"decision 必须是 {DECISIONS}"})
                continue
            cleaned.append(
                {
                    "material_id": mid,
                    "decision": decision,
                    "planned_date": d.get("planned_date"),
                    "planned_time": norm_time(d["planned_time"]) if d.get("planned_time") else None,
                    "reason": str(d.get("reason") or ""),
                    "pending_items": [str(x) for x in (d.get("pending_items") or [])],
                }
            )
        self.final_decisions = (self.final_decisions or []) + cleaned
        if not cleaned:
            # 一条都没解析出来时必须报失败。否则模型会以为提交成功，反复重发到步数上限。
            return {
                "ok": False,
                "error_code": "INVALID_DECISIONS",
                "message": (
                    "没有解析出任何合法 decision。decisions 必须是对象数组，"
                    '形如 [{"material_id":"M01","decision":"block","reason":"..."}]；'
                    "不要把数组整个序列化成字符串传进来。"
                ),
                "invalid": bad,
            }
        return {"ok": True, "accepted": len(cleaned), "invalid": bad}
