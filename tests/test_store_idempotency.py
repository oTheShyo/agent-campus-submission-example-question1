"""防重复：同一请求只产生一份草稿；改时段产生新 revision 并 supersede 旧的。"""


def _item(mid="M03", date="2026-11-01", time="12:00", requested="2026-11-01T12:00", **kw):
    return {"material_id": mid, "date": date, "time": time, "requested_at": requested, **kw}


def test_same_request_twice_creates_one_draft(tmp_path, store):
    first = store.save_drafts([_item()])
    second = store.save_drafts([_item()])

    assert first[0]["deduplicated"] is False
    assert second[0]["deduplicated"] is True
    assert second[0]["draft_id"] == first[0]["draft_id"]
    assert second[0]["revision"] == first[0]["revision"] == 1
    assert len(store.state["drafts"]) == 1


def test_dedupe_survives_reload(tmp_path, make_store):
    store = make_store()
    first = store.save_drafts([_item()])
    reloaded = make_store()  # 同一 state 文件重新加载，模拟"重复提交"发生在另一次运行
    second = reloaded.save_drafts([_item()])
    assert second[0]["draft_id"] == first[0]["draft_id"]
    assert second[0]["deduplicated"] is True
    assert len(reloaded.state["drafts"]) == 1


def test_different_time_creates_new_revision(store):
    store.save_drafts([_item(time="12:00")])
    res = store.save_drafts([_item(time="14:00")])
    drafts = store.state["drafts"]

    assert res[0]["revision"] == 2
    assert len(drafts) == 2
    old = [d for d in drafts if d["revision"] == 1][0]
    new = [d for d in drafts if d["revision"] == 2][0]
    assert old["status"] == "superseded"
    assert old["superseded_by"] == new["draft_id"]
    assert new["status"] == "active"


def test_fingerprint_differs_by_time_and_date(store):
    a = store.fingerprint({"material_id": "M01", "date": "2026-11-01", "time": "18:00", "requested_at": "x"})
    b = store.fingerprint({"material_id": "M01", "date": "2026-11-01", "time": "19:00", "requested_at": "x"})
    c = store.fingerprint({"material_id": "M01", "date": "2026-11-02", "time": "18:00", "requested_at": "x"})
    assert len({a, b, c}) == 3


def test_two_materials_same_slot_different_drafts(store):
    store.save_drafts([_item("M03", time="12:00")])
    res = store.save_drafts([_item("M05", time="12:00")])
    assert res[0]["deduplicated"] is False
    assert len(store.state["drafts"]) == 2


def test_supersede_invalidates_old_confirmation(store):
    conf = store.add_confirmation("M01", "18:00 被占，改 19:00？", ["19:00"], "冲突")
    store.save_drafts([{"material_id": "M01", "date": "2026-11-01", "time": "19:00",
                        "requested_at": "2026-11-01T18:00", "confirmation_id": conf["confirmation_id"]}])
    store.save_drafts([{"material_id": "M01", "date": "2026-11-01", "time": "20:00",
                        "requested_at": "2026-11-01T18:00", "confirmation_id": conf["confirmation_id"]}])

    old = store.get_confirmation(conf["confirmation_id"])
    assert old["status"] == "superseded"
    assert old["superseded_by"]
