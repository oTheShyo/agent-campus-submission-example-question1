# 素材审核与发布排期 Agent

小影科技校招 AI Agent 开发岗 · 项目题 1 交付物。

一个能自己决定"下一步查什么、改成哪个时段、要不要找人确认、还是直接阻断"的排期 Agent。
它**只写排期草稿，不接任何发布接口**，并且这套约束写死在架构里，不靠提示词祈祷模型自觉。

- 题目：项目题 1「素材审核与发布排期 Agent」
- 技术栈：Python 3.11+ / `openai` SDK（OpenAI 兼容端点）/ pytest，ReAct 循环自己实现，未使用 Agent 框架
- 最终 commit：`<!-- COMMIT_SHA -->`（push 后回填）

---

## 1. 30 秒跑起来（离线，无需 key）

```bash
pip install -r requirements.txt      # 只为 live 模式；mock 模式其实零第三方依赖
python -m src.main --input examples/case1_normal.input.json
```

想一次跑完全部 8 个用例（正常 / 信息缺失 / 规则风险 / 时段冲突 / 重复提交 / 保存失败 / 改期重排）：

```bash
python scripts/run_examples.py --mode mock
```

每个用例的 `stdout` 是 JSON 报告，`stderr` 是人读摘要。
单次运行默认把 trace 写到 `var/traces/<mode>/`（`var/` 不入库）；批量跑时 trace 落在 `examples/traces/`。

## 2. 跑真实模型

```bash
cp .env.example .env      # 填 OPENAI_API_BASE / OPENAI_API_KEY / AGENT_MODEL
python -m src.main --input examples/case4_slot_conflict.input.json --mode live
python scripts/run_examples.py --mode live --gap 3.5
```

默认接中国科大端点 `qwen3.8-chat`。换阿里云百炼只需改 `.env` 三行，代码不动
（`.env.example` 里已写好备选配置）。实测两者 function calling 都正常，
区别是 USTC 的 key 有 20 RPM 上限，所以客户端内置了 3.2 秒最小调用间隔与 429 退避。

> Mock 与真实模型的分工要说清楚：**mock 只证明流程和护栏是通的，不证明模型决策效果**；
> 真实模型的效果证据在 `examples/traces/live/`。

## 3. 评审导航

| 你要看什么 | 去哪 |
|---|---|
| 用户、范围、成功标准 | [docs/01-requirements.md](docs/01-requirements.md) |
| 调用流程、动态决策、技术取舍 | [docs/02-solution.md](docs/02-solution.md) |
| 6 个验收点怎么验、实际结果、失败与限制 | [docs/03-evaluation.md](docs/03-evaluation.md) |
| AI 辅助了哪些、我改了什么、怎么验证 | [docs/04-ai-usage.md](docs/04-ai-usage.md) |
| 最难的问题、学习、下一步 | [docs/05-retrospective.md](docs/05-retrospective.md) |
| 输入输出与 trace 对照表 | [examples/INDEX.md](examples/INDEX.md) |

## 4. 目录

```
src/
  main.py       CLI（退出码 0 完成 / 2 未完成 / 1 错误）
  agent.py      ReAct 循环：步数上限、工具调用上限、trace
  tools.py      7 个业务工具 + finalize + OpenAI function schema + 调用账本
  guards.py     硬护栏：规则引擎 + 保存前二次校验
  verifier.py   报告层：状态只从工具回执推导，拦截虚报
  store.py      数据与草稿存储（原子写、幂等指纹、故障注入）
  llm.py        模型接入（限速、429 退避、丢弃思维链）
  mock_brain.py 离线确定性策略（仅验证流程）
  report.py     人读摘要渲染
data/           12 条合成素材、8 条审核规则、基线排期、补查索引
tests/          76 条测试，全部离线
examples/       输入、实际输出、trace、对照表
docs/           5 份文档
```

## 5. 设计要点（细节见 docs/02）

**三层职责切分，让关键缺陷在结构上不可能发生：**

| 层 | 负责 | 不负责 |
|---|---|---|
| 模型 | 选动作：补查 / 换时段 / 请求确认 / 阻断 | 不决定最终状态字段 |
| 工具层 | 硬护栏：占用时段、阻断素材、未声明的时段偏离一律拒绝 | 不解释业务理由 |
| 报告层 | `review_status` / `draft.status` 只由规则事实与工具回执推导 | 不相信模型的任何断言 |

具体三条：

1. **没有"已发布"这个状态。** 审核状态只有 `ready/risk/missing_info/blocked`，草稿状态只有
   `saved/deduplicated/failed/not_attempted`，顶层 `publish_state` 恒为 `draft_only`。
   字段都不存在，越权发布就无处表达。
2. **用户的发布时间要求是只读的。** `requested_publish_at` 没有任何写入口；草稿要偏离它，
   必须带 `request_confirmation` 产生的凭证，否则 `save_schedule_draft` 直接拒绝。
   报告里 `requested_slot` 与 `suggested_slot` 并列输出，冲突调整一眼看得出。
3. **虚报保存成功会被抓住。** 若模型声称 `schedule` 但账本里没有成功保存回执，
   verifier 会把草稿状态降级为 `failed` 并打上 `CLAIMS_SCHEDULED_WITHOUT_RECEIPT`，
   整个运行标记为 `incomplete`。

## 6. 已知限制

- **只接了一个模型做主干。** 没有跨模型一致性对比，样本量小（8 个用例），
  结论是"这几条路径上行为正确"，不是"沙箱外普适正确"。
- **待确认项只登记不闭环。** `request_confirmation` 记录的问题需要人回答，
  本项目没有实现"人回答后回填并重新执行"的二次循环，草稿停在待确认状态等待处理。
- **时段网格是固定整点。** 真实业务可能需要 15 分钟粒度、平台差异化时段、节假日规则。
- **规则引擎是确定性的。** 敏感词用字面匹配，没有语义判定；换成模型判定会引入不确定性，
  但也有必要——现在这版会把"最强大脑"这类正常表达误判成敏感词。
- **没有并发控制。** 两个进程同时写同一 `--state` 文件会互相覆盖，生产环境需要锁或
  走数据库事务。
- **意图与边界**：不接发布、不接真实平台 API、不做排期审批流。

## 7. 复核建议

```bash
python -m pytest -q -p no:cacheprovider        # 76 passed
python scripts/run_examples.py --mode mock     # 8 个用例，约 5 秒
```

重点看两处：

- `src/guards.py::validate_save` —— 越权是怎么被挡住的
- `src/verifier.py::build_report` —— 状态是怎么由回执推导而不是由模型口述的
