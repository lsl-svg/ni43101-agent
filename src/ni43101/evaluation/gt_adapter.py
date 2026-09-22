"""Ground Truth 适配层。

需求方给的是"3 份真实 NI 43-101 PDF + ground truth JSON"，但我们无法假设对方 GT 的字段命名。
所以：所有 GT 都先经过**与抽取结果相同的归一化层**，再进入比较，保证"同单位、同口径"。

支持两种输入：
1. 规范格式（本项目 data/ground_truth/*.json）：tonnage_mt / grade_value / grade_unit / contained_t
2. 原始格式（带单位的文本字段）：tonnage_value + tonnage_unit / grade_value + grade_unit / contained_value + contained_unit

若实际 GT 的 key 不同，只需在 FIELD_ALIASES 里加一行即可，不用改任何评测逻辑。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..normalize import UnitError, canonicalize_raw_row
from ..schemas import ResourceRow
from .metrics import ALL_FIELDS

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    # 规范名 -> 可能出现的 key（中英文都留了位置）
    "category": ("category", "class", "resource_category", "resourceCategory", "类别", "资源类别"),
    "commodity": ("commodity", "metal", "element", "commodity_name", "矿种", "元素"),
    "tonnage_mt": ("tonnage_mt", "tonnage", "tonnes_mt", "ore_tonnage_mt", "矿石量_mt", "吨位_mt"),
    "grade_value": ("grade_value", "grade", "gradeValue", "品位"),
    "grade_unit": ("grade_unit", "grade_units", "gradeUnit", "品位单位"),
    "contained_t": ("contained_t", "contained", "contained_metal", "contained_metal_t", "金属量_t"),
    "tonnage_unit": ("tonnage_unit", "tonnage_units", "tonnageUnit", "吨位单位"),
    "contained_unit": ("contained_unit", "contained_units", "containedUnit", "金属量单位"),
    "page": ("page", "page_number", "页码"),
    "quote": ("quote", "evidence", "source_quote", "原文"),
}


def _get(raw: dict[str, Any], canonical: str) -> Any:
    for key in FIELD_ALIASES.get(canonical, (canonical,)):
        if key in raw and raw[key] not in (None, ""):
            return raw[key]
    return None


def normalize_gt_row(raw: dict[str, Any]) -> tuple[ResourceRow, set[str]]:
    """返回 (规范行, GT 中显式给出的字段集合)。显式字段才参与准确率分母。"""
    flat: dict[str, Any] = {"category": _get(raw, "category"), "commodity": _get(raw, "commodity")}
    for canonical in ("tonnage_mt", "grade_value", "grade_unit", "contained_t", "page", "quote"):
        flat[canonical] = _get(raw, canonical)

    explicit: set[str] = set()
    for canonical in ALL_FIELDS:
        if _get(raw, canonical) is None:
            continue
        explicit.add(canonical)

    # 规范格式里数值已是规范单位；原始格式则借助归一化层换算
    if _get(raw, "tonnage_mt") is not None and _get(raw, "tonnage_unit") is None:
        flat["tonnage_unit"] = "Mt"
    if _get(raw, "contained_t") is not None and _get(raw, "contained_unit") is None:
        flat["contained_unit"] = "t"

    row = canonicalize_raw_row(flat)
    # GT 里没有的字段不要被"补算"污染（否则等于拿自己的公式给自己打分）
    for name in ALL_FIELDS:
        if name not in explicit:
            setattr(row, name, None)
    return row, explicit


def load_gt(path: str | Path) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("rows") or data.get("resources") or []
    normalized: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for raw in rows:
        try:
            row, explicit = normalize_gt_row(raw)
        except (UnitError, ValueError) as exc:
            unresolved.append(f"GT 行无法归一化: {exc}")
            continue
        payload = {f: getattr(row, f) for f in ALL_FIELDS}
        payload = {k: (v.value if hasattr(v, "value") else v) for k, v in payload.items()}
        payload = {k: v for k, v in payload.items() if k in explicit}
        payload["_explicit"] = sorted(explicit)
        normalized.append(payload)
    return {
        "report_id": data.get("report_id") or Path(path).stem,
        "company": data.get("company"),
        "project": data.get("project"),
        "expect_abstain": bool(data.get("expect_abstain", False)),
        "unresolvable_fields": list(data.get("unresolvable_fields") or []) + unresolved,
        "rows": normalized,
    }
