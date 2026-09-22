"""拒答策略单元测试：每条规则都要有正反用例。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ni43101.abstain import evaluate  # noqa: E402
from ni43101.config import RunCfg  # noqa: E402
from ni43101.schemas import CritiqueResult, ExtractionResult, ResourceRow  # noqa: E402

CFG = RunCfg(max_rounds=3, pass_score=8.0, consistency_tol=0.05, max_missing_ratio=0.25)


def row(**kwargs) -> ResourceRow:
    base = dict(
        category="indicated",
        commodity="Au",
        tonnage_mt=120.5,
        grade_value=1.15,
        grade_unit="g/t",
        contained_t=138.575,
    )
    base.update(kwargs)
    return ResourceRow(**base)


def extraction(*rows: ResourceRow, blockers: list[str] | None = None) -> ExtractionResult:
    return ExtractionResult(report_id="t", rows=list(rows), unresolved_blockers=blockers or [])


class TestAbstainRules(unittest.TestCase):
    def test_clean_case_passes(self):
        decision = evaluate(extraction(row()), CritiqueResult(score=9.0), CFG, rounds_used=1)
        self.assertFalse(decision.abstain)

    def test_r1_low_score_after_max_rounds(self):
        decision = evaluate(extraction(row()), CritiqueResult(score=6.0), CFG, rounds_used=3)
        self.assertTrue(decision.abstain)
        self.assertTrue(any(r.startswith("R1") for r in decision.reasons))

    def test_r2_missing_ratio(self):
        decision = evaluate(
            extraction(row(), row(category="inferred", grade_value=None, contained_t=None)),
            CritiqueResult(score=9.0),
            CFG,
            rounds_used=2,
        )
        self.assertTrue(decision.abstain)
        self.assertTrue(any(r.startswith("R2") for r in decision.reasons))

    def test_r3_inconsistency(self):
        decision = evaluate(
            extraction(row(contained_t=25_000.0)), CritiqueResult(score=9.0), CFG, rounds_used=1
        )
        self.assertTrue(decision.abstain)
        self.assertTrue(any(r.startswith("R3") for r in decision.reasons))
        self.assertEqual(decision.severity, "high")

    def test_r4_blocking_issues(self):
        decision = evaluate(
            extraction(row()),
            CritiqueResult(score=9.0, blocking_issues=["rows[0].quote 疑似幻觉"]),
            CFG,
            rounds_used=1,
        )
        self.assertTrue(decision.abstain)

    def test_r4_unresolved_blockers_from_reviser(self):
        decision = evaluate(
            extraction(row(), blockers=["rows[1].grade_value 无法溯源"]),
            CritiqueResult(score=9.0),
            CFG,
            rounds_used=2,
        )
        self.assertTrue(decision.abstain)
        self.assertTrue(decision.needs_human_review)

    def test_r5_empty(self):
        decision = evaluate(extraction(), CritiqueResult(score=0.0), CFG, rounds_used=3)
        self.assertTrue(decision.abstain)
        self.assertTrue(any(r.startswith("R5") for r in decision.reasons))

    def test_r6_magnitude(self):
        bad = row(
            tonnage_mt=214000.0,
            grade_value=1.17,
            grade_unit="%",
            commodity="Li2O",
            contained_t=214000.0 * 1.17 * 1e4,
        )
        decision = evaluate(extraction(bad), CritiqueResult(score=9.0), CFG, rounds_used=1)
        self.assertTrue(decision.abstain)
        self.assertTrue(any(r.startswith("R6") for r in decision.reasons))


if __name__ == "__main__":
    unittest.main()
