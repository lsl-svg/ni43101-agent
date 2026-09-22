"""评测口径单元测试：容差边界、漏抽/多抽、不可确定字段的硬给检测。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ni43101.evaluation.metrics import (  # noqa: E402
    compare_rows,
    filled_unresolvable_fields,
    overall_accuracy,
)
from ni43101.schemas import ResourceRow  # noqa: E402

GT = [
    {
        "category": "indicated",
        "commodity": "Li2O",
        "tonnage_mt": 214.0,
        "grade_value": 1.17,
        "grade_unit": "%",
        "contained_t": 2_503_800.0,
    }
]


def pred(tonnage=214.0, grade=1.17, contained=2_503_800.0) -> ResourceRow:
    return ResourceRow(
        category="indicated",
        commodity="Li2O",
        tonnage_mt=tonnage,
        grade_value=grade,
        grade_unit="%",
        contained_t=contained,
    )


class TestCompare(unittest.TestCase):
    def test_perfect(self):
        stats = compare_rows([pred()], GT, rel_tol=0.05)
        self.assertEqual(overall_accuracy(stats), 1.0)

    def test_tolerance_boundary(self):
        # 恰好 +5% 判对，+6% 判错
        self.assertEqual(compare_rows([pred(tonnage=224.7)], GT, 0.05)["tonnage_mt"].correct, 1)
        self.assertEqual(compare_rows([pred(tonnage=226.84)], GT, 0.05)["tonnage_mt"].wrong, 1)

    def test_missing_row_counts_as_missing(self):
        stats = compare_rows([], GT, 0.05)
        self.assertEqual(stats["tonnage_mt"].missing, 1)
        self.assertEqual(overall_accuracy(stats), 0.0)

    def test_spurious_row_counts_as_fabrication(self):
        extra = ResourceRow(
            category="inferred", commodity="Li2O", tonnage_mt=1.0, grade_value=1.0, grade_unit="%"
        )
        stats = compare_rows([pred(), extra], GT, 0.05)
        self.assertEqual(stats["tonnage_mt"].spurious, 1)
        self.assertLess(overall_accuracy(stats), 1.0)

    def test_null_gt_fields_excluded(self):
        gt = [{"category": "inferred", "commodity": "Au", "tonnage_mt": 35.2, "grade_value": None}]
        rows = [ResourceRow(category="inferred", commodity="Au", tonnage_mt=35.2)]
        stats = compare_rows(rows, gt, 0.05)
        self.assertEqual(stats["tonnage_mt"].correct, 1)
        self.assertEqual(stats["grade_value"].total, 0)  # 不可确定字段不进入分母


class TestUnresolvable(unittest.TestCase):
    def test_filled_detected(self):
        rows = [
            ResourceRow(
                category="inferred", commodity="Au", tonnage_mt=35.2, grade_value=1.05, grade_unit="g/t"
            )
        ]
        filled = filled_unresolvable_fields(rows, ["inferred.grade_value", "inferred.contained_t"])
        self.assertEqual(filled, ["inferred.grade_value"])

    def test_abstained_field_not_counted(self):
        rows = [ResourceRow(category="inferred", commodity="Au", tonnage_mt=35.2, grade_value=None)]
        self.assertEqual(filled_unresolvable_fields(rows, ["inferred.grade_value"]), [])


if __name__ == "__main__":
    unittest.main()
