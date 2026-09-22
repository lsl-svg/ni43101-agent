"""拒答（abstain）策略。

需求里明确提到："我们最看的是：当抽取明显错误时，系统是否 abstain 而不是硬给。"
所以这里把"拒答判定"写成**可枚举、可测试的规则**，而不是让模型自由裁量：

R1 评分门槛   : Revise Loop 用尽 max_rounds 后 CriticMaster 评分仍 < pass_score
R2 关键字段缺失: 行内 tonnage / grade 缺失比例 > max_missing_ratio
R3 内部不一致  : |contained - tonnage x grade| / expected > consistency_tol
R4 未解阻断项  : CriticMaster blocking_issues 非空，或 Reviser 返回 unresolved_blockers
R5 无有效抽取  : 一行都没抽出来
R6 量级/单位可疑: 归一化层给出量级或"矿种-单位"不匹配告警且未被修复

命中任意一条 -> abstain=True, needs_human_review=True。
"""

from __future__ import annotations

from .config import RunCfg
from .normalize import consistency_error, plausibility_warnings, unit_str
from .schemas import AbstainDecision, CritiqueResult, ExtractionResult


def _missing_ratio(extraction: ExtractionResult) -> float:
    if not extraction.rows:
        return 0.0
    missing = sum(1 for r in extraction.rows if r.tonnage_mt is None or r.grade_value is None)
    return missing / len(extraction.rows)


def _inconsistent_rows(extraction: ExtractionResult, tol: float) -> list[str]:
    bad: list[str] = []
    for row in extraction.rows:
        err = consistency_error(row.tonnage_mt, row.grade_value, unit_str(row.grade_unit), row.contained_t)
        if err is not None and err > tol:
            bad.append(f"{row.label}: contained 与 tonnage x grade 偏差 {err:.1%}")
    return bad


def _suspicious_rows(extraction: ExtractionResult) -> list[str]:
    """量级/单位可疑：直接重算合理性，不依赖上游是否写过 warnings。"""
    bad: list[str] = []
    for row in extraction.rows:
        hard = [w for w in plausibility_warnings(row) if "超出合理区间" in w or "通常为" in w]
        hard += [w for w in row.warnings if "无法识别" in w or "疑似" in w]
        if hard:
            bad.append(f"{row.label}: {hard[0]}")
    return bad


def evaluate(
    extraction: ExtractionResult,
    critique: CritiqueResult | None,
    cfg: RunCfg,
    rounds_used: int,
) -> AbstainDecision:
    reasons: list[str] = []

    if not extraction.rows:
        reasons.append("R5: 未抽取出任何资源量数据行")

    ratio = _missing_ratio(extraction)
    if extraction.rows and ratio > cfg.max_missing_ratio:
        reasons.append(f"R2: 关键字段缺失比例 {ratio:.0%} > 阈值 {cfg.max_missing_ratio:.0%}")

    inconsistent = _inconsistent_rows(extraction, cfg.consistency_tol)
    if inconsistent:
        reasons.append("R3: " + "; ".join(inconsistent))

    suspicious = _suspicious_rows(extraction)
    if suspicious:
        reasons.append("R6: " + "; ".join(suspicious))

    if critique is not None:
        if critique.score < cfg.pass_score:
            reasons.append(f"R1: {rounds_used} 轮后评分 {critique.score:.1f} < 门槛 {cfg.pass_score:.1f}")
        if critique.blocking_issues:
            reasons.append("R4: " + "; ".join(critique.blocking_issues))

    if extraction.unresolved_blockers:
        reasons.append("R4: " + "; ".join(extraction.unresolved_blockers))

    abstain = bool(reasons)
    severity = "none"
    if abstain:
        severity = "high" if any(r.startswith(("R3", "R4", "R5")) for r in reasons) else "medium"

    return AbstainDecision(
        abstain=abstain,
        needs_human_review=abstain,
        reasons=reasons,
        severity=severity,  # type: ignore[arg-type]
    )
