# 03 · 评估

## 1. 环境与复现命令

```
Python 3.13.14 / openai 3.41.0 / pytest 9.2.0
模型：qwen3.8-chat @ https://api.llm.ustc.edu.cn/v1（真实调用，非 mock）
日期：2026-10-29
```

```bash
python -m pytest -q -p no:cacheprovider      # 85 passed
python scripts/run_examples.py --mode mock    # 8 个用例，离线，约 5 秒
python scripts/run_examples.py --mode live    # 8 个用例，真实模型，约 6 分 35 秒
```

`examples/test-results.txt` 是 `pytest -v` 的完整输出，`examples/run-summary.live.json` 是 live 跑批的用时摘要。
`run_examples.py` 跑的是 case1~case7 共 8 次运行；第 9 个用例 case8（步数上限）需单独指定
`--max-steps 3`，命令见 `examples/INDEX.md`。
`run_examples.py` 跑的是 case1~case7 共 8 次运行；第 9 个用例 case8（步数上限）需单独指定
`--max-steps 3`，命令见 `examples/INDEX.md`。

## 2. 自动化测试（85 条，全部离线）

| 文件 | 条数 | 覆盖 |
|---|---:|---|
| `tests/test_rules.py` | 11 | R1~R8 逐条命中与不命中；干净素材保持沉默 |
| `tests/test_slots.py` | 9 | 空闲/占用、区间过滤、草稿占位、自身草稿排除 |
| `tests/test_store_idempotency.py` | 6 | 同指纹只一份、跨进程重复提交、revision、supersede 连带作废确认单 |
| `tests/test_store_failure.py` | 6 | 故障注入抛错、磁盘字节不变、内存态回滚、无 tmp 残留 |
| `tests/test_guards.py` | 11 | 越权保存的 8 种拒绝码 + 保存时重算规则 |
| `tests/test_tools.py` | 19 | 工具分发、参数畸形容错、JSON 二次编码、返回结构 |
| `tests/test_agent_loop.py` | 14 | 6 个验收场景 + 改期重排 + 虚报降级 + 两种上限 + 卡死保护 + trace 结构 |
| `tests/test_consent.py` | 9 | 同意主体：pending 不可自证、外部答复生效、reject 作废草稿、跨轮可见 |

## 3. 九个用例：预期 vs 实际

全部为真实模型输出。case1~case7 共 8 次运行 `status` 均为 `completed`、`stop_reason` 均为 `finalized`，
未触达任何上限；case8 是**故意把上限压到 3 步**的对照运行，结果为 `incomplete / step_cap`。

| # | 用例 | 关键预期 | 实际结果 | 步/调用 | 耗时 |
|---|---|---|---|---|---:|
| 1 | `case1_normal` | 两条无风险素材按原要求写入草稿 | M03 `ready/saved` rev1 @12:00；M05 `ready/saved` rev1 @20:00 | 6/6 | 41.3s |
| 2 | `case2_missing_info` | M02 补查失败→阻断；M07 可选字段缺失不影响；M09 已在排期不得重复排 | M02 `missing_info/not_attempted` + 待办；M07 `ready/saved`；M09 `blocked/no_action` | 6/7 | 51.4s |
| 3 | `case3_rule_risk` | M04 版权到期阻断；M06 时长超限可排但需确认；M08 敏感词阻断 | M04 `blocked`；M06 `risk/saved` + `C001` + `effective=pending_confirmation`；M08 `blocked` | 7/7 | 60.0s |
| 4 | `case4_slot_conflict` | 要求 18:00 被占 → 建议 19:00 并标待确认，**不得称为已发布** | `requested=2026-11-01T18:00`、`suggested=2026-11-01 19:00`、`slot_changed=true`、`saved` rev1、`consent_status=pending` + `C001` | 8/8 | 55.0s |
| 5 | `case5_duplicate` | 同一请求重复提交只产生一份草稿 | run1：`saved` rev1；run2：`deduplicated` rev1，库中仍 1 份 | 6/6 ×2 | 43.6s / 40.1s |
| 6 | `case6_save_failure` | 保存失败必须显式返回，不得虚报 | `draft.status=failed`、`error_code=STORE_WRITE_FAILED`、`decision=save_failed`，存储文件未变更 | 5/7 | 35.7s |
| 7 | `case7_rerun_conflict` | 把替代时段也占掉后重提交 → 新 revision 取代旧草稿 | `suggested=16:00`、`saved` **rev2**、旧草稿 `superseded`、`C002` | 8/8 | 68.2s |
| 8 | `case8_step_limit` | 触达步数上限时必须返回明确未完成，且不补写草稿 | `incomplete`、`stop_reason=step_cap`、`draft.status=not_attempted`、退出码 **2** | 3/3 | 18s |

