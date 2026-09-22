"""单位与量纲归一化层。

这是本系统里最容易出错、也最值得单独成层的地方：
NI 43-101 报告里同一份文档会同时出现 Mt / kt / t、g/t / ppm / %、oz / t / kt，
而"金属量 = 吨位 x 品位"的换算在不同单位组合下差 10^4 ~ 10^6 倍。

核心恒等式（记住这两条就够）：
    Au/Ag:  contained(t) = tonnage(Mt) x grade(g/t)          # 百万吨 x 克/吨 = 吨
    Cu 等:  contained(t) = tonnage(Mt) x grade(%) x 1e4
    ppm == g/t （数值等价）
"""

from __future__ import annotations

import re
from typing import Any

from .schemas import ResourceRow

TROY_OZ_PER_GRAM = 31.1034768


class UnitError(ValueError):
    """无法识别的单位。"""


def norm_unit(unit: Any) -> str:
    if unit is None:
        return ""
    return " ".join(str(unit).strip().lower().replace("_", " ").split())


def unit_str(unit: Any) -> str | None:
    """把 enum / str / None 统一成单位字符串（兼容未经过 pydantic 校验的对象）。"""
    if unit is None:
        return None
    return str(getattr(unit, "value", unit))


# --- 吨位 ---------------------------------------------------------------

_TONNAGE_RULES: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"\b(bt|billion)\b|十亿吨"), 1e3),
    (re.compile(r"\b(mt|m tonnes?|million tonnes?|mtpa)\b|百万吨"), 1.0),
    (re.compile(r"\b(kt|thousand tonnes?|000 t|kilotonnes?)\b|千吨"), 1e-3),
    (re.compile(r"\b(t|tonnes?|metric tons?)\b|吨"), 1e-6),
]


def to_mt(value: float, unit: str | None) -> float:
    """把任意吨位单位换算成 Mt（百万吨）。"""
    u = norm_unit(unit)
    for pattern, factor in _TONNAGE_RULES:
        if pattern.search(u):
            return float(value) * factor
    raise UnitError(f"无法识别的吨位单位: {unit!r}")


def mt_to(value_mt: float, unit: str) -> float:
    return float(value_mt) / to_mt(1.0, unit)


# --- 品位 ---------------------------------------------------------------


def grade_to_gpt(value: float, unit: str | None) -> float:
    """把任意品位单位换算成 g/t 数值。"""
    u = norm_unit(unit)
    if not u:
        raise UnitError("品位单位缺失")
    if "%" in u:
        return float(value) * 1e4
    if "ppm" in u or "g/t" in u or "gpt" in u or "gram" in u:
        return float(value)
    raise UnitError(f"无法识别的品位单位: {unit!r}")


def canonical_grade(value: float | None, unit: str | None) -> tuple[float | None, str | None]:
    """返回规范化的 (value, unit)：ppm 一律折叠为 g/t（数值相同），% 保留。"""
    if value is None:
        return None, None
    u = norm_unit(unit)
    if "%" in u:
        return float(value), "%"
    if "ppm" in u or "g/t" in u or "gpt" in u or "gram" in u:
        return float(value), "g/t"
    raise UnitError(f"无法识别的品位单位: {unit!r}")


# --- 金属量 -------------------------------------------------------------


def contained_tonnes(tonnage_mt: float, grade_value: float, grade_unit: str | None) -> float:
    """由吨位与品位计算金属量（吨）。"""
    return float(tonnage_mt) * grade_to_gpt(grade_value, grade_unit)


# 金属量 -> 吨。注意：这里**不能**复用吨位换算（吨位是往 Mt 收敛，金属量是往 t 收敛，
# 用错方向会出现 10^6 倍偏差：560000 t 被当成 0.56 t）。
_MASS_TO_TONNES: list[tuple[re.Pattern[str], float]] = [
    (re.compile(r"\b(bt|billion)\b"), 1e9),
    (re.compile(r"\b(mt|m tonnes?|million tonnes?)\b"), 1e6),
    (re.compile(r"\b(kt|kilotonnes?|thousand tonnes?|000 t)\b"), 1e3),
    (re.compile(r"\b(t|tonnes?|metric tons?)\b"), 1.0),
]


def to_contained_t(value: float, unit: str | None) -> float:
    u = norm_unit(unit)
    if not u:
        return float(value)
    if "oz" in u:
        # NI 43-101 里金/银的金属量常写成 Moz / koz，必须显式处理倍数，
        # 否则 4.45 Moz 会被当成 4.45 oz，误差 10^6 倍。
        if "moz" in u or "million" in u:
            multiplier = 1e6
        elif "koz" in u or "thousand" in u:
            multiplier = 1e3
        else:
            multiplier = 1.0
        return float(value) * multiplier * TROY_OZ_PER_GRAM / 1e6
    if "%" in u or "grade" in u:
        raise UnitError(f"金属量单位疑似品位: {unit!r}")
    for pattern, factor in _MASS_TO_TONNES:
        if pattern.search(u):
            return float(value) * factor
    raise UnitError(f"无法识别的金属量单位: {unit!r}")


def tonnes_to_oz(tonnes: float) -> float:
    return float(tonnes) * 1e6 / TROY_OZ_PER_GRAM


# --- 一致性与合理性 ------------------------------------------------------


def consistency_error(
    tonnage_mt: float | None,
    grade_value: float | None,
    grade_unit: str | None,
    contained_t: float | None,
) -> float | None:
    """金属量与"吨位 x 品位"的相对偏差；任一缺失则返回 None。"""
    if tonnage_mt is None or grade_value is None or contained_t is None:
        return None
    if contained_t == 0:
        return None
    expected = contained_tonnes(tonnage_mt, grade_value, grade_unit)
    if expected == 0:
        return None
    return abs(contained_t - expected) / expected


