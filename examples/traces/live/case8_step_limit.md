# Trace · case8_step_limit

- 运行 ID：`case4-conflict`
- 模式：`live`　模型：`qwen3.8-chat`
- 上限：3 步 / 16 次工具调用
- 结果：`incomplete`　结束原因：`step_cap`　实际步数 3　工具调用 3　tokens 9123

## 决策轨迹

| # | 步 | 工具 | 参数 | 返回摘要 | 后续动作 | 决策依据（模型自述） |
|---:|---:|---|---|---|---|---|
| 1 | 1 | `list_pending_materials` | `{"date": "2026-11-01"}` | count=5 ids=['M01', 'M02', 'M03', 'M05', 'M08'] | `read_material` | 我先列出待处理素材，确认 M01 在列。 |
| 2 | 2 | `read_material` | `{"material_ids": ["M01"]}` | count=1 ids=['M01'] | `check_rules` | M01 在列。读取 M01 的字段详情。 |
| 3 | 3 | `check_rules` | `{"material_ids": ["M01"], "planned_date": "2026-11-01"}` | count=1 ids=['M01'] | `结束（finalize 后收敛）` | 字段齐全，无需补查。现在检查规则。 |

> 表中「决策依据」是模型（mock 模式下为固定策略）在发起该次调用时附带的一句说明；不包含模型内部思维链（`reasoning_content` 在接入层已丢弃）。「后续动作」由下一条记录的工具名给出。
> 完整工具返回体在对应的 `.jsonl` 里，逐行可解析。
