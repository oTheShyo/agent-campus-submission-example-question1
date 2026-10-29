# Trace · case2_missing_info

- 运行 ID：`case2-missing`
- 模式：`mock`　模型：`mock-deterministic`
- 上限：12 步 / 16 次工具调用
- 结果：`completed`　结束原因：`finalized`　实际步数 8　工具调用 8　tokens 0

## 决策轨迹

| # | 步 | 工具 | 参数 | 返回摘要 | 后续动作 | 决策依据（模型自述） |
|---:|---:|---|---|---|---|---|
| 1 | 1 | `list_pending_materials` | `{"date": null}` | count=8 ids=['M01', 'M02', 'M03', 'M04', 'M05', 'M06', 'M07', 'M08'] | `read_material` | 先确认这批素材里哪些还在待处理状态 |
| 2 | 2 | `read_material` | `{"material_ids": ["M02", "M07", "M09"]}` | count=3 ids=['M02', 'M07', 'M09'] M02 missing=['duration_sec', 'rights.license', 'rights.expires_on'] | `lookup_supplement` | 读字段，看有没有必需字段缺口 |
| 3 | 3 | `lookup_supplement` | `{"material_id": "M02", "fields": ["duration_sec", "rights.license", "rights.expires_on"]}` | found=[('duration_sec', False), ('rights.license', False), ('rights.expires_on', False)] | `check_rules` | M02 缺 3 个必需字段，先去外部素材库补查 |
| 4 | 4 | `check_rules` | `{"material_ids": ["M02", "M07", "M09"]}` | count=3 ids=['M02', 'M07', 'M09'] M02 risks=['R1:block', 'R7:block'] M09 risks=['R8:block'] | `query_slots` | 字段齐了，按规则看风险等级 |
| 5 | 5 | `query_slots` | `{"date": "2026-11-01", "for_material_ids": ["M02", "M09"]}` | free=['09:00', '10:00', '11:00', '12:00', '14:00', '16:00', '19:00', '20:00'] occupied=['18:00:M10', '21:00:M11'] | `query_slots` | 排期前先查 2026-11-01 的真实占用 |
| 6 | 6 | `query_slots` | `{"date": "2026-11-02", "for_material_ids": ["M07"]}` | free=['09:00', '10:00', '11:00', '12:00', '14:00', '18:00', '19:00', '20:00', '21:00'] occupied=['16:00:M12'] | `save_schedule_draft` | 排期前先查 2026-11-02 的真实占用 |
| 7 | 7 | `save_schedule_draft` | `{"items": [{"material_id": "M07", "date": "2026-11-02", "time": "12:00"}]}` | saved=[('M07', 'D001', 'new')] | `finalize` | 校验都过了，写入草稿（不发布） |
| 8 | 8 | `finalize` | `{"decisions": [{"material_id": "M02", "decision": "block", "reason": "必需字段缺失（duration_sec, rights.license, rights.expires_on），外部素材库补查不到，需补件后复审", "pending_items": ["向创作者补件：duration_sec", "向创作者补件：rights.license", "向创作者补件：rights.expires_on", "补齐后重新送审"]}, {"material_id": "M07", "decision": "schedule", "planned_date": "2026-11-02", "planned_time": "12:00", "reason": "无阻断风险，2026-11-02 12:00 空闲并已写入草稿"}, {"material_id": "M09", "decision": "block", "reason": "存在阻断级风险 R8，不予排期：该素材已在排期中（2026-11-01 09:00），不得自动重复排期，改期需运营确认。", "pending_items": ["[R8] 素材已在排期中：需人工复审或修改后重排"]}]}` | decisions_accepted=3 | `结束（finalize 后收敛）` | — |

> 表中「决策依据」是模型（mock 模式下为固定策略）在发起该次调用时附带的一句说明；不包含模型内部思维链（`reasoning_content` 在接入层已丢弃）。「后续动作」由下一条记录的工具名给出。
> 完整工具返回体在对应的 `.jsonl` 里，逐行可解析。
