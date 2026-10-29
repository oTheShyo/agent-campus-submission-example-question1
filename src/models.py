"""共用常量、枚举与字段读取工具。

设计约束：**报告里不存在 "published" 这个取值**。
审核状态只有 4 个，草稿状态只有 4 个；因此"虚报已发布"在数据结构层面无处安放。
"""

from __future__ import annotations

import re
from typing import Any

# 审核状态：由规则事实决定，不接受模型自由发挥
REVIEW_STATUSES = ("ready", "risk", "missing_info", "blocked")

# 草稿状态：只由工具回执决定
DRAFT_STATUSES = ("saved", "deduplicated", "failed", "not_attempted")

# 运行状态
RUN_STATUSES = ("completed", "incomplete")

_MISSING = object()


def get_field(obj: Any, path: str) -> Any:
    """按 "rights.license" 这类点号路径取值，缺失返回 _MISSING 哨兵。"""
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def field_present(obj: Any, path: str) -> bool:
    val = get_field(obj, path)
    if val is _MISSING or val is None:
        return False
    if isinstance(val, str) and not val.strip():
        return False
    return True


def flatten_fields(material: dict) -> dict:
    """把嵌套素材拍平成点号键，便于模型阅读与比对。"""
    out: dict[str, Any] = {}

    def walk(node: dict, prefix: str = "") -> None:
        for key, val in node.items():
            if key.startswith("_"):
                continue
            path = f"{prefix}{key}"
            if isinstance(val, dict):
                walk(val, path + ".")
            else:
                out[path] = val

    walk(material)
    return out


def missing_fields(material: dict, required: list[str]) -> list[str]:
    return [f for f in required if not field_present(material, f)]


def publish_date_of(material: dict) -> str:
    """素材要求的发布时间 → 日期部分。"""
    ts = material.get("requested_publish_at") or ""
    return ts.split("T")[0]


def publish_time_of(material: dict) -> str:
    ts = material.get("requested_publish_at") or ""
    return ts.split("T")[1] if "T" in ts else ""


def norm_time(value: str) -> str:
    """把 19:00 / 19:0 / 1900 统一成 HH:MM，便于和时段网格比对。"""
    raw = str(value or "").strip()
    m = re.match(r"^(\d{1,2}):?(\d{2})$", raw)
    if not m:
        return raw
    return f"{int(m.group(1)):02d}:{m.group(2)}"


class StoreWriteError(RuntimeError):
    """草稿写盘失败。必须显式向上返回，绝不能被吞掉当作成功。"""

    code = "STORE_WRITE_FAILED"


class ToolRejected(Exception):
    """工具层硬护栏拒绝。带 error_code，由工具转成结构化返回值。"""

    def __init__(self, code: str, message: str, detail: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail or {}
