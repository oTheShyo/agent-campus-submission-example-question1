"""时段查询：空闲/占用、区间过滤、草稿占位、自己旧草稿不算冲突。"""


def test_baseline_occupancy_1101(store):
    occ = store.occupancy("2026-11-01")
    assert set(occ) == {"09:00", "18:00", "21:00"}
    assert occ["18:00"]["material_id"] == "M10"


def test_free_slots_1101(store):
    assert store.free_slots("2026-11-01") == ["10:00", "11:00", "12:00", "14:00", "16:00", "19:00", "20:00"]


def test_free_slots_1102(store):
    assert "16:00" not in store.free_slots("2026-11-02")


def test_from_time_filter(store):
    assert store.free_slots("2026-11-01", from_time="19:00") == ["19:00", "20:00"]


def test_from_time_beyond_last_slot_falls_back_to_full_grid(store):
    # 22:00 之后没有任何时段时，不应返回空集让模型无从下手，而是退回全天网格
    expected = [t for t in store.slot_grid if t != "16:00"]  # 11-02 的 16:00 已被 M12 占用
    assert store.free_slots("2026-11-02", from_time="22:00") == expected


def test_draft_occupies_slot(store):
    store.save_drafts(
        [{"material_id": "M03", "date": "2026-11-01", "time": "12:00", "requested_at": "2026-11-01T12:00"}]
    )
    assert store.occupancy("2026-11-01")["12:00"]["material_id"] == "M03"
    assert "12:00" not in store.free_slots("2026-11-01")


def test_own_draft_does_not_block_reschedule(store):
    store.save_drafts(
        [{"material_id": "M01", "date": "2026-11-01", "time": "19:00", "requested_at": "2026-11-01T18:00"}]
    )
    # 给 M01 改期时，自己那条 19:00 草稿不应被当成别人占用
    assert "19:00" in store.free_slots("2026-11-01", exclude_material_id="M01")
    assert "19:00" not in store.free_slots("2026-11-01")


def test_extra_schedule_from_input_counts_as_occupied(make_store):
    store = make_store(extra_schedule=[{"date": "2026-11-01", "time": "19:00", "material_id": "M03"}])
    occ = store.occupancy("2026-11-01")
    assert occ["19:00"]["source"] == "input"
    assert "19:00" not in store.free_slots("2026-11-01")


def test_query_slots_excludes_own_draft_for_reschedule(store):
    from src.tools import ToolBox

    store.save_drafts(
        [{"material_id": "M01", "date": "2026-11-01", "time": "19:00", "requested_at": "2026-11-01T18:00"}]
    )
    box = ToolBox(store)
    plain = box.dispatch("query_slots", {"date": "2026-11-01"})
    own = box.dispatch("query_slots", {"date": "2026-11-01", "for_material_ids": ["M01"]})

    assert "19:00" not in plain["free"]      # 别人看是占用
    assert "19:00" in own["free"]            # 自己要改期时不算冲突
    assert own["excluded_own_drafts"] == ["M01"]
