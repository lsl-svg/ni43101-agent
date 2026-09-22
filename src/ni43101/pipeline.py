"""主流程编排：Extract -> Critique -> Revise Loop -> Abstain -> 落盘。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol

from . import abstain as abstain_mod
from .config import Settings
from .evolution import EvolutionLog
from .pdfio import PageText, render_pages
from .schemas import CaseResult, CritiqueResult, EvolutionEntry, ExtractionResult


class ExtractorLike(Protocol):
    def extract(
        self, report_id: str, pages: list[PageText], company: str | None = ..., project: str | None = ...
    ) -> ExtractionResult: ...


class CriticLike(Protocol):
    def critique(self, extraction: ExtractionResult, source_text: str) -> CritiqueResult: ...


class ReviserLike(Protocol):
    def revise(
        self,
        report_id: str,
        extraction: ExtractionResult,
        critique: CritiqueResult,
        pages: list[PageText],
        case_id: str | None = ...,
    ) -> ExtractionResult: ...


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        extractor: ExtractorLike,
        critic: CriticLike,
        reviser: ReviserLike,
        log: EvolutionLog,
    ):
        self.settings = settings
        self.extractor = extractor
        self.critic = critic
        self.reviser = reviser
        self.log = log
        self.cfg = settings.run
        self.out_dir = settings.path(self.cfg.out_dir)

    # --- 落盘 ---
    def _case_dir(self, report_id: str) -> Path:
        path = self.out_dir / report_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _write_json(path: Path, payload: Any) -> None:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _snapshot(self, report_id: str, round_no: int, extraction: ExtractionResult) -> None:
        self._write_json(
            self._case_dir(report_id) / f"round{round_no}.json",
            extraction.model_dump(mode="json"),
        )

    def _write_review_queue(
        self, report_id: str, extraction: ExtractionResult, critique: CritiqueResult | None, decision
    ) -> None:
        """拒答案例写入待人工审核队列（设计要求：abstain + 标注待人工审核）。"""
        flagged = (
            [
                f"{c.field}: {c.verdict} — {c.comment}"
                for c in critique.field_critiques
                if c.verdict in ("wrong", "missing")
            ]
            if critique
            else []
        )
        entry = {
            "report_id": report_id,
            "severity": decision.severity,
            "reasons": decision.reasons,
            "unresolved_blockers": extraction.unresolved_blockers,
            "flagged_fields": flagged[:20],
            "rows": [
                r.model_dump(mode="json")
                for r in extraction.rows
                if r.tonnage_mt is None or r.grade_value is None or r.warnings
            ],
        }
        path = self.out_dir / "review_queue.jsonl"
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")

    # --- 主流程 ---
    def run_case(
        self,
        report_id: str,
        pages: list[PageText],
        company: str | None = None,
        project: str | None = None,
    ) -> CaseResult:
        case_dir = self._case_dir(report_id)
        excerpt = render_pages(pages)

        extraction = self.extractor.extract(report_id, pages, company=company, project=project)
        self._snapshot(report_id, 1, extraction)
        self.log.record_extraction(report_id, 1, extraction, getattr(self.extractor, "model_name", None))

        critique = self.critic.critique(extraction, excerpt)
        critiques: list[CritiqueResult] = [critique]
        self.log.record_critique(
            report_id, 1, critique.score, list(critique.blocking_issues), critique.critic_model
        )

        rounds_used = 1
        while critique.score < self.cfg.pass_score and rounds_used < self.cfg.max_rounds:
            before, score_before = extraction, critique.score
            extraction = self.reviser.revise(report_id, extraction, critique, pages, case_id=report_id)
            rounds_used += 1
            self._snapshot(report_id, rounds_used, extraction)

            new_critique = self.critic.critique(extraction, excerpt)
            self.log.record_revision(
                report_id,
                rounds_used,
                before,
                extraction,
                list(critique.blocking_issues),
                score_before,
                new_critique.score,
            )
            self.log.record_critique(
                report_id,
                rounds_used,
                new_critique.score,
                list(new_critique.blocking_issues),
                new_critique.critic_model,
            )
            critique = new_critique
            critiques.append(critique)

        decision = abstain_mod.evaluate(extraction, critique, self.cfg, rounds_used)
        self.log.record_final(report_id, decision.abstain, decision.reasons, rounds_used)

        result = CaseResult(
            report_id=report_id,
            extraction=extraction,
            critiques=critiques,
            decision=decision,
            rounds_used=rounds_used,
            evolution=[EvolutionEntry(**e) for e in self.log.read() if e.get("case_id") == report_id],
        )
        self._write_json(case_dir / "final.json", result.model_dump(mode="json"))
        if decision.abstain:
            self._write_review_queue(report_id, extraction, critique, decision)
        return result

    def run_case_safe(
        self,
        report_id: str,
        pages: list[PageText],
        company: str | None = None,
        project: str | None = None,
    ) -> CaseResult:
        """单篇失败（网络/限流/解析异常）不应中断整批：降级为"拒答 + 待人工"。"""
        from .schemas import AbstainDecision

        try:
            return self.run_case(report_id, pages, company=company, project=project)
        except Exception as exc:  # noqa: BLE001
            decision = AbstainDecision(
                abstain=True,
                needs_human_review=True,
                reasons=[f"管线异常，已降级为拒答: {type(exc).__name__}: {exc}"],
                severity="high",
            )
            result = CaseResult(
                report_id=report_id,
                extraction=ExtractionResult(report_id=report_id),
                critiques=[],
                decision=decision,
                rounds_used=0,
            )
            self._write_json(self._case_dir(report_id) / "final.json", result.model_dump(mode="json"))
            self.log.record_final(report_id, True, decision.reasons, 0)
            return result
