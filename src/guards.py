"""硬护栏：规则引擎 + 排期保存校验。

这里是「模型不能越权」的地方。所有函数都是纯函数，输入素材/排期快照，输出结构化风险或拒绝原因，
不依赖模型说了什么。

关键点：规则在 **save 时按最终时段重算**。模型先 check_rules 再改时段保存，
不会被"先查后改"的时序绕过（防 TOCTOU）。
"""

from __future__ import annotations

from typing import Any

from .models import (
    field_present,
    get_field,
    missing_fields,
    norm_time,
    publish_date_of,
    publish_time_of,
    _MISSING,
)

BLOCK = "block"
WARN = "warn"


# ---------------------------------------------------------------- 规则求值

def _day_occupant_materials(store, date: str, exclude_material_id: str | None) -> list[dict]:
    """某日已占用时段的素材对象（用于同日互斥类规则）。"""
    seen: set[str] = set()
    out: list[dict] = []
    for entry in store.schedule_entries():
        if entry["date"] != date:
            continue
        mid = entry["material_id"]
        if mid == exclude_material_id or mid in seen:
            continue
        mat = store.materials_by_id.get(mid)
        if mat is not None:
            seen.add(mid)
            out.append(mat)
    return out


def _r_license_unknown(store, mat, rule, ctx) -> dict | None:
    lic = get_field(mat, "rights.license")
    if lic is _MISSING or lic in (None, "", "unknown"):
        return _risk(rule, "版权来源为空或 unknown，无法确认可商用授权。", {"rights.license": None if lic is _MISSING else lic})
    return None


def _r_license_expired(store, mat, rule, ctx) -> dict | None:
    expires = get_field(mat, "rights.expires_on")
    planned = ctx["planned_date"]
    if expires is _MISSING or not expires or not planned:
        return None
    if str(expires) < str(planned):
        return _risk(rule, f"授权到期日 {expires} 早于拟发布日 {planned}，授权已失效。",
                     {"expires_on": expires, "planned_date": planned})
    return None


def _r_duration_exceeded(store, mat, rule, ctx) -> dict | None:
    dur = get_field(mat, "duration_sec")
    limit = rule.get("limit_sec", 180)
    if dur is _MISSING or dur is None:
        return None
    if float(dur) > float(limit):
        return _risk(rule, f"素材时长 {dur} 秒超过阈值 {limit} 秒，需运营确认是否切条或转长视频池。",
                     {"duration_sec": dur, "limit_sec": limit})
    return None


def _r_creator_daily_cap(store, mat, rule, ctx) -> dict | None:
    limit = rule.get("limit", 2)
    same = [m for m in ctx["occupants"] if m.get("creator") == mat.get("creator")]
    if len(same) >= limit:
        return _risk(rule, f"创作者「{mat.get('creator')}」当日已排 {len(same)} 条（上限 {limit}），会互抢流量。",
                     {"creator": mat.get("creator"), "count": len(same), "limit": limit,
                      "same_day_materials": [m["material_id"] for m in same]})
    return None


def _r_brand_conflict(store, mat, rule, ctx) -> dict | None:
    mine = get_field(mat, "content.sponsor")
    mine = None if mine is _MISSING else mine
    rivals = get_field(mat, "content.sponsor_competitors")
    rivals = [] if rivals is _MISSING else list(rivals or [])

    hits: list[dict] = []
    for other in ctx["occupants"]:
        o_sponsor = get_field(other, "content.sponsor")
        o_rivals = get_field(other, "content.sponsor_competitors")
        o_rivals = [] if o_rivals is _MISSING else list(o_rivals or [])
        if mine and mine in o_rivals:
            hits.append({"material_id": other["material_id"], "sponsor": o_sponsor, "relation": "本素材品牌为其竞品"})
        elif o_sponsor and o_sponsor in rivals:
            hits.append({"material_id": other["material_id"], "sponsor": o_sponsor, "relation": "该素材品牌为本素材竞品"})
    if hits:
        return _risk(rule, f"同日存在竞品品牌竞排：{hits[0]['sponsor']}（{hits[0]['material_id']}）。", {"conflicts": hits})
    return None


def _r_sensitive_word(store, mat, rule, ctx) -> dict | None:
    text = " ".join(
        str(x)
        for x in [mat.get("title", ""), get_field(mat, "content.text")]
        if isinstance(x, str)
    )
    hits = [w for w in store.sensitive_words if w and w in text]
    if hits:
        return _risk(rule, f"文案命中广告法敏感词：{'、'.join(hits)}，须改写后复审。", {"hits": hits})
    return None


def _r_required_fields(store, mat, rule, ctx) -> dict | None:
    missing = missing_fields(mat, store.required_fields)
    if missing:
        return _risk(rule, f"必需字段缺失：{'、'.join(missing)}。", {"missing": missing})
    return None


def _r_already_scheduled(store, mat, rule, ctx) -> dict | None:
    mid = mat["material_id"]
    hits = [e for e in store.schedule_entries()
            if e["material_id"] == mid and e["source"] in ("baseline", "input")]
    if hits or mat.get("status") == "scheduled":
        slot = f"{hits[0]['date']} {hits[0]['time']}" if hits else str(mat.get("requested_publish_at", ""))
        return _risk(rule, f"该素材已在排期中（{slot}），不得自动重复排期，改期需运营确认。",
                     {"scheduled_at": slot})
    return None


