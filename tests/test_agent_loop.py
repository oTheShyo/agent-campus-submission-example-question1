"""端到端：用脚本化模型跑通 6 个验收场景 + 幂等复用。

脚本模拟"模型看到工具结果后调整动作"，断言的是**系统结论**而不是模型话术。
"""

import json
from pathlib import Path

from src.agent import Agent
from src.store import DataStore
from src.tools import ToolBox

from fakes import ScriptedLLM, tc

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def run_script(tmp_path, script, spec, fault=None, max_steps=12, max_tool_calls=16, repeat_last=False):
    store = DataStore(DATA_DIR, tmp_path / "state.json", extra_schedule=spec.get("extra_schedule"), fault=fault)
    box = ToolBox(store)
    llm = ScriptedLLM(script, repeat_last=repeat_last)
    agent = Agent(store, box, llm, trace_path=tmp_path / "trace.jsonl",
                  max_steps=max_steps, max_tool_calls=max_tool_calls, mode="mock")
    return agent.run(spec), store, box


def item(report, mid):
    return next(i for i in report["materials"] if i["material_id"] == mid)


# ---------------------------------------------------------------- 1 正常排期

def test_normal_two_materials(tmp_path):
    spec = {"request_id": "t-normal", "materials": ["M03", "M05"], "focus_date": "2026-11-01"}
    script = [
        [tc("list_pending_materials", {"date": "2026-11-01"})],
        [tc("read_material", {"material_ids": ["M03", "M05"]})],
        [tc("check_rules", {"material_ids": ["M03", "M05"]})],
        [tc("query_slots", {"date": "2026-11-01"})],
        [tc("save_schedule_draft", {"items": [
            {"material_id": "M03", "date": "2026-11-01", "time": "12:00"},
            {"material_id": "M05", "date": "2026-11-01", "time": "20:00"},
        ]})],
        [tc("finalize", {"decisions": [
            {"material_id": "M03", "decision": "schedule", "planned_date": "2026-11-01",
             "planned_time": "12:00", "reason": "无风险，要求的 12:00 空闲"},
            {"material_id": "M05", "decision": "schedule", "planned_date": "2026-11-01",
             "planned_time": "20:00", "reason": "无风险，要求的 20:00 空闲"},
        ]})],
    ]
    report, store, _ = run_script(tmp_path, script, spec)

    assert report["status"] == "completed"
    assert report["stop_reason"] == "finalized"
    for mid in ("M03", "M05"):
        assert item(report, mid)["review_status"] == "ready"
        assert item(report, mid)["draft"]["status"] == "saved"
        assert item(report, mid)["pending_confirmations"] == []
    assert report["summary"]["drafts_saved"] == 2
    assert len(store.state["drafts"]) == 2


# ---------------------------------------------------------------- 2 时段冲突（参考实例）

def test_slot_conflict_m01_suggests_1900(tmp_path):
    spec = {"request_id": "t-conflict", "materials": ["M01"], "focus_date": "2026-11-01"}
    script = [
        [tc("read_material", {"material_ids": ["M01"]})],
        [tc("check_rules", {"material_ids": ["M01"]})],
        [tc("query_slots", {"date": "2026-11-01"})],
        [tc("save_schedule_draft", {"items": [{"material_id": "M01", "date": "2026-11-01", "time": "18:00"}]})],
        [tc("query_slots", {"date": "2026-11-01", "from_time": "19:00"})],
        [tc("request_confirmation", {"material_id": "M01", "question": "18:00 已被占用，是否改到 19:00？",
                                     "options": ["19:00", "20:00", "改天"], "reason": "用户要求 18:00 与已有排期冲突"})],
        [tc("save_schedule_draft", {"items": [{"material_id": "M01", "date": "2026-11-01",
                                               "time": "19:00", "confirmation_id": "C001"}]})],
        [tc("finalize", {"decisions": [{"material_id": "M01", "decision": "pending_confirmation",
                                        "planned_date": "2026-11-01", "planned_time": "19:00",
                                        "reason": "18:00 被 M10 占用，19:00 空闲，已登记待确认"}]})],
    ]
    report, store, box = run_script(tmp_path, script, spec)

    m01 = item(report, "M01")
    assert report["status"] == "completed"
    assert m01["review_status"] == "ready"
    assert m01["requested_slot"] == "2026-11-01T18:00"      # 硬性要求一个字节都没动
    assert m01["suggested_slot"] == "2026-11-01 19:00"
    assert m01["slot_changed"] is True
    assert m01["draft"]["status"] == "saved"
    assert m01["draft"]["revision"] == 1
    assert len(m01["pending_confirmations"]) == 1
    assert m01["consistency_flags"] == []
    # 第一次 18:00 的保存被护栏拒绝，且没有留下脏草稿
    assert len(store.state["drafts"]) == 1

    saved = [e for e in box.ledger if e["tool"] == "save_schedule_draft"][0]
    assert saved["result"]["rejected"][0]["error_code"] == "SLOT_OCCUPIED"


