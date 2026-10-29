# 02 · 方案

## 1. 一句话

原生 ReAct 循环 + 工具层硬护栏 + 回执驱动的报告层。框架能省的那点胶水，
换不来"每一层为什么这么切"的可解释性，所以没用框架。

## 2. 调用链

```
                  ┌──────────────────────────────────────────┐
   输入 JSON ───▶ │ Agent.run(spec)                          │
                  │  messages = [system, user(spec)]         │
                  └───────────────┬──────────────────────────┘
                                  │  循环（≤12 步 / ≤16 次工具调用）
                    ┌─────────────▼──────────────┐
                    │ LLM.complete(messages)     │◀──────────┐
                    │  ├ 有 tool_calls → 执行     │           │
                    │  └ 无 tool_calls → 结束     │           │
                    └─────────────┬──────────────┘           │
                                  │ 工具调用                  │
                    ┌─────────────▼──────────────┐           │
                    │ ToolBox.dispatch(name,args) │           │
                    │  ├ guards 校验（硬护栏）    │           │
                    │  ├ store 读写（原子/幂等）  │           │
                    │  └ 记入 ledger + trace      │───────────┘
                    └─────────────┬──────────────┘   工具结果回灌 messages
                                  │
                    ┌─────────────▼──────────────┐
                    │ verifier.build_report()     │
                    │  规则事实 + 工具回执 → 状态 │
                    │  模型只贡献 decision/reason │
                    └─────────────┬──────────────┘
                                  ▼
                        报告 JSON + 人读摘要 + trace
```

三层各管一件事，边界是硬的：

| 层 | 输入 | 输出 | 有没有自由裁量 |
|---|---|---|---|
| 模型 | 工具返回的事实 | 下一个动作 + 理由 | 有，且只有它 |
| 工具层 | 模型给的动作参数 | 成功/拒绝 + 结构化事实 | 无，纯确定性 |
| 报告层 | 规则事实 + 工具账本 | 状态字段 | 无，纯推导 |

## 3. 一次真实决策长什么样（case4，题目参考实例）

用户要求 M01 在 `2026-11-01 18:00` 发布，该时段已被 M10 占用。

| # | 模型动作 | 工具参数 | 观察到什么 | 模型的依据（trace 原文摘录） |
|---|---|---|---|---|
| 1 | `list_pending_materials` | `2026-11-01` | 当天 5 条待处理，M01 在列 | 「先列出待处理素材，确认 M01 在列」 |
| 2 | `read_material` | `["M01"]` | 字段齐全 | 「字段齐全，无缺失」 |
| 3 | `check_rules` | `["M01"]`, 11-01 | 无风险、无阻断 | 「无风险、无阻断」 |
| 4 | `query_slots` | `2026-11-01` | 18:00 被 M10 占，19:00/20:00 空闲 | 「确认 18:00 是否可用」 |
| 5 | `query_slots` | `from_time=18:00` | 18:00 之后只剩 19:00、20:00 | 「18:00 已被 M10 占用，与客户指定时段冲突，查 18:00 之后还有哪些空闲」 |
| 6 | `request_confirmation` | 「18:00 被占，改 19:00？」 | `C001`，pending | 「客户指定时段是硬性要求，冲突需走确认流程，先登记待确认项」 |
| 7 | `save_schedule_draft` | `19:00` + `C001` | `D001` rev1 写入 | 「已拿到 confirmation_id，带凭证保存草稿」 |
| 8 | `finalize` | decisions 被截断成非法 JSON | `INVALID_DECISIONS` | 「finalize 解析失败，我重新以对象数组形式提交」 |
| 9 | `finalize` | 合法对象数组 | accepted=1 | 收敛结束 |

报告呈现为：`requested_slot=2026-11-01T18:00`、`suggested_slot=2026-11-01 19:00`、
`slot_changed=true`、`draft.status=saved`、`pending_confirmations=[C001]`。

这段轨迹来自 `examples/traces/live/case4_slot_conflict.jsonl`，是真实模型跑出来的，不是设计稿。
第 8~9 步值得单独看：模型输出被长度截断成非法 JSON，工具返回一个可读的错误码，
模型自己重发正确格式并收敛——这就是"观察结果 → 调整下一步"最直接的证据。

另外补一条：如果模型**没查就直接去存 18:00**，`save_schedule_draft` 会用 `SLOT_OCCUPIED` 拒掉
（脚本化用例 `test_slot_conflict_m01_suggests_1900` 覆盖了这条分支），所以"先查"不是靠模型自觉，
而是查不查都不会写脏数据。

## 4. 动态决策点

模型在每个岔路口看到的都是工具返回的事实，不是预设分支：

| 观察到 | 可选动作 | 系统约束 |
|---|---|---|
| `missing_required` 非空 | 补查 / 阻断 / 带缺字段硬排 | 硬排一定被拒（`BLOCKING_RISK`），只剩前两条 |
| 补查 `all_found=false` | 阻断 / 继续尝试别的字段 | 天然只剩阻断 |
| `blocking=true` | 排 / 阻断 | 排一定被拒 |
| `warn` 级风险 | 直接排 / 登记确认后排 | 直接排会被拒（无 `confirmation_id`） |
| 要求时段 `SLOT_OCCUPIED` | 换到后续空闲 / 换天 / 阻断 | 换时段需带确认凭证 |
| 保存返回 `STORE_WRITE_FAILED` | 重试 / 如实报失败 | 重试可，但重试仍失败必须报 `save_failed` |
| 保存返回 `deduplicated=true` | 视为成功 / 换个时段绕开 | 提示词明确要求视为成功，绕开会产生多余 revision |

