"""评测入口：读 out/ 下的交付结果 + data/ground_truth/ 下的 GT，产出指标与报告。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..evolution import EvolutionLog
from ..schemas import ExtractionResult, ResourceRow
from .gt_adapter import load_gt
from .metrics import (
    CaseEval,
    EvalSummary,
    FieldStat,
    compare_rows,
    empty_stats,
    filled_unresolvable_fields,
    overall_accuracy,
)


@dataclass
class EvalResult:
    summary: EvalSummary
    evolution_stats: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _rows_from_payload(payload: dict[str, Any]) -> list[ResourceRow]:
    return [ResourceRow(**row) for row in payload.get("rows") or []]


def _round_accuracies(
    case_dir: Path, gt_rows: list[dict[str, Any]], rel_tol: float
) -> dict[int, float | None]:
    out: dict[int, float | None] = {}
    for path in sorted(case_dir.glob("round*.json")):
        try:
            round_no = int(path.stem.replace("round", ""))
        except ValueError:
            continue
        try:
            extraction = ExtractionResult(**_read_json(path))
        except Exception:  # noqa: BLE001
            continue
        out[round_no] = overall_accuracy(compare_rows(extraction.rows, gt_rows, rel_tol))
    return out


def evaluate_run(
    settings: Settings,
    out_dir: str | Path | None = None,
    gt_dir: str | Path | None = None,
    rel_tol: float | None = None,
) -> EvalResult:
    out_path = Path(out_dir) if out_dir else settings.path(settings.run.out_dir)
    gt_path = Path(gt_dir) if gt_dir else settings.path(settings.paths.gt_dir)
    tol = rel_tol if rel_tol is not None else settings.run.consistency_tol

    warnings: list[str] = []
    cases: list[CaseEval] = []
    totals: dict[str, FieldStat] = empty_stats()

    for case_dir in sorted(p for p in out_path.iterdir() if p.is_dir()):
        final_path = case_dir / "final.json"
        if not final_path.is_file():
            continue
        report_id = case_dir.name
        case_data = _read_json(final_path)

        gt_file = gt_path / f"{report_id}.json"
        if not gt_file.is_file():
            warnings.append(f"{report_id}: 缺少 GT（{gt_file.name}），该案例不计入准确率，仅计入拒答统计")
            gt = {"rows": [], "expect_abstain": False, "unresolvable_fields": []}
        else:
            gt = load_gt(gt_file)

        extraction = ExtractionResult(**(case_data.get("extraction") or {"report_id": report_id}))
        fields = compare_rows(extraction.rows, gt["rows"], tol)
        for name, stat in fields.items():
            totals[name].merge(stat)

        decision = case_data.get("decision") or {}
        critiques = case_data.get("critiques") or []
        unresolvable_specs = list(gt.get("unresolvable_fields") or [])
        cases.append(
            CaseEval(
                report_id=report_id,
                fields=fields,
                abstained=bool(decision.get("abstain")),
                expect_abstain=bool(gt.get("expect_abstain")),
                reasons=list(decision.get("reasons") or []),
                final_score=float(critiques[-1]["score"]) if critiques else None,
                rounds_used=int(case_data.get("rounds_used") or 0),
                round_accuracy=_round_accuracies(case_dir, gt["rows"], tol),
                unresolvable_specs=unresolvable_specs,
                unresolvable_filled=filled_unresolvable_fields(extraction.rows, unresolvable_specs),
            )
        )

    if not cases:
        warnings.append(f"{out_path} 下没有任何 final.json，请先运行 `python -m ni43101.cli run`")

    summary = EvalSummary(cases=cases, field_totals=totals, rel_tol=tol)
    evolution_stats = EvolutionLog(settings.path(settings.run.evolution_log)).stats()
    return EvalResult(summary=summary, evolution_stats=evolution_stats, warnings=warnings)