用例文件对应关系见 `examples/INDEX.md`；每条用例都有 `.output.json`（结构化结果）、
`traces/live/*.jsonl`（逐行事件）与 `traces/live/*.md`（人读表格）三份材料。

## 4. 验收点覆盖

| 题目验收点 | 覆盖用例 | 判定依据 |
|---|---|---|
| 正常排期 | case1、case5.run1 | `ready` + `saved`，无待确认项 |
| 素材信息缺失 | case2（M02）、case2（M07 对照） | 先 `lookup_supplement`，补不到才 `missing_info` 且不写草稿 |
| 规则风险 | case3 | `block` 级不排期；`warn` 级排期但带确认 |
| 时段冲突 | case4、case7 | `requested_slot` 不变、`suggested_slot` 变化、带 `confirmation_id` |
| 重复提交 | case5 | 第二次 `deduplicated=true`，revision 不变，库中仅 1 份 |
| 保存失败 | case6 | `failed` + 错误码，磁盘未改 |
| 换占用时段再提交（审核要点） | case7 | rev2 生效，rev1 标 `superseded`，旧确认单随之作废 |
| ReAct 决策循环 | case4 第 8~9 步、case7 第 5 步 | 模型读到错误码后改变动作并收敛 |
| 触达上限返回未完成 | case8 | `status=incomplete`、`stop_reason=step_cap`、草稿 `not_attempted`、退出码 2 |
| 未经用户同意不得改变硬性要求 | case4 + `examples/consent/` 三态 | `consent_status` 只能由 `--confirm-file` 改变；模型自述"已同意"不生效 |

## 5. 三处值得单独看的轨迹

**① 模型被工具拒绝后自我修正（case4 第 8~9 步）**

模型第一次 `finalize` 时把 decisions 序列化成了被截断的字符串，工具返回
`INVALID_DECISIONS` 并给出合法格式；模型下一轮改发对象数组，直接收敛。
这是"观察结果 → 调整动作"最干净的证据，且不是脚本写死的。

**② 补查失败后改走阻断，而不是硬排（case2）**

`lookup_supplement` 对 M02 的三个字段全部返回 `found=false`。模型下一句是
「M02 三个必需字段都补不到，只能阻断」，随后把「补齐哪些字段」「找谁复审」写进待确认项。
如果它选了硬排，`save_schedule_draft` 也会用 `BLOCKING_RISK` 拒掉——两条路都通向同一个结论。

**③ 保存失败没有被粉饰（case6）**

`SCHEDULER_FAULT=save` 注入后，模型重试一次仍失败，最终 `decision=save_failed`，
报告里 `draft.status=failed`、`error_code=STORE_WRITE_FAILED`。
同时测试断言了故障前后存储文件字节完全相同，排除"写了一半"。

## 6. 同意边界：谁有资格说"用户同意了"

题目原文是「**未经用户同意**不得擅自改变硬性发布要求」。这句话里的"用户"必须是模型之外的人，
所以我把"同意"从提示词里的约束改成了数据流上的约束：

| 环节 | 谁能做 | 证据 |
|---|---|---|
| 提出待确认（`pending`） | 模型（工具 `request_confirmation`） | `issued_by=agent` |
| 把 `pending` 变成 `accepted`/`rejected` | **只有 `--confirm-file` 这条外部输入** | `answered_by` / `answered_at` |
| 报告里的 `consent_status` | 由确认单的 `answer` 推导 | `granted` 必须带人名 |

三态实测（`examples/consent/`，输入复用 case4，mock 模式验状态机）：

| 场景 | `consent_status` | `effective_status` | 草稿 |
|---|---|---|---|
| 模型登记后无人答复 | `pending` | `pending_confirmation` | `saved`（rev1） |
| 人答复 accept | `granted` | `scheduled` | `deduplicated`（复用同一份） |
| 人答复 reject | `rejected` | `blocked` | `not_attempted`，旧草稿转 `withdrawn` |

