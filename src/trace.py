"""Trace 落盘：JSONL，一行一个事件，可直接 diff / grep。

记录的粒度按题目要求：「步骤、工具参数、返回摘要、后续动作及简要决策依据」。
不记录模型思维链。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

TZ = timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


def summarize(result: dict, limit: int = 400) -> str:
    """把工具返回压成一句话摘要，避免 trace 里塞满整份 JSON。"""
    if not isinstance(result, dict):
        return str(result)[:limit]
    if result.get("ok") is False:
        return f"FAILED[{result.get('error_code')}] {result.get('message', '')}"[:limit]

    tool = result.get("_tool")
    parts: list[str] = []
    if "free" in result:
        parts.append(f"free={result['free']}")
        parts.append(f"occupied={[o['time'] + ':' + o['material_id'] for o in result.get('occupied', [])]}")
    if "materials" in result and isinstance(result["materials"], list):
        parts.append(f"count={result.get('count', len(result['materials']))}")
        ids = [m.get("material_id") for m in result["materials"] if isinstance(m, dict)]
        if ids:
            parts.append(f"ids={ids}")
        for m in result["materials"]:
            if isinstance(m, dict) and m.get("risks"):
                parts.append(f"{m['material_id']} risks={[r['rule_id'] + ':' + r['severity'] for r in m['risks']]}")
            if isinstance(m, dict) and m.get("missing_required"):
                parts.append(f"{m['material_id']} missing={m['missing_required']}")
    if "results" in result:
        parts.append(f"found={[(r['field'], r['found']) for r in result['results']]}")
    if "saved" in result:
        parts.append(f"saved={[(s['material_id'], s['draft_id'], 'dedup' if s['deduplicated'] else 'new') for s in result['saved']]}")
    if result.get("rejected"):
        parts.append(f"rejected={[(r['material_id'], r['error_code']) for r in result['rejected']]}")
    if "confirmation_id" in result:
        parts.append(f"confirmation={result['confirmation_id']} status={result.get('status')}")
    if "accepted" in result and "invalid" in result:
        parts.append(f"decisions_accepted={result['accepted']}")
    if not parts:
        parts.append(json.dumps(result, ensure_ascii=False)[:limit])
    return (" ".join(parts) or "")[:limit]


class TraceWriter:
    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        self.events: list[dict] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text("", encoding="utf-8")

    def write(self, event: dict) -> None:
        event = {"ts": now_iso(), **event}
        self.events.append(event)
        if self.path:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
