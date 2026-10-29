"""CLI 入口。

  python -m src.main --input examples/case1_normal.input.json            # 离线 mock
  python -m src.main --input examples/case1_normal.input.json --mode live  # 真实模型

退出码：0 运行完成 / 2 未完成（触达步数或工具调用上限、或有关键一致性缺陷）/ 1 输入或环境错误。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .agent import Agent
from .llm import OpenAICompatLLM
from .mock_brain import MockBrain
from .report import render_human
from .store import DataStore
from .tools import ToolBox

ROOT = Path(__file__).resolve().parents[1]


def load_env(path: Path | None = None) -> None:
    """极简 .env 读取：不覆盖已有环境变量。"""
    path = path or (Path.cwd() / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m src.main", description="素材审核与发布排期 Agent")
    p.add_argument("--input", required=True, help="输入 JSON 路径")
    p.add_argument("--mode", choices=["mock", "live"], default=None,
                   help="mock=离线确定性策略（默认）；live=真实模型")
    p.add_argument("--data-dir", default=str(ROOT / "data"), help="合成数据目录")
    p.add_argument("--state", default=None, help="草稿库状态文件，默认 var/<input名>.state.json")
    p.add_argument("--trace", default=None, help="trace 输出路径；传 'none' 关闭")
    p.add_argument("--out", default=None, help="把 JSON 报告写到该路径")
    p.add_argument("--max-steps", type=int, default=None)
    p.add_argument("--max-tool-calls", type=int, default=None)
    p.add_argument(
        "--confirm-file",
        default=None,
        help="人工对确认单的答复文件，形如 {\"C001\": {\"answer\": \"accept\", \"answered_by\": \"ops\"}}；"
             "这是确认单唯一的答复入口，模型无法代填",
    )
    p.add_argument("--quiet", action="store_true", help="不打印人读摘要")
    return p


def _derive_materials(store: DataStore, spec: dict) -> list[str]:
    if spec.get("materials"):
        return list(spec["materials"])
    focus = spec.get("focus_date")
    return [
        m["material_id"]
        for m in store.materials
        if m.get("status") == "pending"
        and (focus is None or (m.get("requested_publish_at") or "").startswith(focus))
    ]


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_env()

    input_path = Path(args.input)
    try:
        spec = json.loads(input_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(json.dumps({"status": "error", "error": f"输入文件读取失败：{exc}"}, ensure_ascii=False))
        return 1

    mode = args.mode or os.getenv("AGENT_MODE", "mock")
    stem = input_path.stem.replace(".input", "")
    state_path = Path(args.state) if args.state else ROOT / "var" / f"{stem}.state.json"
    # 默认 trace 写到 var/，避免随手跑一次就改写仓库里已提交的示例产物；
    # scripts/run_examples.py 会显式指定 examples/traces/ 下的路径。
    default_trace = ROOT / "var" / "traces" / mode / f"{stem}.jsonl"
    if args.trace == "none":
        trace_path = None
    elif args.trace:
        trace_path = Path(args.trace)
    else:
        trace_path = default_trace

    fault = os.getenv("SCHEDULER_FAULT", "")
    try:
        store = DataStore(args.data_dir, state_path, extra_schedule=spec.get("extra_schedule"), fault=fault)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": f"数据加载失败：{exc}"}, ensure_ascii=False))
        return 1

    spec["materials"] = _derive_materials(store, spec)

    # 人工答复必须在工具跑起来之前写入：这是确认单从 pending 变成 accept/reject 的唯一通道
    if args.confirm_file:
        try:
            answers = json.loads(Path(args.confirm_file).read_text(encoding="utf-8"))
            if not isinstance(answers, dict):
                raise ValueError("顶层必须是对象：{\"C001\": {\"answer\": \"accept\"}}")
        except Exception as exc:
            print(json.dumps({"status": "error", "error": f"confirm-file 读取失败：{exc}"},
                             ensure_ascii=False))
            return 1
        spec["confirm_answers"] = store.apply_answers(answers)
        applied = spec["confirm_answers"]["applied"]
        bad = spec["confirm_answers"]["rejected"]
        if bad:
            print(f"[confirm-file] 忽略无效答复：{bad}", file=sys.stderr)
        if applied:
            print(f"[confirm-file] 已应用 {len(applied)} 条人工答复", file=sys.stderr)

    toolbox = ToolBox(store)
    if mode == "live":
        api_key = os.getenv("OPENAI_API_KEY", "")
        if not api_key or api_key.startswith("sk-replace"):
            print(json.dumps({"status": "error",
                              "error": "live 模式需要 OPENAI_API_KEY（参考 .env.example）；也可用 --mode mock 离线运行"},
                             ensure_ascii=False))
            return 1
        llm = OpenAICompatLLM(
            model=os.getenv("AGENT_MODEL", "qwen3.8-chat"),
            base_url=os.getenv("OPENAI_API_BASE", "https://api.llm.ustc.edu.cn/v1"),
            api_key=api_key,
            timeout=float(os.getenv("AGENT_TIMEOUT", "60")),
            min_interval=float(os.getenv("AGENT_MIN_INTERVAL", "3.2")),
        )
    else:
        llm = MockBrain(store, spec)

    agent = Agent(
        store,
        toolbox,
        llm,
        trace_path=str(trace_path) if trace_path else None,
        max_steps=args.max_steps or int(os.getenv("AGENT_MAX_STEPS", "12")),
        max_tool_calls=args.max_tool_calls or int(os.getenv("AGENT_MAX_TOOL_CALLS", "16")),
        mode=mode,
    )

    try:
        report = agent.run(spec)
    except Exception as exc:
        print(json.dumps({"status": "error", "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=False))
        return 1

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.quiet:
        print(render_human(report), file=sys.stderr)

    return 0 if report["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
