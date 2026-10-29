# 01 · 需求

## 1. 用户

内容团队的运营（排期负责人）。日常动作是：拿到一批待发素材 → 判断每条能不能发 → 找空闲时段 → 排期 → 交给发布环节。

现在这套动作靠人肉在群里对表：素材资料散落、版权日期靠记、时段靠抢、改期靠喊。痛点不是"算不出时间"，
而是**没人担保这一版排期是不是干净的**：有没有踩版权、有没有撞时段、有没有和用户要求不一致还不吭声。

## 2. 目标

让 Agent 接收一批素材，输出一份**可确认的**排期草稿：

- 每条素材的审核状态与风险原因
- 建议时段（以及它和用户要求是否一致）
- 需要人拍板的待确认项
- 草稿到底存没存上

关键在于"可确认"三个字：Agent 不替人做决定，它把需要人决定的部分**显式挖出来**，
其余的按规则和事实填好。

## 3. 输入

| 输入 | 来源 | 说明 |
|---|---|---|
| 素材 ID 与元数据 | `data/materials.json` | 12 条合成素材：标题、创作者、时长、版权、赞助商、文案 |
| 发布时间要求 | 同上 `requested_publish_at` | 用户的原始要求，**只读**，Agent 无权修改 |
| 已有排期 | `data/schedule.json` + 输入里的 `extra_schedule` | 基线排期 + 运营临时占位 |
| 审核规则 | `data/rules.json` | 8 条规则，数据驱动 |
| 补查索引 | `data/asset_index.json` | 模拟另一套素材库，部分字段查得到、部分查不到 |

## 4. 输出

一份 JSON 报告（`stdout`），每条素材固定这几个字段：

```
material_id / title
review_status     ready | risk | missing_info | blocked
risk_reasons[]    命中的规则 id + 人话原因
requested_slot    用户原始要求（只读）
suggested_slot    建议的时段（可能与要求不同）
slot_changed      是否偏离了用户要求
draft{status, draft_id, revision, deduplicated, error_code}
pending_confirmations[]
decision / decision_reason
consistency_flags[]  交叉校验发现的矛盾
```

外加运行级字段：`status`(completed/incomplete)、`stop_reason`、`budget`(步数/工具调用/上限)、`publish_state`。

**`publish_state` 恒为 `draft_only`。** 报告里不存在"已发布"这个取值。

## 5. 范围

做：

- 一次处理一批（2~3 条）素材，逐条给出结论
- 信息缺失先补查，补不到才阻断
- 规则风险按 `block` / `warn` 分级处理
- 时段冲突时查后续空闲、给替代方案、走确认流程
- 幂等保存（同一请求只产生一份草稿），改期产生新 revision
- 保存失败显式返回失败
- 步数/工具调用上限，触顶返回 `incomplete`

不做（明确写在限制里）：

- 不接真实发布接口，不调平台 API
- 不接真实审批系统：`--confirm-file` 只模拟人工答复，没有权限校验、审批 UI 与答复留痕
- 不做多 Agent 协作、不做 Web 界面、不做线上部署
- 不做并发的排期锁

## 6. 成功标准

按题目 6 个必验收点，逐条可判定：

| 验收点 | 通过判据 |
|---|---|
| 正常排期 | 无风险素材在其要求时段成功写入草稿，`review_status=ready`、`draft.status=saved` |
| 素材信息缺失 | 缺必需字段 → 先触发补查；补不到 → `missing_info`，不写草稿，给出补件清单 |
| 规则风险 | `block` 级 → `blocked` 且不写草稿；`warn` 级 → 可排但必须带待确认项 |
| 时段冲突 | 要求时段被占 → 给出替代时段，`slot_changed=true`，带确认凭证，`requested_slot` 原样保留 |
| 重复提交 | 同一请求第二次保存 → `draft.status=deduplicated`，库中仍只有一份草稿，revision 不变 |
| 保存失败 | `draft.status=failed` + `error_code`，且存储文件字节不变，绝不出现 `saved` |

另加三条自设标准：

- **越权可判定**：任何报告里的 `draft.status` 必须有对应工具回执；无回执即 `incomplete`。
- **同意主体可判定**：`consent_status=granted` 必须来自 `--confirm-file` 的人工答复并带人名；
  模型自称"用户已同意"不产生任何效果。
- **可复现**：`python scripts/run_examples.py --mode mock` 5 秒内跑完全部用例，无需 key。

## 7. 关键假设（评审时请注意）

1. 素材的 `requested_publish_at` 视为**硬性要求**。真实业务里可能分"硬性/软性"，本项目全部按硬性处理，
   使得"未经同意不得改动"这条约束有明确的适用对象。
2. "待确认"等于"草稿写好了但在等人点头"，不等于"没排"。报告用 `pending_confirmation` 和
   `saved` 两个独立字段分别表达，避免把两件事混成一件。
3. 时段是固定整点网格（`09:00`~`21:00` 共 10 个），排期只能落在网格上，否则拒绝。
4. 素材是合成的，规则是按短视频内容审核的常见约束拟的，不代表小影真实业务规则。
