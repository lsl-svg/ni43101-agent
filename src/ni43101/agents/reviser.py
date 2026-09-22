"""Reviser Agent：按 CriticMaster 的反馈定点修复，并把 Evolution Log 的历史失败转成 few-shot。

关键约定（防"越改越错"）：
- 只改被指出的字段；
- 修复必须有原文依据，找不到依据就置 null + 写入 unresolved_blockers（交给上游 abstain），
  绝不允许"猜一个看起来合理的值"——这是本系统评分重点。
"""

from __future__ import annotations

from pathlib import Path

from ..evolution import EvolutionLog
from ..llm import LLMClient
from ..pdfio import PageText, render_pages
from ..schemas import CritiqueResult, ExtractionResult
from .extractor import build_extraction


class ReviserAgent:
    def __init__(
        self,
        llm: LLMClient,
        prompt_path: str | Path,
        log: EvolutionLog | None = None,
        few_shot_k: int = 2,
    ):
        self.llm = llm
        self.system_prompt = Path(prompt_path).read_text(encoding="utf-8")
        self.log = log
        self.few_shot_k = few_shot_k

    def revise(
        self,
        report_id: str,
        extraction: ExtractionResult,
        critique: CritiqueResult,
        pages: list[PageText],
        case_id: str | None = None,
    ) -> ExtractionResult:
        # 注意：这里**不排除当前案例**——"从自己过去的失败里学"正是设计要求的 few-shot 闭环；
        # 复跑时注入的样例就来自同一份 evolution.jsonl。
        few_shot = self.log.mine_few_shot(self.few_shot_k) if self.log else "（暂无）"
        system = self.system_prompt.replace("{{FEW_SHOT}}", few_shot)

        excerpt = render_pages(pages)
        user = "\n".join(
            [
                f"report_id: {report_id}",
                "",
                "当前抽取结果（JSON）：",
                extraction.model_dump_json(indent=2),
                "",
                f"CriticMaster 评分: {critique.score}",
                "必须修复的阻断项：",
                "\n".join(f"- {b}" for b in critique.blocking_issues) or "- （无）",
                "修改建议：",
                "\n".join(f"- {s}" for s in critique.suggested_fixes) or "- （无）",
                "",
                "原文节选（唯一事实来源）：",
                excerpt,
            ]
        )
        raw = self.llm.json(system, user)
        revised = build_extraction(
            report_id,
            raw,
            model=self.llm.cfg.model,
            company=extraction.company,
            project=extraction.project,
        )
        # 结构性防线：模型若把行删光/丢类别，视为未修复，保留原结果由 abstain 兜底
        if not revised.rows:
            revised.rows = extraction.rows
            revised.unresolved_blockers.append("Reviser 返回空行集，保留原始抽取结果")
        return revised
