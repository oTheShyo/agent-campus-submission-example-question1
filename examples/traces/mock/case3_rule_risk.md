# Trace · case3_rule_risk

- 运行 ID：`case3-rule-risk`
- 模式：`mock`　模型：`mock-deterministic`
- 上限：12 步 / 16 次工具调用
- 结果：`completed`　结束原因：`finalized`　实际步数 8　工具调用 8　tokens 0

## 决策轨迹

| # | 步 | 工具 | 参数 | 返回摘要 | 后续动作 | 决策依据（模型自述） |
|---:|---:|---|---|---|---|---|
| 1 | 1 | `list_pending_materials` | `{"date": null}` | count=8 ids=['M01', 'M02', 'M03', 'M04', 'M05', 'M06', 'M07', 'M08'] | `read_material` | 先确认这批素材里哪些还在待处理状态 |
| 2 | 2 | `read_material` | `{"material_ids": ["M04", "M06", "M08"]}` | count=3 ids=['M04', 'M06', 'M08'] | `check_rules` | 读字段，看有没有必需字段缺口 |
| 3 | 3 | `check_rules` | `{"material_ids": ["M04", "M06", "M08"]}` | count=3 ids=['M04', 'M06', 'M08'] M04 risks=['R2:block'] M06 risks=['R3:warn'] M08 risks=['R6:block'] | `query_slots` | 字段齐了，按规则看风险等级 |
| 4 | 4 | `query_slots` | `{"date": "2026-11-01", "for_material_ids": ["M08"]}` | free=['10:00', '11:00', '12:00', '14:00', '16:00', '19:00', '20:00'] occupied=['09:00:M09', '18:00:M10', '21:00:M11'] | `query_slots` | 排期前先查 2026-11-01 的真实占用 |
| 5 | 5 | `query_slots` | `{"date": "2026-11-02", "for_material_ids": ["M04", "M06"]}` | free=['09:00', '10:00', '11:00', '12:00', '14:00', '18:00', '19:00', '20:00', '21:00'] occupied=['16:00:M12'] | `request_confirmation` | 排期前先查 2026-11-02 的真实占用 |
| 6 | 6 | `request_confirmation` | `{"material_id": "M06", "question": "该素材存在 1 条警告级风险，是否仍按 14:00 排期？", "reason": "素材时长 245 秒超过阈值 180 秒，需运营确认是否切条或转长视频池。", "options": ["14:00", "改天", "取消"]}` | confirmation=C001 status=pending | `save_schedule_draft` | M06 与用户要求不一致或有警告级风险，先登记待确认 |
| 7 | 7 | `save_schedule_draft` | `{"items": [{"material_id": "M06", "date": "2026-11-02", "time": "14:00", "confirmation_id": "C001"}]}` | saved=[('M06', 'D001', 'new')] | `finalize` | 校验都过了，写入草稿（不发布） |
| 8 | 8 | `finalize` | `{"decisions": [{"material_id": "M04", "decision": "block", "reason": "存在阻断级风险 R2，不予排期：授权到期日 2026-10-31 早于拟发布日 2026-11-02，授权已失效。", "pending_items": ["[R2] 授权到期日早于发布日：需人工复审或修改后重排"]}, {"material_id": "M06", "decision": "pending_confirmation", "planned_date": "2026-11-02", "planned_time": "14:00", "reason": "存在警告级风险，已登记待确认后写入 14:00 草稿"}, {"material_id": "M08", "decision": "block", "reason": "存在阻断级风险 R6，不予排期：文案命中广告法敏感词：最强、绝对，须改写后复审。", "pending_items": ["[R6] 敏感词命中：需人工复审或修改后重排"]}]}` | decisions_accepted=3 | `结束（finalize 后收敛）` | — |

> 表中「决策依据」是模型（mock 模式下为固定策略）在发起该次调用时附带的一句说明；不包含模型内部思维链（`reasoning_content` 在接入层已丢弃）。「后续动作」由下一条记录的工具名给出。
> 完整工具返回体在对应的 `.jsonl` 里，逐行可解析。
