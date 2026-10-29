"""数据读取与草稿存储。

两条硬规则：
1. 写盘用临时文件 + os.replace 原子替换，失败时磁盘字节不变。
2. 故障注入（SCHEDULER_FAULT=save）必须在**任何写入之前**抛出，
   保证"保存失败"用例里存储文件真的没被改动。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import StoreWriteError, norm_time

TZ = timezone(timedelta(hours=8))


def _now() -> str:
    return datetime.now(TZ).isoformat(timespec="seconds")


class DataStore:
    def __init__(
        self,
        data_dir: str | Path,
        state_path: str | Path,
        extra_schedule: list[dict] | None = None,
        fault: str | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.state_path = Path(state_path)
        self.extra_schedule = list(extra_schedule or [])
        self.fault = os.getenv("SCHEDULER_FAULT", "") if fault is None else fault

        doc = self._read_json(self.data_dir / "materials.json")
        self.materials: list[dict] = doc["materials"]
        self.materials_by_id: dict[str, dict] = {m["material_id"]: m for m in self.materials}

        rules = self._read_json(self.data_dir / "rules.json")
        self.rules_doc = rules
        self.rules: list[dict] = rules["rules"]
        self.slot_grid: list[str] = rules["slot_grid"]
        self.required_fields: list[str] = rules["required_fields"]
        self.optional_fields: list[str] = rules.get("optional_fields", [])
        self.sensitive_words: list[str] = rules["sensitive_words"]

        self.baseline: list[dict] = self._read_json(self.data_dir / "schedule.json")["entries"]
        self.asset_index: dict[str, dict] = self._read_json(self.data_dir / "asset_index.json")

        self.state = self._load_state()

    # ---------- 基础 IO ----------

    @staticmethod
    def _read_json(path: Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))

    def _load_state(self) -> dict:
        if self.state_path.exists():
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
            raw.setdefault("drafts", [])
            raw.setdefault("confirmations", [])
            raw.setdefault("rejections", [])
            return raw
        return {"drafts": [], "confirmations": [], "rejections": []}

    def _write_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + ".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_path)

    def _assert_writable(self) -> None:
        if self.fault == "save":
            raise StoreWriteError("草稿存储不可写（SCHEDULER_FAULT=save 注入）")

    # ---------- 排期占用 ----------

    def schedule_entries(self) -> list[dict]:
        """基线排期 + 输入补充排期 + 生效中的草稿，三者合并才算真实占用。"""
        entries = [dict(e, source=e.get("source", "baseline")) for e in self.baseline]
        for e in self.extra_schedule:
            entries.append(
                {
                    "date": e["date"],
                    "time": norm_time(e["time"]),
                    "material_id": e.get("material_id", "EXTERNAL"),
                    "source": "input",
                }
            )
        for d in self.state["drafts"]:
            if d.get("status") != "active":
                continue
            entries.append(
                {
                    "date": d["date"],
                    "time": d["time"],
                    "material_id": d["material_id"],
                    "source": "draft",
                }
            )
        return entries

    def occupancy(self, date: str, exclude_material_id: str | None = None,
                  exclude_ids: set[str] | None = None) -> dict[str, dict]:
        """某日 time -> 占用方。

        exclude_material_id / exclude_ids 用于改期：素材自己已有的草稿不算冲突，
        否则重新排期时会被自己挡在门外。
        """
        skip = set(exclude_ids or ())
        if exclude_material_id:
            skip.add(exclude_material_id)
        occ: dict[str, dict] = {}
        for e in self.schedule_entries():
            if e["date"] != date:
                continue
            if e["material_id"] in skip:
                continue
            occ[e["time"]] = {"material_id": e["material_id"], "source": e["source"]}
        return occ

    def free_slots(self, date: str, from_time: str | None = None,
                   exclude_material_id: str | None = None,
                   exclude_ids: set[str] | None = None) -> list[str]:
        occ = self.occupancy(date, exclude_material_id, exclude_ids)
        grid = self.slot_grid
        if from_time:
            start = norm_time(from_time)
            grid = [t for t in grid if t >= start] or list(self.slot_grid)
        return [t for t in grid if t not in occ]

    # ---------- 确认记录 ----------

    def add_confirmation(self, material_id: str, question: str, options: list[str], reason: str) -> dict:
        seq = len([c for c in self.state["confirmations"]]) + 1
        rec = {
            "confirmation_id": f"C{seq:03d}",
            "material_id": material_id,
            "question": question,
            "options": options,
            "reason": reason,
            "status": "pending",
            "created_at": _now(),
        }
        self.state["confirmations"].append(rec)
        self._write_state()
        return rec

    def get_confirmation(self, confirmation_id: str) -> dict | None:
        for c in self.state["confirmations"]:
            if c["confirmation_id"] == confirmation_id:
                return c
        return None

    # ---------- 草稿写入 ----------

    @staticmethod
    def fingerprint(item: dict) -> str:
        """幂等指纹：同一素材 + 同一目标时段 + 同一原始请求 → 同一份草稿。"""
        raw = "|".join(
            [
                str(item["material_id"]),
                str(item["date"]),
                norm_time(item["time"]),
                str(item.get("requested_at", "")),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def find_active_draft(self, material_id: str) -> dict | None:
        for d in self.state["drafts"]:
            if d["material_id"] == material_id and d.get("status") == "active":
                return d
        return None

    def find_draft_by_fingerprint(self, fp: str) -> dict | None:
        for d in self.state["drafts"]:
            if d["fingerprint"] == fp and d.get("status") == "active":
                return d
        return None

    def save_drafts(self, items: list[dict]) -> list[dict]:
        """写入草稿。命中幂等指纹 → 返回原草稿且不新增记录。"""
        self._assert_writable()

        before = json.dumps(self.state, ensure_ascii=False, sort_keys=True)
        results: list[dict] = []
        try:
            for item in items:
                fp = self.fingerprint(item)
                dup = self.find_draft_by_fingerprint(fp)
                if dup is not None:
                    results.append(
                        {
                            "material_id": item["material_id"],
                            "draft_id": dup["draft_id"],
                            "revision": dup["revision"],
                            "date": dup["date"],
                            "time": dup["time"],
                            "deduplicated": True,
                            "ok": True,
                        }
                    )
                    continue

                same_material = [d for d in self.state["drafts"] if d["material_id"] == item["material_id"]]
                revision = max([d["revision"] for d in same_material] + [0]) + 1
                draft_id = f"D{len(self.state['drafts']) + 1:03d}"

                for prev in same_material:
                    if prev.get("status") == "active":
                        prev["status"] = "superseded"
                        prev["superseded_by"] = draft_id
                        # 旧草稿的确认单随之作废，避免报告里堆一串过期的待确认项
                        if prev.get("confirmation_id"):
                            conf = self.get_confirmation(prev["confirmation_id"])
                            if conf is not None and conf.get("status") == "pending":
                                conf["status"] = "superseded"
                                conf["superseded_by"] = draft_id

                self.state["drafts"].append(
                    {
                        "draft_id": draft_id,
                        "material_id": item["material_id"],
                        "date": item["date"],
                        "time": norm_time(item["time"]),
                        "requested_at": item.get("requested_at", ""),
                        "deviates_from_request": bool(item.get("deviates_from_request", False)),
                        "confirmation_id": item.get("confirmation_id"),
                        "fingerprint": fp,
                        "revision": revision,
                        "status": "active",
                        "created_at": _now(),
                    }
                )
                results.append(
                    {
                        "material_id": item["material_id"],
                        "draft_id": draft_id,
                        "revision": revision,
                        "date": item["date"],
                        "time": norm_time(item["time"]),
                        "deduplicated": False,
                        "ok": True,
                    }
                )

            self._write_state()
            return results
        except Exception as exc:  # 原子性：失败回滚内存态，磁盘本就未改
            self.state = json.loads(before)
            if isinstance(exc, StoreWriteError):
                raise
            raise StoreWriteError(f"草稿写入失败：{exc}") from exc