def test_report_never_claims_published(tmp_path):
    spec = {"request_id": "t-status", "materials": ["M03"], "focus_date": "2026-11-01"}
    script = [
        [tc("save_schedule_draft", {"items": [{"material_id": "M03", "date": "2026-11-01", "time": "12:00"}]})],
        [tc("finalize", {"decisions": [{"material_id": "M03", "decision": "schedule",
                                        "planned_date": "2026-11-01", "planned_time": "12:00", "reason": "ok"}]})],
    ]
    report, _, _ = run_script(tmp_path, script, spec)
    blob = json.dumps(report, ensure_ascii=False).lower()
    assert "published" not in blob
    assert report["publish_state"] == "draft_only"


# ---------------------------------------------------------------- 3 信息缺失

def test_missing_info_supplement_fails_then_block(tmp_path):
    spec = {"request_id": "t-missing", "materials": ["M02"], "focus_date": "2026-11-01"}
    script = [
        [tc("read_material", {"material_ids": ["M02"]})],
        [tc("lookup_supplement", {"material_id": "M02",
                                  "fields": ["rights.license", "rights.expires_on", "duration_sec"]})],
        [tc("check_rules", {"material_ids": ["M02"]})],
        [tc("finalize", {"decisions": [{"material_id": "M02", "decision": "block",
                                        "reason": "必需字段缺失且外部素材库补查不到，需补件后复审",
                                        "pending_items": ["向创作者索取版权授权文件", "补充素材时长"]}]})],
    ]
    report, store, _ = run_script(tmp_path, script, spec)
    m02 = item(report, "M02")

    assert report["status"] == "completed"
    assert m02["review_status"] == "missing_info"
    assert m02["draft"]["status"] == "not_attempted"
    assert len(m02["pending_confirmations"]) == 2
    assert store.state["drafts"] == []
    assert any("R7" in r for r in m02["risk_reasons"])


# ---------------------------------------------------------------- 4 规则风险

def test_rule_risk_block_and_warn(tmp_path):
    spec = {"request_id": "t-risk", "materials": ["M04", "M06"], "focus_date": "2026-11-02"}
    script = [
        [tc("check_rules", {"material_ids": ["M04", "M06"]})],
        [tc("query_slots", {"date": "2026-11-02"})],
        [tc("request_confirmation", {"material_id": "M06", "question": "时长 245 秒超过 180 秒阈值，是否切条后发布？",
                                     "options": ["切条", "转长视频池", "取消"], "reason": "R3 时长超限"})],
        [tc("save_schedule_draft", {"items": [{"material_id": "M06", "date": "2026-11-02",
                                               "time": "14:00", "confirmation_id": "C001"}]})],
        [tc("finalize", {"decisions": [
            {"material_id": "M04", "decision": "block", "reason": "版权授权已于 2026-10-31 到期（R2）"},
            {"material_id": "M06", "decision": "pending_confirmation", "planned_date": "2026-11-02",
             "planned_time": "14:00", "reason": "仅时长 warn 级风险，已登记确认后存草稿"},
        ]})],
    ]
    report, _, _ = run_script(tmp_path, script, spec)

    m04, m06 = item(report, "M04"), item(report, "M06")
    assert m04["review_status"] == "blocked"
    assert m04["draft"]["status"] == "not_attempted"
    assert any("R2" in r for r in m04["risk_reasons"])
    assert m06["review_status"] == "risk"
    assert m06["draft"]["status"] == "saved"
    assert len(m06["pending_confirmations"]) == 1
    assert report["summary"]["drafts_saved"] == 1


# ---------------------------------------------------------------- 5 重复提交

