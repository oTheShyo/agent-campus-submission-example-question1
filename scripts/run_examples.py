"""一键跑完全部示例用例，产出 output JSON 与 trace。

  python scripts/run_examples.py --mode mock     # 离线，无 key，可直接复现
  python scripts/run_examples.py --mode live     # 真实模型，需要 .env

用例之间有依赖（重复提交、改期重排），所以固定顺序执行，不能乱序。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EX = ROOT / "examples"
VAR = ROOT / "var" / "examples"

# (输出名, 输入文件, 复用的 state 名, 额外环境变量)
CASES = [
    ("case1_normal", "case1_normal.input.json", "case1", {}),
    ("case2_missing_info", "case2_missing_info.input.json", "case2", {}),
    ("case3_rule_risk", "case3_rule_risk.input.json", "case3", {}),
    ("case4_slot_conflict", "case4_slot_conflict.input.json", "case4", {}),
    ("case5_duplicate.run1", "case1_normal.input.json", "dup", {}),
    ("case5_duplicate.run2", "case5_duplicate.input.json", "dup", {}),
    ("case6_save_failure", "case6_save_failure.input.json", "case6", {"SCHEDULER_FAULT": "save"}),
    ("case7_rerun_conflict", "case7_rerun_conflict.input.json", "case4", {}),
]


def run_case(name: str, input_file: str, state: str, extra_env: dict, mode: str, gap: float,
             case_timeout: float = 300.0) -> dict:
    out_path = EX / f"{name}.output.json" if mode == "live" else EX / f"{name}.output.mock.json"
    trace_path = EX / "traces" / mode / f"{name}.jsonl"
    # 以仓库相对路径传给 CLI，报告里的 trace_path 才不会写死本机目录
    trace_arg = trace_path.relative_to(ROOT).as_posix()
    state_path = VAR / f"{state}.state.json"
    if name.endswith(".run1") or name in ("case1_normal", "case2_missing_info", "case3_rule_risk",
                                          "case4_slot_conflict", "case6_save_failure"):
        # 这些是各自序列的第一次运行，先清掉旧状态，保证可复现
        if state_path.exists():
            state_path.unlink()

    env = dict(os.environ)
    env.update(extra_env)
    # Windows 中文环境下子进程默认按 GBK 输出，与下面的 utf-8 解码不匹配
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    cmd = [
        sys.executable, "-m", "src.main",
        "--input", str(EX / input_file),
        "--mode", mode,
        "--state", str(state_path),
        "--trace", trace_arg,
        "--out", str(out_path),
    ]
    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True,
                              encoding="utf-8", timeout=case_timeout)
        returncode, stdout, stderr = proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        returncode, stdout, stderr = 124, "", f"超时（>{case_timeout}s）"
    elapsed = time.time() - started

    status = "error"
    if out_path.exists():
        try:
            status = json.loads(out_path.read_text(encoding="utf-8")).get("status", "error")
        except json.JSONDecodeError:
            status = "error"

    print(f"[{name:26s}] exit={returncode} status={status:10s} {elapsed:5.1f}s  -> {out_path.name}")
    if returncode not in (0, 2):
        print((stdout or "")[-800:])
        print((stderr or "")[-800:])

    if gap:
        time.sleep(gap)
    return {"name": name, "exit": returncode, "status": status, "elapsed": round(elapsed, 1),
            "output": str(out_path.relative_to(ROOT)), "trace": str(trace_path.relative_to(ROOT))}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["mock", "live"], default="mock")
    ap.add_argument("--gap", type=float, default=None, help="用例之间的间隔秒数（限流端点建议 3~5）")
    ap.add_argument("--case-timeout", type=float, default=300.0, help="单个用例超时秒数")
    args = ap.parse_args()

    gap = args.gap if args.gap is not None else (3.5 if args.mode == "live" else 0.0)
    VAR.mkdir(parents=True, exist_ok=True)
    (EX / "traces" / args.mode).mkdir(parents=True, exist_ok=True)

    results = [run_case(name, inp, state, env, args.mode, gap, args.case_timeout)
               for name, inp, state, env in CASES]

    summary_path = EX / f"run-summary.{args.mode}.json"
    summary_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n共 {len(results)} 个用例，摘要写入 {summary_path.relative_to(ROOT)}")
    bad = [r for r in results if r["status"] != "completed"]
    if bad:
        print("非 completed 的用例：" + ", ".join(f"{r['name']}({r['status']})" for r in bad))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