两个必须说清的边界：

1. **`pending` 时仍允许写草稿。** 因为草稿不是发布，题目参考实例也要求"建议 19:00 并标记待确认"，
   不给草稿等于把建议悬在空中。所以这里防的是"改动生效且无人知情"，不是"改动不存在"。
2. **`reject` 会连带作废草稿。** 这一点最初漏了：人否决后旧草稿还 `active`，就还在占 19:00 这个时段。
   现在 `apply_answers` 在 reject 时把关联草稿标 `withdrawn`，占用自动释放。
   `tests/test_consent.py::test_reject_withdraws_existing_draft` 锁住了这条。

同时补了一个此前没有的一致性检查：`effective_status` 由草稿事实 + 同意状态 + 未答复待办三者推导，
与模型自述的 `decision` 并列输出；两者不一致时打 `STALE_PENDING_DECISION`。
这条是跑 case3 时发现的——M06 有警告级风险、挂着待确认，我最初却把 `effective_status` 判成了 `scheduled`，
说明只看"时段是否偏离"不够，还要看是否仍有未答复的待办。

## 7. mock 与 live 的一致性

8 个用例中，mock 与 live 在
`(素材, 审核状态, 建议时段, 草稿状态, revision)` 这五元组上一致 **7/8**；
把 `consent_status`、`effective_status` 一并纳入比较，结果仍是 7/8。

唯一分歧在 case7：19:00 被 M03 占掉后，**live 模型选了 16:00，mock 选了 20:00**。
两者与用户要求的 18:00 都相差 2 小时，都合法，也都被正确标成 `pending_confirmation`。
这不矛盾——它说明这个用例本身留了两个等距合法解，而 mock 不是模型，分歧正好暴露在哪里。
我没有为了凑"全一致"去改 mock，因为那会把"mock 不等于模型"这件事藏起来。

其余 7 个用例完全一致，说明护栏与报告层是确定性的、用例设计在其余分支上没有歧义。
**一致性仍然不能说明 mock 具备模型能力**——它是按优先级写死的策略，遇到用例之外的输入不会泛化。
真实决策质量只能看 live 的 trace。

## 8. 失败与限制（都是实际踩到的，不是假想）

| 问题 | 现象 | 处理 |
|---|---|---|
| 工具参数被长度截断 | case4 第 8 步 decisions 是半截 JSON | 明确返回 `INVALID_DECISIONS` 让模型重发，不静默降级 |
| 数组被二次编码成字符串 | 模型传 `"decisions": "[{...}]"` | `_as_list` 先尝试 `json.loads`，解得开就用 |
| 工具畸形参数崩进程 | 一次 `AttributeError` 直接让整轮退出码 1 | `dispatch` 兜底捕获，转为 `TOOL_ERROR` 观察结果 |
| 模型卡在同一个错误上 | 曾连续 12 次重发同样非法参数，烧掉 30 分钟 | 同一工具同错误码连续 3 次 → `stop_reason=repeated_failure` 停下 |
| 决策枚举覆盖不全 | 「保存失败」「无需操作」两种真实状态无路可走 | 补 `save_failed` / `no_action`，并各自补上防滥用校验 |
| 崩溃非原子 | 一次崩溃后已写入的草稿留在库里 | 本轮未解决，写入限制清单 |
| 端点限流 | key 上限 20 RPM，批量跑会被限 | 3.2s 最小间隔 + 2/4/8s 退避 |
| 单次调用偶发很慢 | 观察到单步 13s（复杂上下文 + 长推理） | 用例级 240s 超时，超时记为 `error` 不静默通过 |

## 9. 没做到的

- **同意靠文件而非审批系统**：`--confirm-file` 能推进状态，但没有审批 UI、权限校验与答复留痕，
  生产环境应接工单/审批流。
- **样本量小**：9 个用例、1 个模型、每例跑一次。单次通过不等于稳定，可能需要多次重复统计。
- **未做跨模型对比**：百炼端点已实测可用（`qwen3.8-flash`，无 20 RPM 限制），但没有跑对照。
- **敏感词字面匹配**：会误伤正常表达，已知。
- **并发未验证**：没有任何多进程同时写同一 state 的测试，这是已知缺口。