def test_duplicate_submission_yields_single_draft(tmp_path):
    spec = {"request_id": "t-dup", "materials": ["M03"], "focus_date": "2026-11-01"}

    def script():
        return [
            [tc("query_slots", {"date": "2026-11-01"})],
            [tc("save_schedule_draft", {"items": [{"material_id": "M03", "date": "2026-11-01", "time": "12:00"}]})],
            [tc("finalize", {"decisions": [{"material_id": "M03", "decision": "schedule",
                                            "planned_date": "2026-11-01", "planned_time": "12:00",
                                            "reason": "无风险"}]})],
        ]

    first, store, _ = run_script(tmp_path, script(), spec)
    assert first["materials"][0]["draft"]["status"] == "saved"

    # 同一 state 文件、同一请求再跑一次（模拟运营重复提交）
    box2 = ToolBox(DataStore(DATA_DIR, tmp_path / "state.json"))
    report2 = Agent(DataStore(DATA_DIR, tmp_path / "state.json"), box2, ScriptedLLM(script()),
                    trace_path=tmp_path / "trace2.jsonl", mode="mock").run(spec)

    assert report2["materials"][0]["draft"]["status"] == "deduplicated"
    assert report2["materials"][0]["draft"]["deduplicated"] is True
    assert report2["materials"][0]["draft"]["revision"] == 1
    assert len(DataStore(DATA_DIR, tmp_path / "state.json").state["drafts"]) == 1


# ---------------------------------------------------------------- 6 保存失败

def test_save_failure_is_reported_not_hidden(tmp_path):
    spec = {"request_id": "t-fail", "materials": ["M05"], "focus_date": "2026-11-01"}
    script = [
        [tc("query_slots", {"date": "2026-11-01"})],
        [tc("save_schedule_draft", {"items": [{"material_id": "M05", "date": "2026-11-01", "time": "20:00"}]})],
        [tc("save_schedule_draft", {"items": [{"material_id": "M05", "date": "2026-11-01", "time": "20:00"}]})],
        [tc("finalize", {"decisions": [{"material_id": "M05", "decision": "save_failed",
                                        "reason": "草稿存储写入失败，重试一次仍失败，需运维介入"}]})],
    ]
    report, store, _ = run_script(tmp_path, script, spec, fault="save")
    m05 = item(report, "M05")

    assert m05["draft"]["status"] == "failed"
    assert m05["draft"]["error_code"] == "STORE_WRITE_FAILED"
    assert m05["review_status"] == "ready"      # 审核通过，但草稿没存上
    assert report["summary"]["drafts_failed"] == 1
    assert report["status"] == "completed"      # 如实报告失败也算跑完了
    assert store.state["drafts"] == []


def test_model_lying_about_save_is_downgraded(tmp_path):
    spec = {"request_id": "t-lie", "materials": ["M05"], "focus_date": "2026-11-01"}
    script = [
        [tc("query_slots", {"date": "2026-11-01"})],
        [tc("finalize", {"decisions": [{"material_id": "M05", "decision": "schedule",
                                        "planned_date": "2026-11-01", "planned_time": "20:00",
                                        "reason": "已排期"}]})],
    ]
    report, store, _ = run_script(tmp_path, script, spec)
    m05 = item(report, "M05")

    assert m05["draft"]["status"] == "failed"
    assert "CLAIMS_SCHEDULED_WITHOUT_RECEIPT" in m05["consistency_flags"]
    assert report["status"] == "incomplete"
    assert store.state["drafts"] == []


# ---------------------------------------------------------------- 上限

def test_tool_call_cap_returns_incomplete(tmp_path):
    spec = {"request_id": "t-cap", "materials": ["M01"], "focus_date": "2026-11-01"}
    script = [[tc("query_slots", {"date": "2026-11-01"})]]
    report, store, _ = run_script(tmp_path, script, spec, max_tool_calls=3, repeat_last=True)

    assert report["status"] == "incomplete"
    assert report["stop_reason"] == "tool_call_cap"
    assert report["budget"]["tool_calls_used"] == 3
    assert store.state["drafts"] == []


def test_step_cap_returns_incomplete(tmp_path):
    spec = {"request_id": "t-step", "materials": ["M01"], "focus_date": "2026-11-01"}
    script = [[tc("query_slots", {"date": "2026-11-01"})]]
    report, _, _ = run_script(tmp_path, script, spec, max_steps=4, repeat_last=True)

    assert report["status"] == "incomplete"
    assert report["stop_reason"] == "step_cap"
    assert report["budget"]["steps_used"] == 4


def test_missing_decision_marks_incomplete(tmp_path):
    spec = {"request_id": "t-nodecision", "materials": ["M03", "M05"], "focus_date": "2026-11-01"}
    script = [[tc("finalize", {"decisions": [{"material_id": "M03", "decision": "block", "reason": "缺判断"}]})]]
    report, _, _ = run_script(tmp_path, script, spec, repeat_last=True, max_steps=3)

    assert report["status"] == "incomplete"
    assert "MISSING_DECISION" in item(report, "M05")["consistency_flags"]


