"""硬护栏：越权保存必须被拒绝，且拒绝理由可读。"""

from src import guards


def codes(rejected):
    return [r["error_code"] for r in rejected]


def test_blocked_material_cannot_be_saved(store):
    accepted, rejected = guards.validate_save(
        store, [{"material_id": "M04", "date": "2026-11-02", "time": "16:00"}]
    )
    assert accepted == []
    assert "BLOCKING_RISK" in codes(rejected)
    assert "R2" in rejected[0]["risks"]


def test_occupied_slot_cannot_be_saved(store):
    accepted, rejected = guards.validate_save(
        store, [{"material_id": "M01", "date": "2026-11-01", "time": "18:00"}]
    )
    assert accepted == []
    assert codes(rejected) == ["SLOT_OCCUPIED"]
    assert rejected[0]["occupied_by"] == "M10"


def test_deviation_without_confirmation_is_rejected(store):
    accepted, rejected = guards.validate_save(
        store, [{"material_id": "M01", "date": "2026-11-01", "time": "19:00"}]
    )
    assert accepted == []
    assert codes(rejected) == ["DEVIATION_WITHOUT_CONFIRMATION"]


def test_deviation_with_confirmation_is_accepted(store):
    conf = store.add_confirmation("M01", "18:00 已占用，是否改 19:00？", ["19:00", "改天"], "时段冲突")
    accepted, rejected = guards.validate_save(
        store, [{"material_id": "M01", "date": "2026-11-01", "time": "19:00",
                 "confirmation_id": conf["confirmation_id"]}]
    )
    assert rejected == []
    assert accepted[0]["deviates_from_request"] is True
    assert accepted[0]["confirmation_id"] == conf["confirmation_id"]


def test_confirmation_from_other_material_is_rejected(store):
    conf = store.add_confirmation("M05", "问题", ["A"], "理由")
    accepted, rejected = guards.validate_save(
        store, [{"material_id": "M01", "date": "2026-11-01", "time": "19:00",
                 "confirmation_id": conf["confirmation_id"]}]
    )
    assert accepted == []
    assert codes(rejected) == ["UNKNOWN_CONFIRMATION"]


def test_unknown_confirmation_id_is_rejected(store):
    accepted, rejected = guards.validate_save(
        store, [{"material_id": "M01", "date": "2026-11-01", "time": "19:00", "confirmation_id": "C999"}]
    )
    assert accepted == []
    assert codes(rejected) == ["UNKNOWN_CONFIRMATION"]


def test_off_grid_time_is_rejected(store):
    accepted, rejected = guards.validate_save(
        store, [{"material_id": "M01", "date": "2026-11-01", "time": "18:30", "confirmation_id": "C001"}]
    )
    assert accepted == []
    assert codes(rejected) == ["INVALID_SLOT"]


def test_unknown_material_is_rejected(store):
    accepted, rejected = guards.validate_save(store, [{"material_id": "M99", "date": "2026-11-01", "time": "19:00"}])
    assert accepted == []
    assert codes(rejected) == ["MATERIAL_NOT_FOUND"]


def test_duplicate_lines_in_one_request_are_rejected(store):
    accepted, rejected = guards.validate_save(
        store,
        [
            {"material_id": "M03", "date": "2026-11-01", "time": "12:00"},
            {"material_id": "M03", "date": "2026-11-01", "time": "14:00"},
        ],
    )
    assert len(accepted) == 1
    assert codes(rejected) == ["DUPLICATE_ITEM"]


def test_matching_requested_time_needs_no_confirmation(store):
    accepted, rejected = guards.validate_save(
        store, [{"material_id": "M03", "date": "2026-11-01", "time": "12:00"}]
    )
    assert rejected == []
    assert accepted[0]["deviates_from_request"] is False


def test_rules_are_rechecked_at_save_time(store):
    """先 check 后改时段也不能绕过：save 时按最终日期重算规则。"""
    # M04 在 2026-10-30 发布不触发 R2，但要求的是 11-02，改不了
    accepted, rejected = guards.validate_save(
        store, [{"material_id": "M04", "date": "2026-11-02", "time": "20:00", "confirmation_id": "C001"}]
    )
    assert accepted == []
    assert "BLOCKING_RISK" in codes(rejected)
