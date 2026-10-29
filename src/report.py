"""把报告渲染成人能读的样子（stdout 仍输出 JSON，人读版走 stderr）。"""

from __future__ import annotations

STATUS_CN = {
    "ready": "通过",
    "risk": "有警告级风险",
    "missing_info": "信息不足",
    "blocked": "阻断",
}

DRAFT_CN = {
    "saved": "草稿已保存",
    "deduplicated": "草稿已存在（幂等命中）",
    "failed": "保存失败",
    "not_attempted": "未尝试保存",
}


def render_human(report: dict) -> str:
    lines: list[str] = []
    lines.append("=" * 62)
    lines.append(f"批次 {report['batch'].get('request_id')}  模式={report['mode']}  模型={report['model']}")
    lines.append(f"运行状态 {report['status']}  结束原因 {report['stop_reason']}  "
                 f"步数 {report['budget']['steps_used']}/{report['budget']['max_steps']}  "
                 f"工具调用 {report['budget']['tool_calls_used']}/{report['budget']['max_tool_calls']}")
    lines.append(f"发布状态 {report['publish_state']}（仅草稿，未接任何发布接口）")
    lines.append("=" * 62)

    for item in report["materials"]:
        lines.append(f"[{item['material_id']}] {item.get('title') or ''}")
        lines.append(f"  审核状态 : {item['review_status']}（{STATUS_CN.get(item['review_status'], '')}）")
        risks = item["risk_reasons"]
        lines.append(f"  风险原因 : {'无' if not risks else ''}")
        for r in risks:
            lines.append(f"             - {r}")
        lines.append(f"  要求时段 : {item['requested_slot'] or '未提供'}")
        sug = item["suggested_slot"] or "无"
        tail = "  ← 已偏离用户要求，待确认" if item["slot_changed"] else ""
        lines.append(f"  建议时段 : {sug}{tail}")
        d = item["draft"]
        extra = ""
        if d["draft_id"]:
            extra = f"  {d['draft_id']} rev{d['revision']}"
        if d["error_code"]:
            extra += f"  错误码={d['error_code']}"
        lines.append(f"  草稿状态 : {d['status']}（{DRAFT_CN.get(d['status'], '')}）{extra}")
        if item["pending_confirmations"]:
            lines.append("  待确认项 :")
            for p in item["pending_confirmations"]:
                tag = p["confirmation_id"] or "模型补充"
                lines.append(f"             - [{tag}] {p['question']}")
        else:
            lines.append("  待确认项 : 无")
        if item["consistency_flags"]:
            lines.append(f"  ⚠ 一致性 : {', '.join(item['consistency_flags'])}")
        lines.append("")

    s = report["summary"]
    lines.append("-" * 62)
    lines.append(
        f"合计 {s['total']} 项：通过 {s['ready']} / 警告 {s['risk']} / 信息不足 {s['missing_info']} / 阻断 {s['blocked']}"
    )
    lines.append(
        f"草稿：保存 {s['drafts_saved']} / 幂等命中 {s['drafts_deduplicated']} / "
        f"失败 {s['drafts_failed']} / 未尝试 {s['drafts_not_attempted']}；待确认 {s['pending_confirmations']} 项"
    )
    if s["critical_flags"]:
        lines.append(f"⚠ 关键一致性缺陷：{', '.join(s['critical_flags'])}")
    if report.get("trace_path"):
        lines.append(f"trace: {report['trace_path']}")
    return "\n".join(lines)
