"""工具层行为：分发、参数校验、返回结构。"""

from src.tools import TOOL_NAMES, TOOLS_SPEC, ToolBox


def test_all_schemas_have_unique_names(store):
    assert len(TOOL_NAMES) == len(set(TOOL_NAMES))
    assert set(TOOL_NAMES) == {
        "list_pending_materials",
        "read_material",
        "lookup_supplement",
        "check_rules",
        "query_slots",
        "request_confirmation",
        "save_schedule_draft",
        "finalize",
    }
    for spec in TOOLS_SPEC:
        assert spec["function"]["description"]
        assert spec["function"]["parameters"]["type"] == "object"


def test_unknown_tool_is_reported_not_raised(store):
    box = ToolBox(store)
    res = box.dispatch("no_such_tool", {})
    assert res["ok"] is False
    assert res["error_code"] == "UNKNOWN_TOOL"


def test_bad_arguments_are_reported_not_raised(store):
    box = ToolBox(store)
    res = box.dispatch("query_slots", {"nonsense": 1})
    assert res["ok"] is False
    assert res["error_code"] == "BAD_ARGUMENTS"


def test_list_pending_materials(store):
    box = ToolBox(store)
    res = box.dispatch("list_pending_materials", {})
    assert res["ok"] is True
    assert res["count"] == 8
    assert "M01" in [m["material_id"] for m in res["materials"]]


def test_list_pending_materials_by_date(store):
    box = ToolBox(store)
    res = box.dispatch("list_pending_materials", {"date": "2026-11-02"})
    ids = [m["material_id"] for m in res["materials"]]
    assert ids == ["M04", "M06", "M07"]


def test_read_material_reports_missing_and_optional(store):
    box = ToolBox(store)
    res = box.dispatch("read_material", {"material_ids": ["M02", "M07", "M99"]})
    by_id = {m["material_id"]: m for m in res["materials"]}
    assert "duration_sec" in by_id["M02"]["missing_required"]
    assert by_id["M07"]["missing_required"] == []
    assert by_id["M07"]["missing_optional"] == ["content.sponsor"]
    assert res["unknown_ids"] == ["M99"]


def test_read_material_marks_already_scheduled(store):
    box = ToolBox(store)
    res = box.dispatch("read_material", {"material_ids": ["M09"]})
    assert res["materials"][0]["already_scheduled"] is True


def test_lookup_supplement_found_and_not_found(store):
    box = ToolBox(store)
    ok = box.dispatch("lookup_supplement", {"material_id": "M07", "fields": ["content.sponsor"]})
    assert ok["all_found"] is True
    assert ok["results"][0]["value"].startswith("自有品牌")

    miss = box.dispatch("lookup_supplement", {"material_id": "M02", "fields": ["rights.license"]})
    assert miss["all_found"] is False
    assert miss["results"][0]["found"] is False


def test_check_rules_aggregates(store):
    box = ToolBox(store)
    res = box.dispatch("check_rules", {"material_ids": ["M01", "M04"], "planned_date": "2026-11-02"})
    assert res["any_blocking"] is True
    assert res["materials"][0]["blocking"] is False


def test_query_slots_shape(store):
    box = ToolBox(store)
    res = box.dispatch("query_slots", {"date": "2026-11-01"})
    assert res["free"] == ["10:00", "11:00", "12:00", "14:00", "16:00", "19:00", "20:00"]
    assert {"time": "18:00", "material_id": "M10", "source": "baseline"} in res["occupied"]


def test_save_rejects_all_items_returns_false(store):
    box = ToolBox(store)
    res = box.dispatch("save_schedule_draft", {"items": [{"material_id": "M04", "date": "2026-11-02", "time": "16:00"}]})
    assert res["ok"] is False
    assert res["error_code"] == "ALL_ITEMS_REJECTED"
    assert res["rejected"][0]["error_code"] == "BLOCKING_RISK"


def test_save_failure_returns_false_not_raise(store, make_store):
    broken = make_store(fault="save")
    box = ToolBox(broken)
    res = box.dispatch("save_schedule_draft", {"items": [{"material_id": "M03", "date": "2026-11-01", "time": "12:00"}]})
    assert res["ok"] is False
    assert res["error_code"] == "STORE_WRITE_FAILED"


def test_finalize_validates_decision_enum(store):
    box = ToolBox(store)
    res = box.dispatch(
        "finalize",
        {"decisions": [{"material_id": "M01", "decision": "publish_now", "reason": "x"},
                       {"material_id": "M01", "decision": "block", "reason": "ok"}]},
    )
    assert res["ok"] is True
    assert res["accepted"] == 1
    assert res["invalid"][0]["error"] != ""


def test_save_note_never_says_published(store):
    box = ToolBox(store)
    res = box.dispatch("save_schedule_draft", {"items": [{"material_id": "M03", "date": "2026-11-01", "time": "12:00"}]})
    assert "未发布" in res["note"]
    assert "published" not in str(res).lower()


def test_string_instead_of_array_is_coerced(store):
    box = ToolBox(store)
    res = box.dispatch("read_material", {"material_ids": "M03"})
    assert res["ok"] is True
    assert [m["material_id"] for m in res["materials"]] == ["M03"]


def test_malformed_save_items_return_error_not_crash(store):
    box = ToolBox(store)
    res = box.dispatch("save_schedule_draft", {"items": "M03"})
    assert res["ok"] is False
    assert res["error_code"] == "BAD_ARGUMENTS"


def test_malformed_finalize_decisions_return_error_not_crash(store):
    box = ToolBox(store)
    res = box.dispatch("finalize", {"decisions": ["M03"]})
    assert res["ok"] is False
    assert res["error_code"] == "INVALID_DECISIONS"
    assert res["invalid"][0]["error"].startswith("decision 必须是对象")


def test_finalize_accepts_json_encoded_string(store):
    """真实模型会把整个数组序列化成字符串传进来，必须能解开。"""
    import json

    box = ToolBox(store)
    payload = json.dumps([{"material_id": "M03", "decision": "block", "reason": "测试"}])
    res = box.dispatch("finalize", {"decisions": payload})
    assert res["ok"] is True
    assert res["accepted"] == 1
    assert box.final_decisions[0]["material_id"] == "M03"


def test_save_with_bad_item_shape_is_rejected(store):
    box = ToolBox(store)
    res = box.dispatch("save_schedule_draft", {"items": ["M03"]})
    assert res["ok"] is False
    assert res["error_code"] == "BAD_ARGUMENTS"
