"""归一化层单元测试（无外部依赖，python -m unittest 直接跑）。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ni43101.normalize import (  # noqa: E402
    UnitError,
    canonical_grade,
    canonicalize_raw_row,
    consistency_error,
    contained_tonnes,
    normalize_category,
    plausibility_warnings,
    to_contained_t,
    to_mt,
    tonnes_to_oz,
)


class TestTonnage(unittest.TestCase):
    def test_mt_kt_t_bt(self):
        self.assertAlmostEqual(to_mt(214.0, "Mt"), 214.0)
        self.assertAlmostEqual(to_mt(214000, "kt"), 214.0)
        self.assertAlmostEqual(to_mt(214e6, "t"), 214.0)
        self.assertAlmostEqual(to_mt(0.214, "bt"), 214.0)

    def test_unknown_unit_raises(self):
        with self.assertRaises(UnitError):
            to_mt(1.0, "buckets")


class TestGrade(unittest.TestCase):
    def test_percent_to_gpt(self):
        self.assertAlmostEqual(canonical_grade(1.17, "%")[0], 1.17)
        self.assertAlmostEqual(contained_tonnes(214.0, 1.17, "%"), 2_503_800.0)

    def test_ppm_folds_into_gpt(self):
        value, unit = canonical_grade(1500, "ppm")
        self.assertEqual(unit, "g/t")
        self.assertAlmostEqual(value, 1500.0)


class TestContainedMetal(unittest.TestCase):
    def test_tonnes_identity(self):
        # 回归用例：曾把金属量"吨"走了吨位换算，560000 t 被当成 0.56 t，偏差 10^6 倍
        self.assertAlmostEqual(to_contained_t(560_000, "t"), 560_000)
        self.assertAlmostEqual(to_contained_t(2.5, "Mt"), 2_500_000)
        self.assertAlmostEqual(to_contained_t(2_503.8, "kt"), 2_503_800)

    def test_gold_tonnes_to_ounces(self):
        # 100 t Au = 3.215 Moz
        self.assertAlmostEqual(tonnes_to_oz(100.0) / 1e6, 3.215, places=3)

    def test_moz_multiplier(self):
        # 4.45 Moz Au = 138.41 t（若把 Moz 当 oz，会差 10^6 倍）
        self.assertAlmostEqual(to_contained_t(4.45, "Moz"), 138.4105, places=3)

    def test_consistency(self):
        self.assertIsNone(consistency_error(214.0, None, "%", 2_503_800.0))
        self.assertLess(consistency_error(120.5, 1.15, "g/t", 138.41), 0.05)
        self.assertGreater(consistency_error(214.0, 1.17, "%", 25_000.0), 0.5)


class TestCategory(unittest.TestCase):
    def test_noisy_labels(self):
        self.assertEqual(normalize_category("Indicated Resources"), "indicated")
        self.assertEqual(normalize_category("Inferred (underground)"), "inferred")
        self.assertEqual(normalize_category("measured"), "measured")

    def test_total_rows_rejected(self):
        with self.assertRaises(ValueError):
            normalize_category("Total")


class TestPlausibility(unittest.TestCase):
    def test_li2o_grade_out_of_range_flagged(self):
        row = canonicalize_raw_row(
            {
                "category": "indicated",
                "commodity": "Li2O",
                "tonnage_value": 100.0,
                "tonnage_unit": "Mt",
                "grade_value": 10.0,
                "grade_unit": "%",
            }
        )
        self.assertTrue(any("合理区间" in w for w in plausibility_warnings(row)))

    def test_unit_mismatch_flagged(self):
        row = canonicalize_raw_row(
            {
                "category": "indicated",
                "commodity": "Au",
                "tonnage_value": 120.5,
                "tonnage_unit": "Mt",
                "grade_value": 1.15,
                "grade_unit": "%",
            }
        )
        self.assertTrue(any("通常为 g/t" in w for w in row.warnings))


class TestRawRowCanonicalization(unittest.TestCase):
    def test_full_row(self):
        row = canonicalize_raw_row(
            {
                "category": "Indicated",
                "commodity": "Au",
                "tonnage_value": 120.5,
                "tonnage_unit": "Mt",
                "grade_value": 1.15,
                "grade_unit": "g/t",
                "contained_value": 4.45,
                "contained_unit": "Moz",
                "page": 88,
                "quote": "Indicated 120.5 1.15 4.45",
            }
        )
        self.assertAlmostEqual(row.tonnage_mt, 120.5)
        self.assertAlmostEqual(row.grade_value, 1.15)
        self.assertAlmostEqual(row.contained_t, 138.4105, places=3)
        self.assertEqual(row.key, ("indicated", "AU"))


if __name__ == "__main__":
    unittest.main()
