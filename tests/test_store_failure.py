"""保存失败：必须显式抛错，且磁盘字节不变（不能"看起来失败其实写了半截"）。"""

import json

import pytest

from src.models import StoreWriteError

ITEM = {"material_id": "M03", "date": "2026-11-01", "time": "12:00", "requested_at": "2026-11-01T12:00"}


def test_fault_injection_raises(make_store):
    store = make_store(fault="save")
    with pytest.raises(StoreWriteError):
        store.save_drafts([ITEM])


def test_fault_leaves_no_state_file(make_store):
    store = make_store(fault="save")
    with pytest.raises(StoreWriteError):
        store.save_drafts([ITEM])
    assert not store.state_path.exists()


def test_fault_keeps_existing_bytes_unchanged(make_store):
    healthy = make_store()
    healthy.save_drafts([ITEM])
    before = healthy.state_path.read_bytes()

    broken = make_store(fault="save")
    with pytest.raises(StoreWriteError):
        broken.save_drafts([{"material_id": "M05", "date": "2026-11-01", "time": "20:00",
                             "requested_at": "2026-11-01T20:00"}])

    assert broken.state_path.read_bytes() == before
    assert len(json.loads(before)["drafts"]) == 1


def test_fault_from_env(monkeypatch, make_store):
    monkeypatch.setenv("SCHEDULER_FAULT", "save")
    store = make_store(fault=None)  # 显式传 None → 走环境变量
    with pytest.raises(StoreWriteError):
        store.save_drafts([ITEM])


def test_in_memory_state_rolls_back_on_failure(make_store):
    store = make_store(fault="save")
    with pytest.raises(StoreWriteError):
        store.save_drafts([ITEM])
    assert store.state["drafts"] == []


def test_state_file_is_atomic_no_tmp_left(make_store):
    store = make_store()
    store.save_drafts([ITEM])
    leftovers = list(store.state_path.parent.glob("*.tmp"))
    assert leftovers == []
