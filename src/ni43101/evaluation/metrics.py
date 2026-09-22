"""评测层（对应评测交付清单里的评分协议）。

放在包内（src/ni43101/evaluation）而不是顶层 `eval/`，是为了避免与内置函数 `eval` 同名导致
潜在的导入歧义——评审时可直接看 `python -m ni43101.cli eval`。

评分协议（按设计要求）：
- 数值字段：相对容差 ±5% 判对；
- 类别字段（category / commodity / grade_unit）：归一化后精确匹配；
- ground truth 里为 null 的字段不计入分母（属于"不可确定"，用来测 abstain 而不是测准确率）；
- 额外统计**硬给率**：本应拒答（GT 标注 expect_abstain，或该案例确有错值）却输出了结论的比例。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

CATEGORICAL_FIELDS = ("category", "commodity", "grade_unit")
NUMERIC_FIELDS = ("tonnage_mt", "grade_value", "contained_t")
ALL_FIELDS = CATEGORICAL_FIELDS + NUMERIC_FIELDS


def _close(pred: float, gt: float, rel_tol: float) -> bool:
    if gt == 0:
        return abs(pred) < 1e-9
    return abs(pred - gt) / abs(gt) <= rel_tol


def _scalar(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    return value


def _norm_categorical(field_name: str, value: Any) -> str:
    text = str(_scalar(value)).strip()
    if field_name == "commodity":
        return text.upper()
    if field_name == "category":
        return text.lower()
    return text


@dataclass
class FieldStat:
    correct: int = 0
    wrong: int = 0
    missing: int = 0
    spurious: int = 0

    @property
    def total(self) -> int:
        return self.correct + self.wrong + self.missing + self.spurious

    @property
    def accuracy(self) -> float | None:
        return (self.correct / self.total) if self.total else None

    def merge(self, other: FieldStat) -> None:
        self.correct += other.correct
        self.wrong += other.wrong
        self.missing += other.missing
        self.spurious += other.spurious

    def as_dict(self) -> dict[str, Any]:
        return {
            "correct": self.correct,
            "wrong": self.wrong,
            "missing": self.missing,
            "spurious": self.spurious,
            "total": self.total,
            "accuracy": self.accuracy,
        }


def empty_stats() -> dict[str, FieldStat]:
    return {f: FieldStat() for f in ALL_FIELDS}


def compare_rows(
    pred_rows: Iterable[Any],
    gt_rows: Iterable[dict[str, Any]],
    rel_tol: float = 0.05,
) -> dict[str, FieldStat]:
    """按 (category, commodity) 对齐后逐字段比较。

    pred_rows: ResourceRow（已归一化）
    gt_rows  : dict，键为规范字段名，**只比较 GT 中显式给出的字段**
    """
    stats = empty_stats()
    pred_list = list(pred_rows)
    used: set[int] = set()

    for gt in gt_rows:
        fields = [f for f in ALL_FIELDS if gt.get(f) is not None]
        gt_key = (
            _norm_categorical("category", gt.get("category")),
            _norm_categorical("commodity", gt.get("commodity")),
        )
        match_idx: int | None = None
        for idx, pred in enumerate(pred_list):
            if idx in used:
                continue
            if (
                _norm_categorical("category", pred.category),
                _norm_categorical("commodity", pred.commodity),
            ) == gt_key:
                match_idx = idx
                break

        if match_idx is None:
            for name in fields:
                stats[name].missing += 1
            continue

        used.add(match_idx)
        pred = pred_list[match_idx]
        for name in fields:
            pred_value = _scalar(getattr(pred, name, None))
            gt_value = gt[name]
            if pred_value is None or pred_value == "":
                stats[name].missing += 1
            elif name in NUMERIC_FIELDS:
                try:
                    ok = _close(float(pred_value), float(gt_value), rel_tol)
                except (TypeError, ValueError):
                    ok = False
                stats[name].correct += int(ok)
                stats[name].wrong += int(not ok)
            else:
                ok = _norm_categorical(name, pred_value) == _norm_categorical(name, gt_value)
                stats[name].correct += int(ok)
                stats[name].wrong += int(not ok)

    # 多抽出来的行（与 GT 无法对齐）计为 spurious，直接体现"编造数据"的代价
    for idx, pred in enumerate(pred_list):
        if idx in used:
            continue
        for name in ALL_FIELDS:
            if _scalar(getattr(pred, name, None)) is not None:
                stats[name].spurious += 1

    return stats


def overall_accuracy(stats: dict[str, FieldStat]) -> float | None:
    correct = sum(s.correct for s in stats.values())
    total = sum(s.total for s in stats.values())
    return (correct / total) if total else None


@dataclass
class CaseEval:
    report_id: str
    fields: dict[str, FieldStat]
    abstained: bool
    expect_abstain: bool
    reasons: list[str] = field(default_factory=list)
    final_score: float | None = None
    rounds_used: int = 0
    round_accuracy: dict[int, float | None] = field(default_factory=dict)
    unresolvable_specs: list[str] = field(default_factory=list)
    unresolvable_filled: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float | None:
        return overall_accuracy(self.fields)

    @property
    def error_fields(self) -> int:
        return sum(s.wrong + s.missing + s.spurious for s in self.fields.values())

    @property
    def has_error(self) -> bool:
        return self.error_fields > 0

    @property
    def hard_give(self) -> bool:
        """本应拒答却给了结论。"""
        return self.expect_abstain and not self.abstained

    @property
    def hard_give_field_count(self) -> int:
        """把错值当作结论输出的字段数（未 abstain 时才有意义）。"""
        return self.error_fields if not self.abstained else 0


@dataclass
class EvalSummary:
    cases: list[CaseEval]
    field_totals: dict[str, FieldStat]
    rel_tol: float

    @property
    def overall_accuracy(self) -> float | None:
        return overall_accuracy(self.field_totals)

    @property
    def n_cases(self) -> int:
        return len(self.cases)

    @property
    def abstained_cases(self) -> list[CaseEval]:
        return [c for c in self.cases if c.abstained]

    @property
    def expect_abstain_cases(self) -> list[CaseEval]:
        return [c for c in self.cases if c.expect_abstain]

    @property
    def abstain_recall(self) -> float | None:
        expect = self.expect_abstain_cases
        if not expect:
            return None
        hit = [c for c in expect if c.abstained]
        return len(hit) / len(expect)

    @property
    def abstain_precision(self) -> float | None:
        abstained = self.abstained_cases
        if not abstained:
            return None
        justified = [c for c in abstained if c.expect_abstain or c.has_error]
        return len(justified) / len(abstained)

    @property
    def hard_give_rate(self) -> float | None:
        expect = self.expect_abstain_cases
        if not expect:
            return None
        return sum(1 for c in expect if c.hard_give) / len(expect)

    @property
    def hard_give_field_total(self) -> int:
        return sum(c.hard_give_field_count for c in self.cases)

    @property
    def unresolvable_total(self) -> int:
        """GT 明确标注"不可确定"的字段总数。"""
        return sum(len(c.unresolvable_specs) for c in self.cases)

    @property
    def unresolvable_filled_total(self) -> int:
        """在"不可确定"字段上依然给出了具体值的次数——这才是最硬核的硬给指标。"""
        return sum(len(c.unresolvable_filled) for c in self.cases)

    @property
    def unresolvable_fill_rate(self) -> float | None:
        total = self.unresolvable_total
        if not total:
            return None
        return self.unresolvable_filled_total / total


def filled_unresolvable_fields(rows: Iterable[Any], specs: Iterable[str]) -> list[str]:
    """检查 GT 标注为不可确定的字段，系统是否仍输出了具体值。

    spec 形如 "inferred.grade_value"。
    """
    filled: list[str] = []
    row_list = list(rows)
    for spec in specs:
        if "." not in spec:
            continue
        category, field_name = spec.split(".", 1)
        for row in row_list:
            if _norm_categorical("category", row.category) != _norm_categorical("category", category):
                continue
            value = _scalar(getattr(row, field_name, None))
            if value is not None and value != "":
                filled.append(spec)
    return filled
