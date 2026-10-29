"""同意主体测试。

题目原文是「未经用户同意不得擅自改变硬性发布要求」。这里的"用户"必须是模型之外的人，
所以本文件锁死一条：**确认单的 answer 只能由外部答复写入，模型说"已同意"不算数。**
"""

from __future__ import annotations

from pathlib import Path

from src.agent import Agent
from src.store import DataStore
from src.tools import ToolBox

from fakes import ScriptedLLM, tc

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SPEC = {"request_id": "t-consent", "materials": ["M01"], "focus_date": "2026-11-01"}


def _run(tmp_path, script, confirm=None, spec=None):
    spec = spec or SPEC
    store = DataStore(DATA_DIR, tmp_path / "state.json", extra_schedule=spec.get("extra_schedule"))
    if confirm:
        store.apply_answers(confirm)
    box = ToolBox(store)
    agent = Agent(store, box, ScriptedLLM(script), trace_path=None, mode="mock")
    return agent.run(spec), store, box


def _deviation_script(decision: str, reason: str, save_first: bool = True) -> list:
    steps = [
        [tc("read_material", {"material_ids": ["M01"]})],
        [tc("query_slots", {"date": "2026-11-01", "for_material_ids": ["M01"]})],
        [tc("request_confirmation", {"material_id": "M01", "question": "18:00 被占，改 19:00？",
                                     "reason": "时段冲突", "options": ["19:00"]})],
    ]
    if save_first:
        steps.append([tc("save_schedule_draft", {"items": [
            {"material_id": "M01", "date": "2026-11-01", "time": "19:00", "confirmation_id": "C001"}
        ]})])
    steps.append([tc("finalize", {"decisions": [
        {"material_id": "M01", "decision": decision, "planned_date": "2026-11-01",
         "planned_time": "19:00", "reason": reason}
    ]})])
    return steps


# ---------------------------------------------------------------- 工具层

def test_request_confirmation_only_creates_pending(tmp_path):
    store = DataStore(DATA_DIR, tmp_path / "s.json")
    rec = store.add_confirmation("M01", "改到 19:00？", ["19:00"], "冲突")

    assert rec["status"] == "pending"
    assert rec["answer"] is None
    assert rec["answered_by"] is None
    assert rec["issued_by"] == "agent"      # 提出方是模型，不是用户


def test_apply_answers_accept_marks_answered(tmp_path):
    store = DataStore(DATA_DIR, tmp_path / "s.json")
    store.add_confirmation("M01", "改到 19:00？", ["19:00"], "冲突")

    out = store.apply_answers({"C001": {"answer": "accept", "answered_by": "ops@x", "note": "同意"}})

    assert out["applied"] == [{"confirmation_id": "C001", "answer": "accept", "answered_by": "ops@x"}]
    conf = store.get_confirmation("C001")
    assert conf["status"] == "accepted"
    assert conf["answer"] == "accept"
    assert conf["answered_by"] == "ops@x"


def test_apply_answers_rejects_bad_input(tmp_path):
    store = DataStore(DATA_DIR, tmp_path / "s.json")
    store.add_confirmation("M01", "改到 19:00？", ["19:00"], "冲突")
    store.apply_answers({"C001": {"answer": "accept", "answered_by": "ops"}})

    out = store.apply_answers({
        "C999": {"answer": "accept"},          # 不存在
        "C001": {"answer": "reject"},          # 已回答过，不允许覆盖
    })
    assert {r["reason"] for r in out["rejected"]} == {"UNKNOWN_CONFIRMATION", "ALREADY_ANSWERED"}
    assert out["applied"] == []

    store2 = DataStore(DATA_DIR, tmp_path / "s2.json")
    store2.add_confirmation("M01", "q", [], "r")
    bad = store2.apply_answers({"C001": {"answer": "maybe"}})
    assert bad["rejected"][0]["reason"] == "INVALID_ANSWER"
    assert store2.get_confirmation("C001")["answer"] is None


def test_rejected_confirmation_cannot_be_used_to_save(tmp_path):
    """人工拒绝后，模型不能再拿这个凭证去落草稿——这是补上的一致性口子。"""
    store = DataStore(DATA_DIR, tmp_path / "s.json")
    store.add_confirmation("M01", "改到 19:00？", ["19:00"], "冲突")
    store.apply_answers({"C001": {"answer": "reject", "answered_by": "ops@x"}})
    box = ToolBox(store)

    res = box.dispatch("save_schedule_draft", {"items": [
        {"material_id": "M01", "date": "2026-11-01", "time": "19:00", "confirmation_id": "C001"}
    ]})

    assert res["ok"] is False
    assert res["error_code"] == "ALL_ITEMS_REJECTED"
    assert res["rejected"][0]["error_code"] == "CONFIRMATION_REJECTED"
    assert store.state["drafts"] == []


