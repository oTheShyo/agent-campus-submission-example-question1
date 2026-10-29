"""把 JSONL trace 渲染成 markdown 表格，便于评审直接阅读。

  python scripts/render_trace.py examples/traces/live/*.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def render(path: Path) -> str:
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    start = next((e for e in events if e["type"] == "run_start"), {})
    end = next((e for e in events if e["type"] == "run_end"), {})
    calls = [e for e in events if e["type"] == "tool_call"]

    lines = [
        f"# Trace · {path.stem}",
        "",
        f"- 运行 ID：`{start.get('run_id')}`",
        f"- 模式：`{start.get('mode')}`　模型：`{start.get('model')}`",
        f"- 上限：{start.get('max_steps')} 步 / {start.get('max_tool_calls')} 次工具调用",
        f"- 结果：`{end.get('status')}`　结束原因：`{end.get('stop_reason')}`　"
        f"实际步数 {end.get('steps')}　工具调用 {end.get('tool_calls')}　tokens {end.get('tokens')}",
        "",
        "## 决策轨迹",
        "",
        "| # | 步 | 工具 | 参数 | 返回摘要 | 后续动作 | 决策依据（模型自述） |",
        "|---:|---:|---|---|---|---|---|",
    ]
    for i, c in enumerate(calls):
        args = json.dumps(c.get("args"), ensure_ascii=False)
        args = args.replace("|", "\\|")
        summary = str(c.get("result_summary", "")).replace("|", "\\|")
        rationale = (c.get("rationale") or "").replace("\n", " ").replace("|", "\\|") or "—"
        nxt = calls[i + 1]["tool"] if i + 1 < len(calls) else "结束（finalize 后收敛）"
        lines.append(
            f"| {i + 1} | {c.get('step')} | `{c.get('tool')}` | `{args}` | {summary} | `{nxt}` | {rationale} |"
        )

    lines += [
        "",
        "> 表中「决策依据」是模型（mock 模式下为固定策略）在发起该次调用时附带的一句说明；"
        "不包含模型内部思维链（`reasoning_content` 在接入层已丢弃）。「后续动作」由下一条记录的工具名给出。",
        "> 完整工具返回体在对应的 `.jsonl` 里，逐行可解析。",
        "",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    for raw in argv:
        p = Path(raw)
        out = p.with_suffix(".md")
        out.write_text(render(p), encoding="utf-8")
        print(f"{p} -> {out.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
