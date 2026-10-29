# 04 · AI 使用记录

题目允许使用 AI 辅助，要求如实记录使用范围、个人贡献与验证结果。这里按"谁做了什么"和"AI 错在哪、怎么发现、怎么改"两部分写。

## 1. 工具与范围

用 WorkBuddy（对话式 AI 编码助手）完成主要实现，全程在同一个会话里推进：需求拆解 → 方案设计 → 写码 → 跑测试 → 跑真实模型 → 写文档。

| 环节 | AI 承担 | 我承担 |
|---|---|---|
| 需求理解 | 通读题目、列出 6 个验收点与"换时段再提交"这条隐藏考点 | 确认考点划分，决定扩到 7~8 个用例 |
| 技术选型 | 探测本机环境、实测两个端点的 function calling 与延迟 | 拍板：原生 ReAct + `openai` SDK + pytest，不用 LangGraph |
| 架构 | 按三层（模型/工具/报告）写实现 | 提出"关键缺陷要在架构上不可能发生"，定下"报告只认回执"这条主线 |
| 测试 | 编写 76 条 pytest | 指定必须覆盖的对抗点：越权改时段、虚报保存、触顶、幂等 |
| 真实模型跑批 | 写 `scripts/run_examples.py`，执行 8 个用例 | 审阅每条 trace，判断模型行为是否合理、是否需要改提示词 |
| 文档 | 起草 5 份文档与 README | 逐节核对数字与结论，删掉夸大表述 |

模型调用全部使用自己的 API key（中国科大端点 `qwen3.8-chat`），未使用评测方提供的任何凭据。

## 2. AI 犯过的错，以及怎么发现的

这部分比"AI 帮我写了多少行"更有信息量——三道错都是被测试或真实运行抓出来的，不是靠读代码看出来的。

### 错误 1：把"时段没变"误判成"时段被改"

报告层初版这样判断时段是否偏离：

```python
slot_changed = suggested != requested   # "2026-11-01 12:00" vs "2026-11-01T12:00"
```

两者只差一个分隔符，于是所有正常排期都被标成"已偏离用户要求，待确认"。
**发现方式**：跑 case1（正常排期）时人读摘要里出现了 `← 已偏离用户要求`，与预期不符。
**修改**：改成元组比较 `(date, time) != (req_date, req_time)`，并保留 `requested_slot` 原样输出。

### 错误 2：工具层遇到畸形参数把整个循环搞崩

工具分发只捕获了 `ToolRejected` 和 `TypeError`。真实模型跑 case2 时传了一个结构不对的
`decisions`，直接抛 `AttributeError: 'str' object has no attribute 'get'`，进程退出码 1，
整轮运行报废。

**发现方式**：live 跑批脚本里 case2 `status=error`，日志只有一行 traceback。
**修改**：
1. `dispatch` 兜底捕获所有异常，返回 `TOOL_ERROR` 让模型自己改参数重试；
2. 数组参数统一走 `_as_list` 容错（模型常把 `["M03"]` 写成 `"M03"`）；
3. 工具参数 JSON 被长度截断时不再静默降级，返回 `INVALID_JSON` 提示重发。

这件事也直接进了限制清单：**一次运行内的状态不是原子的**，崩溃前已写入的草稿会留在库里。

### 错误 3：素材自己已有的草稿被当成"别人占用"

`query_slots` 返回的占用里包含所有草稿，包括正在排期的这条素材自己的旧草稿。
于是重复提交时模型看到"我想排的时段被占了"，把素材挤到别的时段，产生多余的 revision。

**发现方式**：mock 模式下把 case1 连跑两次，第二次的 M03 从 12:00 跳到了 14:00。
**修改**：`query_slots` 增加 `for_material_ids` 参数，排除这些素材自己的草稿；
配套测试 `test_query_slots_excludes_own_draft_for_reschedule` 锁住行为。

### 错误 4：`decision` 枚举漏了一个真实状态

初版 `decision` 只有 `schedule / pending_confirmation / block`。保存失败时模型无路可走：
说 `schedule` 是虚报（会被 verifier 降级、整轮标 incomplete），说 `block` 又不对（素材本身没问题）。

**发现方式**：写"保存失败"用例的断言时，发现自己没法给这个场景写一个诚实的 decision。
**修改**：补 `save_failed`，并把"声称 save_failed 但一次都没尝试保存"也纳入一致性检查
（`SAVE_FAILED_WITHOUT_ATTEMPT`）——否则这个新枚举会变成新的撒谎口子。

## 3. 验证方式

所有结论都有可执行的复核入口，不靠截图：

| 结论 | 复核方式 |
|---|---|
| 护栏能挡住越权 | `python -m pytest tests/test_guards.py -v`（12 条） |
| 保存失败不改动磁盘 | `tests/test_store_failure.py`（断言失败前后文件字节相同） |
| 6 个验收点行为正确 | `tests/test_agent_loop.py`（脚本化模型逐场景断言） |
| 真实模型能走通并会调整动作 | `examples/traces/live/*.jsonl` + 对应 `.md` |
| mock 与 live 结论一致 | `examples/*.output.mock.json` 与 `*.output.json` 对照 |

## 4. 明确未做的事

- 没有让 AI 生成"看起来更漂亮"的性能数字。文档里出现的所有数字（76 条测试、8 个用例、
  每次运行的步数与 token 数、端点 20 RPM）都来自实际执行输出。
- 没有用 AI 编造业务规则。`data/rules.json` 里的 8 条规则是按短视频内容审核的常见约束拟的
  合成规则，不代表小影真实制度，文档与 README 都已注明。
- 没有隐藏 mock 与 live 的差别。mock 是确定性策略，README 与 docs/03 都写明它只能验证流程。
