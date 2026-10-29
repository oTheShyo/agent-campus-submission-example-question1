"""规则引擎单测：每条规则都要能命中，也要能在干净素材上保持沉默。"""

from src import guards


def rule_ids(risks):
    return sorted(r["rule_id"] for r in risks)


def test_clean_material_has_no_risk(store):
    m01 = store.materials_by_id["M01"]
    risks = guards.evaluate_rules(store, m01, "2026-11-01", "19:00")
    assert rule_ids(risks) == []


def test_r1_license_unknown(store):
    m02 = store.materials_by_id["M02"]
    risks = guards.evaluate_rules(store, m02)
    assert "R1" in rule_ids(risks)


def test_r2_license_expired_before_publish_date(store):
    m04 = store.materials_by_id["M04"]  # expires 2026-10-31, publish 2026-11-02
    risks = guards.evaluate_rules(store, m04, "2026-11-02", "16:00")
    assert "R2" in rule_ids(risks)
    assert guards.blocking_risks(risks)


def test_r2_not_fired_before_expiry(store):
    m04 = store.materials_by_id["M04"]
    risks = guards.evaluate_rules(store, m04, "2026-10-30", "16:00")
    assert "R2" not in rule_ids(risks)


def test_r3_duration_warn_only(store):
    m06 = store.materials_by_id["M06"]  # 245s
    risks = guards.evaluate_rules(store, m06, "2026-11-02", "14:00")
    assert "R3" in rule_ids(risks)
    assert not guards.blocking_risks(risks)
    assert len(guards.warn_risks(risks)) == 1


def test_r6_sensitive_word(store):
    m08 = store.materials_by_id["M08"]  # 文案含「最强」「绝对」
    risks = guards.evaluate_rules(store, m08, "2026-11-01", "16:00")
    assert "R6" in rule_ids(risks)
    assert guards.blocking_risks(risks)


def test_r7_required_fields(store):
    m02 = store.materials_by_id["M02"]
    risks = guards.evaluate_rules(store, m02)
    r7 = [r for r in risks if r["rule_id"] == "R7"][0]
    assert "duration_sec" in r7["evidence"]["missing"]
    assert "rights.license" in r7["evidence"]["missing"]


def test_r8_already_scheduled(store):
    m09 = store.materials_by_id["M09"]
    risks = guards.evaluate_rules(store, m09, "2026-11-01", "09:00")
    assert "R8" in rule_ids(risks)


def test_r5_brand_conflict_against_same_day_occupant(store):
    # 让 11-01 18:00 的 M10 变成 M01 的竞品品牌
    store.materials_by_id["M10"]["content"]["sponsor"] = "完美日记"
    m01 = store.materials_by_id["M01"]
    risks = guards.evaluate_rules(store, m01, "2026-11-01", "19:00")
    assert "R5" in rule_ids(risks)
    assert guards.blocking_risks(risks)


def test_r4_creator_daily_cap(store):
    # 11-01 已有 M09/M10 占用，把它们都改成同一创作者
    store.materials_by_id["M09"]["creator"] = "橘猫日记"
    store.materials_by_id["M10"]["creator"] = "橘猫日记"
    m03 = store.materials_by_id["M03"]  # creator 橘猫日记
    risks = guards.evaluate_rules(store, m03, "2026-11-01", "12:00")
    assert "R4" in rule_ids(risks)
    assert not guards.blocking_risks(risks)


def test_optional_field_missing_is_not_a_risk(store):
    m07 = store.materials_by_id["M07"]  # 缺 content.sponsor（可选）
    risks = guards.evaluate_rules(store, m07, "2026-11-02", "12:00")
    assert rule_ids(risks) == []