# 领域合理性区间（可按需扩展）。commodity -> (min, max, unit)
PLAUSIBLE_GRADES: dict[str, tuple[float, float, str]] = {
    "AU": (0.1, 20.0, "g/t"),
    "AG": (1.0, 1000.0, "g/t"),
    "CU": (0.05, 10.0, "%"),
    "ZN": (0.1, 20.0, "%"),
    "PB": (0.1, 20.0, "%"),
    "NI": (0.05, 5.0, "%"),
    "LI2O": (0.1, 3.0, "%"),
    "FE": (5.0, 70.0, "%"),
}
PLAUSIBLE_TONNAGE_MT = (0.05, 5000.0)


def plausibility_warnings(row: ResourceRow) -> list[str]:
    """量级与"矿种-单位"匹配性检查：不依赖任何 LLM，纯确定性规则。"""
    warns: list[str] = []
    if row.tonnage_mt is not None and not (
        PLAUSIBLE_TONNAGE_MT[0] <= row.tonnage_mt <= PLAUSIBLE_TONNAGE_MT[1]
    ):
        warns.append(f"吨位 {row.tonnage_mt} Mt 超出合理区间 {PLAUSIBLE_TONNAGE_MT}，疑似单位或小数点错位")
    if row.grade_value is not None and row.grade_unit is not None:
        spec = PLAUSIBLE_GRADES.get(row.commodity.strip().upper())
        if spec:
            lo, hi, expected_unit = spec
            actual_unit = unit_str(row.grade_unit)
            if actual_unit != expected_unit:
                warns.append(f"{row.commodity} 品位单位通常为 {expected_unit}，实际为 {actual_unit}")
            if not (lo <= row.grade_value <= hi):
                warns.append(
                    f"{row.commodity} 品位 {row.grade_value} 超出合理区间 ({lo}, {hi}) {expected_unit}"
                )
    return warns


# --- 资源类别 -----------------------------------------------------------

_CATEGORY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("measured", re.compile(r"measur", re.IGNORECASE)),
    ("indicated", re.compile(r"indicat", re.IGNORECASE)),
    ("inferred", re.compile(r"inferr", re.IGNORECASE)),
]


def normalize_category(value: Any) -> str:
    """把 'Indicated Resources' / 'Inferred (underground)' 之类的写法收敛到枚举值。

    注意：Total / Subtotal / 合计 这类汇总行必须被拒绝——把合计行当成一个类别抽出来，
    是 NI 43-101 抽取里很常见的"看起来对、实际重复计数"错误。
    """
    text = str(value or "")
    for canonical, pattern in _CATEGORY_PATTERNS:
        if pattern.search(text):
            return canonical
    raise ValueError(f"无法识别的资源类别: {value!r}（Total/合计等汇总行应被排除）")


# --- 原始输出 -> 规范行 ---------------------------------------------------


def _first(raw: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in raw and raw[k] not in (None, ""):
            return raw[k]
    return None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).replace(",", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(m.group()) if m else None


def canonicalize_raw_row(raw: dict[str, Any]) -> ResourceRow:
    """把 LLM/夹具的原始行转成规范行：单位归一 + 补算金属量 + 生成告警。"""
    tonnage_raw = _first(raw, "tonnage_value", "tonnage", "tonnage_mt")
    tonnage_unit = _first(raw, "tonnage_unit", "tonnage_units", "unit_tonnage")
    grade_raw = _first(raw, "grade_value", "grade")
    grade_unit = _first(raw, "grade_unit", "grade_units", "unit_grade")
    contained_raw = _first(raw, "contained_value", "contained", "contained_metal")
    contained_unit = _first(raw, "contained_unit", "unit_contained")

    row = ResourceRow(
        category=normalize_category(_first(raw, "category", "class", "resource_category")),
        commodity=str(_first(raw, "commodity", "metal", "element") or "unknown"),
        page=int(_as_float(_first(raw, "page", "page_number")) or 0) or None,
        quote=_first(raw, "quote", "evidence", "source_quote"),
        confidence=_as_float(_first(raw, "confidence")),
    )

    # 吨位：给了 >1 的数值却标 t（例如 214000 t）这类情况，仍按标注单位换算，交给告警层暴露
    try:
        if _as_float(tonnage_raw) is not None and tonnage_unit:
            row.tonnage_mt = to_mt(_as_float(tonnage_raw), tonnage_unit)
        elif _as_float(tonnage_raw) is not None:
            row.tonnage_mt = _as_float(tonnage_raw)  # 已是规范单位（GT 路径）
            row.warnings.append("吨位单位缺失，按 Mt 处理")
    except UnitError as exc:
        row.warnings.append(str(exc))

    try:
        value, unit = canonical_grade(_as_float(grade_raw), grade_unit)
        row.grade_value, row.grade_unit = value, unit  # type: ignore[assignment]
    except UnitError as exc:
        row.warnings.append(str(exc))

    try:
        if _as_float(contained_raw) is not None:
            row.contained_t = to_contained_t(
                _as_float(contained_raw), contained_unit or ("oz" if "oz" in str(contained_unit) else "t")
            )
    except UnitError as exc:
        row.warnings.append(str(exc))

    # 金属量缺失则用 吨位 x 品位 补算（并标注来源，便于审计）
    if row.contained_t is None and row.tonnage_mt is not None and row.grade_value is not None:
        try:
            row.contained_t = contained_tonnes(row.tonnage_mt, row.grade_value, unit_str(row.grade_unit))
            row.warnings.append("contained 由 tonnage x grade 补算")
        except UnitError as exc:
            row.warnings.append(str(exc))

    row.warnings.extend(plausibility_warnings(row))
    return row
