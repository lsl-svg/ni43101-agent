"""CriticMaster Agent：异构模型评审 + 确定性规则兜底。

两层评审的理由：
- 纯 LLM 评审会"漏机械性错误"：单位错位、金属量与吨位x品位不一致这类问题，
  模型经常判 ok（它更关注语义合理性，而不是算术）。
- 纯规则评审会漏"语义/溯源"错误：编造的引用、跨类别串行、表格串列。
所以：规则层负责**硬约束与评分上限**，LLM 层负责**语义与溯源的细致审查**，两者取严。
"""

from __future__ import annotations

import re
from pathlib import Path

from ..config import RunCfg
from ..llm import LLMClient
from ..normalize import consistency_error, plausibility_warnings, unit_str
from ..schemas import CritiqueResult, ExtractionResult, FieldCritique

_WS = re.compile(r"\s+")


def _norm_text(text: str) -> str:
    return _WS.sub(" ", text or "").strip().lower()


class RuleCritic:
    """确定性检查，不花 token，可单元测试。"""

    name = "rule-critic"

    def check(self, extraction: ExtractionResult, source_text: str, cfg: RunCfg) -> CritiqueResult:
        critiques: list[FieldCritique] = []
        blocking: list[str] = []
        fixes: list[str] = []
        score = 10.0
        source = _norm_text(source_text)

        if not extraction.rows:
            return CritiqueResult(
                score=0.0,
                blocking_issues=["未抽取出任何资源量行"],
                suggested_fixes=["检查表格页是否被 select_relevant_pages 命中"],
                critic_model=self.name,
            )

        for idx, row in enumerate(extraction.rows):
            prefix = f"rows[{idx}]"

            # 1) 关键字段缺失
            for field_name, value in (("tonnage_mt", row.tonnage_mt), ("grade_value", row.grade_value)):
                if value is None:
                    critiques.append(
                        FieldCritique(
                            field=f"{prefix}.{field_name}",
                            verdict="missing",
                            confidence=0.9,
                            comment="原文未给出或抽取遗漏；不允许猜测补值",
                        )
                    )
                    score -= 2.0
                    fixes.append(f"{prefix}.{field_name} 回原文确认，确认不到就保持 null 并标注 unresolved")

            # 2) 幻觉溯源：quote 必须在原文中出现
            if row.quote:
                head = _norm_text(row.quote)[:40]
                if head and head not in source:
                    critiques.append(
                        FieldCritique(
                            field=f"{prefix}.quote",
                            verdict="wrong",
                            confidence=0.95,
                            comment="引用片段在原文中不存在，疑似幻觉",
                        )
                    )
                    blocking.append(f"{prefix}.quote 疑似幻觉：{row.quote[:60]!r} 未在原文出现")
                    score -= 4.0

            # 3) 单位/量级告警（重算 + 归一化阶段留下的告警一起看）
            for warn in plausibility_warnings(row) + list(row.warnings):
                if "无法识别" in warn:
                    critiques.append(
                        FieldCritique(field=f"{prefix}.unit", verdict="wrong", confidence=0.9, comment=warn)
                    )
                    blocking.append(f"{prefix} 单位无法识别：{warn}")
                    score -= 4.0
                elif "超出合理区间" in warn or "疑似" in warn or "通常为" in warn:
                    critiques.append(
                        FieldCritique(
                            field=f"{prefix}.value", verdict="suspect", confidence=0.7, comment=warn
                        )
                    )
                    blocking.append(f"{prefix} 量级可疑：{warn}")
                    score -= 2.0

            # 4) 内部一致性：contained ~= tonnage x grade
            err = consistency_error(
                row.tonnage_mt,
                row.grade_value,
                unit_str(row.grade_unit),
                row.contained_t,
            )
            if err is not None and err > cfg.consistency_tol:
                verdict = "wrong" if err > 0.5 else "suspect"
                critiques.append(
                    FieldCritique(
                        field=f"{prefix}.contained_t",
                        verdict=verdict,
                        confidence=0.85,
                        comment=f"与 tonnage x grade 偏差 {err:.1%}，超过容差 {cfg.consistency_tol:.0%}",
                    )
                )
                blocking.append(f"{prefix} 金属量与吨位x品位不自洽（偏差 {err:.1%}）")
                score -= 4.0 if verdict == "wrong" else 2.0
                fixes.append(f"{prefix}: 用 tonnage x grade 重算 contained，或核对是否单位标错")

        score = max(0.0, min(10.0, score))
        return CritiqueResult(
            score=score,
            field_critiques=critiques,
            blocking_issues=blocking,
            suggested_fixes=fixes,
            critic_model=self.name,
        )


class CriticMasterAgent:
    def __init__(
        self,
        llm: LLMClient | None,
        prompt_path: str | Path | None,
        cfg: RunCfg,
        rules: RuleCritic | None = None,
    ):
        self.llm = llm
        self.cfg = cfg
        self.rules = rules or RuleCritic()
        self.system_prompt = Path(prompt_path).read_text(encoding="utf-8") if prompt_path else ""

    def critique(self, extraction: ExtractionResult, source_text: str) -> CritiqueResult:
        rule_result = self.rules.check(extraction, source_text, self.cfg)

        llm_result: CritiqueResult | None = None
        if self.llm is not None and self.system_prompt:
            user = (
                "待审抽取结果（JSON）：\n"
                f"{extraction.model_dump_json(indent=2)}\n\n"
                "对应原文节选：\n"
                f"{source_text[:40_000]}"
            )
            raw = self.llm.json(self.system_prompt, user)
            llm_result = CritiqueResult(
                score=float(raw.get("score", 0.0)),
                field_critiques=[
                    FieldCritique(**fc) for fc in (raw.get("field_critiques") or []) if isinstance(fc, dict)
                ],
                blocking_issues=list(raw.get("blocking_issues") or []),
                suggested_fixes=list(raw.get("suggested_fixes") or []),
                critic_model=self.llm.cfg.model,
            )

        if llm_result is None:
            return rule_result

        # 取严：规则层给评分上限，阻断项取并集
        return CritiqueResult(
            score=min(llm_result.score, rule_result.score),
            field_critiques=llm_result.field_critiques + rule_result.field_critiques,
            blocking_issues=list(dict.fromkeys(llm_result.blocking_issues + rule_result.blocking_issues)),
            suggested_fixes=list(dict.fromkeys(llm_result.suggested_fixes + rule_result.suggested_fixes)),
            critic_model=f"{llm_result.critic_model}+{rule_result.critic_model}",
        )