def test_trace_file_records_steps(tmp_path):
    spec = {"request_id": "t-trace", "materials": ["M03"], "focus_date": "2026-11-01"}
    script = [
        [tc("query_slots", {"date": "2026-11-01"})],
        [tc("finalize", {"decisions": [{"material_id": "M03", "decision": "block", "reason": "x"}]})],
    ]
    report, _, _ = run_script(tmp_path, script, spec)
    events = [json.loads(line) for line in (tmp_path / "trace.jsonl").read_text(encoding="utf-8").splitlines()]

    kinds = [e["type"] for e in events]
    assert kinds[0] == "run_start"
    assert "tool_call" in kinds
    assert kinds[-1] == "run_end"
    call = [e for e in events if e["type"] == "tool_call"][0]
    assert call["tool"] == "query_slots"
    assert call["args"] == {"date": "2026-11-01"}
    assert "free=" in call["result_summary"]
    assert "reasoning" not in json.dumps(call)      # 不落模型思维链


# ---------------------------------------------------------------- 换占用时段后重复提交

def test_reschedule_creates_revision_and_supersedes(tmp_path):
    spec1 = {"request_id": "t-rerun", "materials": ["M01"], "focus_date": "2026-11-01"}
    script1 = [
        [tc("query_slots", {"date": "2026-11-01", "for_material_ids": ["M01"]})],
        [tc("request_confirmation", {"material_id": "M01", "question": "18:00 被占，改 19:00？",
                                     "reason": "时段冲突", "options": ["19:00"]})],
        [tc("save_schedule_draft", {"items": [{"material_id": "M01", "date": "2026-11-01",
                                               "time": "19:00", "confirmation_id": "C001"}]})],
        [tc("finalize", {"decisions": [{"material_id": "M01", "decision": "pending_confirmation",
                                        "planned_date": "2026-11-01", "planned_time": "19:00", "reason": "待确认"}]})],
    ]
    first, _, _ = run_script(tmp_path, script1, spec1)
    assert first["materials"][0]["draft"]["revision"] == 1
    assert len(first["materials"][0]["pending_confirmations"]) == 1

    # 运营把 19:00 也占了，再提交同一素材
    spec2 = dict(spec1, request_id="t-rerun-2",
                 extra_schedule=[{"date": "2026-11-01", "time": "19:00", "material_id": "M03"}])
    script2 = [
        [tc("query_slots", {"date": "2026-11-01", "for_material_ids": ["M01"]})],
        [tc("request_confirmation", {"material_id": "M01", "question": "19:00 也被占，改 20:00？",
                                     "reason": "替代时段再次冲突", "options": ["20:00"]})],
        [tc("save_schedule_draft", {"items": [{"material_id": "M01", "date": "2026-11-01",
                                               "time": "20:00", "confirmation_id": "C002"}]})],
        [tc("finalize", {"decisions": [{"material_id": "M01", "decision": "pending_confirmation",
                                        "planned_date": "2026-11-01", "planned_time": "20:00", "reason": "再次待确认"}]})],
    ]
    second, store, _ = run_script(tmp_path, script2, spec2)
    m01 = second["materials"][0]

    assert m01["suggested_slot"] == "2026-11-01 20:00"
    assert m01["draft"]["revision"] == 2
    assert m01["draft"]["status"] == "saved"
    # 旧确认单已作废，报告里只剩本轮这一条
    assert len(m01["pending_confirmations"]) == 1
    assert m01["pending_confirmations"][0]["confirmation_id"] == "C002"
    assert store.get_confirmation("C001")["status"] == "superseded"
    assert [d["status"] for d in store.state["drafts"]] == ["superseded", "active"]


def test_repeated_same_failure_stops_instead_of_burning_steps(tmp_path):
    """模型卡在同一个错误上时，不能一直烧步数到上限。"""
    spec = {"request_id": "t-stall", "materials": ["M03"], "focus_date": "2026-11-01"}
    script = [[tc("save_schedule_draft", {"items": "M03"})]]      # 永远是畸形参数
    report, store, _ = run_script(tmp_path, script, spec, repeat_last=True, max_steps=12, max_tool_calls=16)

    assert report["status"] == "incomplete"
    assert report["stop_reason"] == "repeated_failure"
    assert report["budget"]["tool_calls_used"] == 3
    assert store.state["drafts"] == []
