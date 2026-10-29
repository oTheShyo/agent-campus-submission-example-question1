# Trace · case1_normal

- 运行 ID：`case1-normal`
- 模式：`mock`　模型：`mock-deterministic`
- 上限：12 步 / 16 次工具调用
- 结果：`completed`　结束原因：`finalized`　实际步数 6　工具调用 6　tokens 0

## 决策轨迹

| # | 步 | 工具 | 参数 | 返回摘要 | 后续动作 | 决策依据（模型自述） |
|---:|---:|---|---|---|---|---|
| 1 | 1 | `list_pending_materials` | `{"date": "2026-11-01"}` | count=5 ids=['M01', 'M02', 'M03', 'M05', 'M08'] | `read_material` | 先确认这批素材里哪些还在待处理状态 |
| 2 | 2 | `read_material` | `{"material_ids": ["M03", "M05"]}` | count=2 ids=['M03', 'M05'] | `check_rules` | 读字段，看有没有必需字段缺口 |
| 3 | 3 | `check_rules` | `{"material_ids": ["M03", "M05"]}` | count=2 ids=['M03', 'M05'] | `query_slots` | 字段齐了，按规则看风险等级 |
| 4 | 4 | `query_slots` | `{"date": "2026-11-01", "for_material_ids": ["M03", "M05"]}` | free=['10:00', '11:00', '12:00', '14:00', '16:00', '19:00', '20:00'] occupied=['09:00:M09', '18:00:M10', '21:00:M11'] | `save_schedule_draft` | 排期前先查 2026-11-01 的真实占用 |
| 5 | 5 | `save_schedule_draft` | `{"items": [{"material_id": "M03", "date": "2026-11-01", "time": "12:00"}, {"material_id": "M05", "date": "2026-11-01", "time": "20:00"}]}` | saved=[('M03', 'D001', 'new'), ('M05', 'D002', 'new')] | `finalize` | 校验都过了，写入草稿（不发布） |
| 6 | 6 | `finalize` | `{"decisions": [{"material_id": "M03", "decision": "schedule", "planned_date": "2026-11-01", "planned_time": "12:00", "reason": "无阻断风险，2026-11-01 12:00 空闲并已写入草稿"}, {"material_id": "M05", "decision": "schedule", "planned_date": "2026-11-01", "planned_time": "20:00", "reason": "无阻断风险，2026-11-01 20:00 空闲并已写入草稿"}]}` | decisions_accepted=2 | `结束（finalize 后收敛）` | — |

> 表中「决策依据」是模型（mock 模式下为固定策略）在发起该次调用时附带的一句说明；不包含模型内部思维链（`reasoning_content` 在接入层已丢弃）。「后续动作」由下一条记录的工具名给出。
> 完整工具返回体在对应的 `.jsonl` 里，逐行可解析。
