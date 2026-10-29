# examples · 用例与验收点对照

所有 `*.output.json` 是**真实模型**跑出来的结果；`*.output.mock.json` 是离线确定性策略的结果，
用于无 key 环境复现流程。trace 分两套目录，文件同名。

## 验收点 → 用例 → 文件

| 验收点 | 用例 | 输入 | 实际输出（live） | Trace（live） | 离线输出 |
|---|---|---|---|---|---|
| 正常排期 | case1 | `case1_normal.input.json` | `case1_normal.output.json` | `traces/live/case1_normal.md` | `case1_normal.output.mock.json` |
| 素材信息缺失 | case2 | `case2_missing_info.input.json` | `case2_missing_info.output.json` | `traces/live/case2_missing_info.md` | 同名 `.mock.json` |
| 规则风险 | case3 | `case3_rule_risk.input.json` | `case3_rule_risk.output.json` | `traces/live/case3_rule_risk.md` | 同名 `.mock.json` |
| 时段冲突 | case4 | `case4_slot_conflict.input.json` | `case4_slot_conflict.output.json` | `traces/live/case4_slot_conflict.md` | 同名 `.mock.json` |
| 重复提交 | case5（两次运行） | `case1_normal.input.json` + `case5_duplicate.input.json` | `case5_duplicate.run1.output.json` / `case5_duplicate.run2.output.json` | `traces/live/case5_duplicate.run{1,2}.md` | 同名 `.mock.json` |
| 保存失败 | case6 | `case6_save_failure.input.json` | `case6_save_failure.output.json` | `traces/live/case6_save_failure.md` | 同名 `.mock.json` |
| 换占用时段再提交 | case7 | `case7_rerun_conflict.input.json` | `case7_rerun_conflict.output.json` | `traces/live/case7_rerun_conflict.md` | 同名 `.mock.json` |
| 触达步数上限返回未完成 | case8 | 复用 `case4_slot_conflict.input.json` | `case8_step_limit.output.json` | `traces/live/case8_step_limit.md` | `case8_step_limit.output.mock.json` |

case8 不是新输入，而是把 case4 压到 3 步（`--max-steps 3`）跑出来的。复现命令：

```bash
python -m src.main --input examples/case4_slot_conflict.input.json --mode live \
  --state var/b.state.json --out examples/case8_step_limit.output.json \
  --trace examples/traces/live/case8_step_limit.jsonl --max-steps 3
# 期望：status=incomplete、stop_reason=step_cap、draft.status=not_attempted、退出码 2
```

## 人工同意闭环（pending → granted / rejected）

`examples/consent/` 是同一份 state 在三种答复下的结果，输入均复用 `case4_slot_conflict`，
模式为 mock（这里验的是流程与状态机，不是模型决策）：

| 文件 | 人工答复 | `consent_status` | `effective_status` | `draft.status` |
|---|---|---|---|---|
| `round1_pending.output.json` | 无 | `pending` | `pending_confirmation` | `saved` |
| `round2_granted.output.json` | `confirm.accept.json` | `granted` | `scheduled` | `deduplicated` |
| `round2_rejected.output.json` | `confirm.reject.json` | `rejected` | `blocked` | `not_attempted`（旧草稿转 `withdrawn`） |

复现（第二轮必须复用同一个 `--state`）：

```bash
python -m src.main --input examples/case4_slot_conflict.input.json --mode mock \
  --state var/c.state.json --out examples/consent/round1_pending.output.json --trace none
python -m src.main --input examples/case4_slot_conflict.input.json --mode mock \
  --state var/c.state.json --confirm-file examples/confirm.accept.json \
  --out examples/consent/round2_granted.output.json --trace none
```

每个 `.jsonl` 是与 `.md` 配套的原始事件流（一行一个事件，可直接 `jq` / Python 解析）。

## 用例之间的依赖（必须按此顺序跑）

```
case1  ──────────────► 独立
case2  ──────────────► 独立
case3  ──────────────► 独立
case4  ── 写 D001@19:00 ──► case7 复用 case4 的 state，把 19:00 也占掉，产生 rev2
case1  ── 写 D001/D002 ──► case5.run2 复用 case5 的 state，命中幂等
case6  ── 需 SCHEDULER_FAULT=save，独立
```

`scripts/run_examples.py` 已按上述顺序写好，不会乱序；手工复现时注意 `--state` 要指向同一个文件。

## 关键结论速查

| 用例 | 一句话结论 |
|---|---|
| case4 | `requested=18:00`、`suggested=19:00`、`saved`(rev1)、`C001` 待确认；**全报告无 published 字段** |
| case5.run2 | 两条素材均 `deduplicated`，revision 仍为 1，库中只有一份草稿 |
| case6 | `failed` + `STORE_WRITE_FAILED`，磁盘字节未变 |
| case7 | `saved` **rev2**、rev1 标 `superseded`、确认单 `C001` 标 `superseded` |
| case2 | M02 补查失败 → `missing_info` 且未写草稿；M09 `no_action`，不重复排期 |

## 其它材料

| 文件 | 说明 |
|---|---|
| `test-results.txt` | `pytest -v` 完整输出（85 passed） |
| `run-summary.live.json` | live 跑批的退出码、状态、耗时 |
| `run-summary.mock.json` | 离线跑批的同上摘要 |
| `confirm.accept.json` / `confirm.reject.json` | 人工答复样例，配合 `--confirm-file` 使用 |

## 关于 mock 的边界（请勿误读）

`*.output.mock.json` 里的 `model` 字段是 `mock-deterministic`，它是一段按优先级写死的策略，
只走 curl 一遍流程、验证护栏与报告层是否自洽。它**不调用任何模型**，
因此不能用来评价"Agent 决策得好不好"。决策质量证据只看 `traces/live/`。