_HANDLERS = {
    "license_unknown": _r_license_unknown,
    "license_expired": _r_license_expired,
    "duration_exceeded": _r_duration_exceeded,
    "creator_daily_cap": _r_creator_daily_cap,
    "brand_conflict": _r_brand_conflict,
    "sensitive_word": _r_sensitive_word,
    "required_fields": _r_required_fields,
    "already_scheduled": _r_already_scheduled,
}


def _risk(rule: dict, reason: str, evidence: dict) -> dict:
    return {
        "rule_id": rule["id"],
        "name": rule["name"],
        "severity": rule["severity"],
        "reason": reason,
        "evidence": evidence,
    }


def evaluate_rules(
    store,
    material: dict,
    planned_date: str | None = None,
    planned_time: str | None = None,
) -> list[dict]:
    """按给定（或素材要求的）日期求值全部规则，返回风险列表。"""
    date = planned_date or publish_date_of(material)
    ctx = {
        "planned_date": date,
        "planned_time": planned_time or publish_time_of(material),
        "occupants": _day_occupant_materials(store, date, material["material_id"]),
    }
    risks: list[dict] = []
    for rule in store.rules:
        handler = _HANDLERS.get(rule["type"])
        if handler is None:
            continue
        hit = handler(store, material, rule, ctx)
        if hit is not None:
            risks.append(hit)
    return risks


def blocking_risks(risks: list[dict]) -> list[dict]:
    return [r for r in risks if r["severity"] == BLOCK]


def warn_risks(risks: list[dict]) -> list[dict]:
    return [r for r in risks if r["severity"] == WARN]


# ---------------------------------------------------------------- 保存校验

def validate_save(store, items: list[dict]) -> tuple[list[dict], list[dict]]:
    """逐条校验待保存草稿，返回 (accepted, rejected)。

    拒绝码：
      MATERIAL_NOT_FOUND / INVALID_SLOT / BLOCKING_RISK / SLOT_OCCUPIED
      DEVIATION_WITHOUT_CONFIRMATION / UNKNOWN_CONFIRMATION / DUPLICATE_ITEM
      CONFIRMATION_REJECTED（人工已拒绝该偏离方案）
    """
    accepted: list[dict] = []
    rejected: list[dict] = []
    seen: set[str] = set()

    for item in items:
        mid = str(item.get("material_id") or "")
        date = str(item.get("date") or "")
        time = norm_time(item.get("time") or "")
        conf: dict | None = None

        def reject(code: str, reason: str, **extra: Any) -> None:
            rejected.append({"material_id": mid, "error_code": code, "reason": reason, **extra})

        mat = store.materials_by_id.get(mid)
        if mat is None:
            reject("MATERIAL_NOT_FOUND", f"素材 {mid} 不存在。")
            continue
        if mid in seen:
            reject("DUPLICATE_ITEM", f"同一请求内重复提交了素材 {mid}，只处理第一条。")
            continue
        seen.add(mid)

        if not date or not time:
            reject("INVALID_SLOT", "date 与 time 必填。")
            continue
        if time not in store.slot_grid:
            reject("INVALID_SLOT", f"时段 {time} 不在发布时段网格内（{', '.join(store.slot_grid)}）。")
            continue

        risks = evaluate_rules(store, mat, date, time)
        blocks = blocking_risks(risks)
        if blocks:
            reject(
                "BLOCKING_RISK",
                "存在阻断级风险，不允许自动排期：" + "；".join(f"[{r['rule_id']}] {r['reason']}" for r in blocks),
                risks=[r["rule_id"] for r in blocks],
            )
            continue

        occ = store.occupancy(date, exclude_material_id=mid)
        if time in occ:
            holder = occ[time]
            reject(
                "SLOT_OCCUPIED",
                f"{date} {time} 已被素材 {holder['material_id']} 占用（来源 {holder['source']}），请改选其他时段。",
                occupied_by=holder["material_id"],
            )
            continue

        requested_at = mat.get("requested_publish_at") or ""
        deviates = bool(date + "T" + time != requested_at)
        cid = item.get("confirmation_id")
        if deviates:
            if not cid:
                reject(
                    "DEVIATION_WITHOUT_CONFIRMATION",
                    f"拟排 {date} {time} 与用户要求 {requested_at} 不一致，未经确认不能写入草稿。"
                    "请先调用 request_confirmation 获取 confirmation_id。",
                    requested_at=requested_at,
                )
                continue
            conf = store.get_confirmation(str(cid))
            if conf is None:
                reject("UNKNOWN_CONFIRMATION", f"确认单 {cid} 不存在。")
                continue
            if conf["material_id"] != mid:
                reject("UNKNOWN_CONFIRMATION", f"确认单 {cid} 属于素材 {conf['material_id']}，不能用于 {mid}。")
                continue
            # 人工明确拒绝过的方案不允许再落草稿（此前这是个漏洞）
            if conf.get("answer") == "reject":
                reject(
                    "CONFIRMATION_REJECTED",
                    f"确认单 {cid} 已被人为拒绝（{conf.get('answered_by')}），"
                    f"不能再按 {date} {time} 保存草稿；请改选时段或阻断。",
                    confirmation_id=str(cid),
                )
                continue

        accepted.append(
            {
                "material_id": mid,
                "date": date,
                "time": time,
                "requested_at": requested_at,
                "deviates_from_request": deviates,
                "confirmation_id": str(cid) if cid else None,
                "confirmation_answer": (conf.get("answer") if deviates and cid else None),
            }
        )

    return accepted, rejected