def test_read_material_exposes_confirmation_answer(tmp_path):
    store = DataStore(DATA_DIR, tmp_path / "s.json")
    store.add_confirmation("M01", "改到 19:00？", ["19:00"], "冲突")
    store.apply_answers({"C001": {"answer": "accept", "answered_by": "ops@x"}})
    box = ToolBox(store)

    res = box.dispatch("read_material", {"material_ids": ["M01"]})
    confs = res["materials"][0]["confirmations"]
    assert confs[0]["confirmation_id"] == "C001"
    assert confs[0]["answer"] == "accept"
    assert confs[0]["answered_by"] == "ops@x"


# ---------------------------------------------------------------- 报告层（关键）

def test_model_claiming_agreement_does_not_grant_consent(tmp_path):
    """模型自己说"用户已同意"不产生任何效果。"""
    report, _, _ = _run(tmp_path, _deviation_script(
        "schedule", "用户已同意改到 19:00，双方确认无误。"))

    m = report["materials"][0]
    assert m["slot_changed"] is True
    assert m["consent_status"] == "pending"          # 不是 granted
    assert m["confirmations"][0]["answer"] is None
    assert m["confirmations"][0]["answered_by"] is None
    assert report["summary"]["consent_granted"] == 0
    assert report["summary"]["consent_pending"] == 1


def test_external_accept_turns_consent_granted(tmp_path):
    # 第一轮：模型登记确认单，此时无人答复
    first, _, _ = _run(tmp_path, _deviation_script(
        "pending_confirmation", "待人工确认改到 19:00。", save_first=False))
    assert first["materials"][0]["consent_status"] == "pending"
    assert first["materials"][0]["confirmations"][0]["answer"] is None

    # 第二轮：外部答复 accept 后重跑，同一 state 文件里 C001 已变为 accepted
    second_script = [
        [tc("read_material", {"material_ids": ["M01"]})],
        [tc("save_schedule_draft", {"items": [
            {"material_id": "M01", "date": "2026-11-01", "time": "19:00", "confirmation_id": "C001"}]})],
        [tc("finalize", {"decisions": [
            {"material_id": "M01", "decision": "schedule", "planned_date": "2026-11-01",
             "planned_time": "19:00", "reason": "外部已批准改到 19:00。"}]})],
    ]
    report, _, _ = _run(tmp_path, second_script,
                        confirm={"C001": {"answer": "accept", "answered_by": "ops@x"}})

    m = report["materials"][0]
    assert m["consent_status"] == "granted"
    assert m["effective_status"] == "scheduled"
    assert m["confirmations"][0]["answered_by"] == "ops@x"
    assert report["summary"]["consent_granted"] == 1


def test_reject_withdraws_existing_draft(tmp_path):
    """人否决后，之前基于待确认写的草稿必须一起作废，不能继续占着时段。"""
    first, _, _ = _run(tmp_path, _deviation_script("pending_confirmation", "待确认"))
    assert first["materials"][0]["draft"]["status"] == "saved"

    second_script = [
        [tc("read_material", {"material_ids": ["M01"]})],
        [tc("finalize", {"decisions": [
            {"material_id": "M01", "decision": "block", "planned_date": "2026-11-01",
             "planned_time": "19:00", "reason": "替代时段提案已被否决"}]})],
    ]
    report, store, _ = _run(tmp_path, second_script,
                            confirm={"C001": {"answer": "reject", "answered_by": "ops@x"}})

    m = report["materials"][0]
    assert m["consent_status"] == "rejected"
    assert m["effective_status"] == "blocked"
    assert m["draft"]["status"] == "not_attempted"       # 本轮没有写新草稿
    assert [d["status"] for d in store.state["drafts"]] == ["withdrawn"]
    # withdrawn 的草稿不再占时段
    assert "2026-11-01 19:00" not in [f"{e['date']} {e['time']}" for e in store.schedule_entries()]


def test_no_deviation_needs_no_consent(tmp_path):
    spec = {"request_id": "t-clean", "materials": ["M03"], "focus_date": "2026-11-01"}
    script = [
        [tc("read_material", {"material_ids": ["M03"]})],
        [tc("save_schedule_draft", {"items": [
            {"material_id": "M03", "date": "2026-11-01", "time": "12:00"}]})],
        [tc("finalize", {"decisions": [
            {"material_id": "M03", "decision": "schedule", "planned_date": "2026-11-01",
             "planned_time": "12:00", "reason": "按原要求排期"}]})],
    ]
    report, _, _ = _run(tmp_path, script, spec=spec)

    m = report["materials"][0]
    assert m["slot_changed"] is False
    assert m["consent_status"] == "not_required"
    assert m["confirmations"] == []
