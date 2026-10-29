# Trace · case2_missing_info

- 运行 ID：`case2-missing`
- 模式：`live`　模型：`qwen3.8-chat`
- 上限：12 步 / 16 次工具调用
- 结果：`completed`　结束原因：`finalized`　实际步数 6　工具调用 7　tokens 22845

## 决策轨迹

| # | 步 | 工具 | 参数 | 返回摘要 | 后续动作 | 决策依据（模型自述） |
|---:|---:|---|---|---|---|---|
| 1 | 1 | `list_pending_materials` | `{}` | count=8 ids=['M01', 'M02', 'M03', 'M04', 'M05', 'M06', 'M07', 'M08'] | `read_material` | 我先列出待处理素材，确认 M02、M07、M09 的状态。 |
| 2 | 2 | `read_material` | `{"material_ids": ["M02", "M07", "M09"]}` | count=3 ids=['M02', 'M07', 'M09'] M02 missing=['duration_sec', 'rights.license', 'rights.expires_on'] | `lookup_supplement` | M09 不在待处理列表里，我先读取 M02、M07、M09 的元数据核对情况。 |
| 3 | 3 | `lookup_supplement` | `{"material_id": "M02", "fields": ["duration_sec", "rights.license", "rights.expires_on"]}` | found=[('duration_sec', False), ('rights.license', False), ('rights.expires_on', False)] | `check_rules` | M09 已确认在排期中（status=scheduled），无需操作。M02 缺 3 个必需字段，先补查；M07 字段齐全，直接查风险。 |
| 4 | 3 | `check_rules` | `{"material_ids": ["M07"], "planned_date": "2026-11-02"}` | count=1 ids=['M07'] | `query_slots` | M09 已确认在排期中（status=scheduled），无需操作。M02 缺 3 个必需字段，先补查；M07 字段齐全，直接查风险。 |
| 5 | 4 | `query_slots` | `{"date": "2026-11-02", "from_time": "12:00", "for_material_ids": ["M07"]}` | free=['12:00', '14:00', '18:00', '19:00', '20:00', '21:00'] occupied=['16:00:M12'] | `save_schedule_draft` | M02 三个必需字段外部库都查不到，无法补，只能阻断。M07 无风险，查 11-02 12:00 时段占用。 |
| 6 | 5 | `save_schedule_draft` | `{"items": [{"material_id": "M07", "date": "2026-11-02", "time": "12:00"}]}` | saved=[('M07', 'D001', 'new')] | `finalize` | M07 要求的 12:00 空闲，直接保存草稿。 |
| 7 | 6 | `finalize` | `{"decisions": [{"material_id": "M02", "decision": "block", "reason": "外部渠道素材，必需字段 duration_sec、rights.license、rights.expires_on 均缺失，外部素材库无记录无法补查，信息不足不得排期。", "pending_items": ["请内容运营-小林向素材提供方（剪辑阿哲/外部渠道）补齐：视频时长 duration_sec、授权类型 rights.license、授权到期日 rights.expires_on", "补齐后重新提交排期"]}, {"material_id": "M07", "decision": "schedule", "planned_date": "2026-11-02", "planned_time": "12:00", "reason": "字段齐全、无风险，用户要求的 2026-11-02 12:00 时段空闲，草稿已保存（D001）。"}, {"material_id": "M09", "decision": "no_action", "reason": "素材已在排期中（status=scheduled，already_scheduled=true），用户要求核对，确认无需重复操作。"}]}` | decisions_accepted=3 | `结束（finalize 后收敛）` | M07 草稿已保存（D001）。M02 因必需字段缺失且外部库查不到，阻断。M09 已在排期中，无需操作。提交最终结论。 |

> 表中「决策依据」是模型（mock 模式下为固定策略）在发起该次调用时附带的一句说明；不包含模型内部思维链（`reasoning_content` 在接入层已丢弃）。「后续动作」由下一条记录的工具名给出。
> 完整工具返回体在对应的 `.jsonl` 里，逐行可解析。