## 5. 工具设计取舍

**为什么 `request_confirmation` 是独立工具，而不是 `save` 的一个布尔参数？**

因为"登记待确认项"和"写草稿"是两件事，且顺序有语义：先有凭证，才能带着偏离写入。
拆开以后，护栏可以在 `save` 时校验凭证的**来源**（是不是本素材的、是不是真实存在），
而不是相信模型传了一个 `skip_check=true`。

**为什么 `read_material` / `check_rules` 收数组？**

限流。USTC 端点的 key 是 20 RPM，一条素材一次调用的话，3 条素材光读取就 3 次。
批量化把一轮 run 压到 6~9 次调用，既省配额也让 trace 短到能读完。

**为什么 `query_slots` 有 `for_material_ids`？**

改期场景里，素材自己已有的草稿会被算成"占用"，于是模型看到"我想排的时段被我自己占了"，
白白绕开。这个参数把这个坑填掉——测试 `test_query_slots_excludes_own_draft_for_reschedule`
锁住了这个行为。

**为什么没有 `publish` 工具？**

因为不许。没有这个工具，模型连"尝试发布"的机会都没有——这比在提示词里写十遍"不要发布"管用。

## 6. 护栏细节

`guards.validate_save` 逐条校验，任一不过就整条拒绝（不是部分写入）：

| 拒绝码 | 触发条件 |
|---|---|
| `MATERIAL_NOT_FOUND` | 素材不存在 |
| `INVALID_SLOT` | 时段不在网格内 / 缺 date、time |
| `BLOCKING_RISK` | 保存时**重算**规则，存在 block 级风险 |
| `SLOT_OCCUPIED` | 目标时段被基线排期、输入补充排期或他人草稿占用 |
| `DEVIATION_WITHOUT_CONFIRMATION` | 目标时段 ≠ 用户要求，且没带 `confirmation_id` |
| `UNKNOWN_CONFIRMATION` | 凭证不存在，或属于别的素材 |
| `DUPLICATE_ITEM` | 同一次请求里同一素材出现两次 |

其中 `BLOCKING_RISK` 是**保存时重算**的，不是复用模型之前 `check_rules` 的结论。
这样"先查一个安全日期、再改到危险日期保存"的时序绕过不了（`test_rules_are_rechecked_at_save_time`）。

## 7. 幂等与 revision

指纹 = `sha256(material_id | date | time | requested_publish_at)` 取前 16 位。

- 同指纹已存在 → 返回原 `draft_id`、`deduplicated=true`，**不新增记录、revision 不变**
- 同素材不同时段 → 新 revision，旧草稿标 `superseded`，旧草稿的确认单同步标 `superseded`
- 指纹里不含 `request_id`：同一请求重复提交当然同指纹，换了请求 ID 也照样识别为重复

这与"素材已在基线排期"是两件事：后者由规则 `R8` 报阻断，语义是"这条不该被自动再排一次"。

## 8. 上限与未完成

- `max_steps`（默认 12）：LLM 回合数
- `max_tool_calls`（默认 16）：工具调用总次数
- 任一触顶 → `status=incomplete`、`stop_reason=step_cap|tool_call_cap`、退出码 **2**，
  且**不再补写任何草稿**（触顶后不"友好地帮你把剩下的排了"）
- 模型只 `finalize` 了一部分素材 → 缺的那条打 `MISSING_DECISION`，运行同样 `incomplete`

## 9. 限流

USTC 端点实测 `x-litellm-key-rpm-limit: 20`，即 3.2 秒/次。客户端做两件事：

1. 调用前不足 `AGENT_MIN_INTERVAL`（默认 3.2s）就补足等待
2. 429 / 5xx / 超时按 `2s → 4s → 8s` 退避，最多 3 次

退避事件记进 `OpenAICompatLLM.retry_events`，trace 里能看到"这次慢了是因为被限流"。

## 10. 技术取舍

| 选择 | 备选 | 为什么不选备选 |
|---|---|---|
| 原生 ReAct 循环（~80 行） | LangGraph / ADK / AgentScope | 本项目只有一个循环、8 个工具，框架换来的是"图长什么样"的仪式，代价是把护栏和报告校验挤到框架回调里，说不清谁负责什么。题目也明说框架名不加分 |
| `openai` SDK | 纯 urllib | SDK 天然支持任意 OpenAI 兼容端点，换 USTC/百炼/DeepSeek 只改 `base_url`；自己写 HTTP 层反而多一处易错点 |
| 规则数据化（`data/rules.json` + 8 个 handler） | 规则写死在代码里 | 加一条同类型规则不用改代码；也让测试可以直接构造边界素材 |
| 报告由回执推导 | 让模型输出结构化 JSON 结论 | 模型输出 JSON 也能跑，但"它说存好了"和"真的存好了"就分不开了。这条是本项目最核心的取舍 |
| 一进程一 state 文件 | SQLite | 8 个用例各自独立，文件够用且能直接 diff；并发与事务是明确写进限制里的缺口 |
| 丢弃 `reasoning_content` | 存进 trace 做分析 | 题目要求不提交模型内部思维过程；而且思维链对评审没有增量信息，只会让 trace 变脏 |
