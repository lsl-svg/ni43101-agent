"""Extractor Agent：调强模型把 NI 43-101 表格抽成结构化行。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..llm import LLMClient
from ..normalize import UnitError, canonicalize_raw_row
from ..pdfio import PageText, render_pages
from ..schemas import ExtractionResult


def build_extraction(
    report_id: str,
    raw: dict[str, Any],
    model: str | None = None,
    company: str | None = None,
    project: str | None = None,
) -> ExtractionResult:
    """原始 JSON（模型输出或夹具）-> 规范化 ExtractionResult。

    单行解析失败不应对整篇致命：把坏行记进 notes，其余照常返回。
    """
    result = ExtractionResult(
        report_id=raw.get("report_id") or report_id,
        company=raw.get("company") or company,
        project=raw.get("project") or project,
        notes=raw.get("notes"),
        unresolved_blockers=list(raw.get("unresolved_blockers") or []),
        extractor_model=model,
    )
    bad_rows: list[str] = []
    for idx, raw_row in enumerate(raw.get("rows") or []):
        if not isinstance(raw_row, dict):
            bad_rows.append(f"rows[{idx}]: 非对象")
            continue
        try:
            result.rows.append(canonicalize_raw_row(raw_row))
        except (ValidationError, UnitError, ValueError) as exc:
            bad_rows.append(f"rows[{idx}]: {type(exc).__name__} {exc}")
    if bad_rows:
        extra = "；".join(bad_rows[:5])
        result.notes = ((result.notes + " | ") if result.notes else "") + f"丢弃异常行: {extra}"
    return result


class ExtractorAgent:
    def __init__(self, llm: LLMClient, prompt_path: str | Path, model_name: str | None = None):
        self.llm = llm
        self.system_prompt = Path(prompt_path).read_text(encoding="utf-8")
        self.model_name = model_name or llm.cfg.model

    def extract(
        self,
        report_id: str,
        pages: list[PageText],
        company: str | None = None,
        project: str | None = None,
    ) -> ExtractionResult:
        excerpt = render_pages(pages)
        user = (
            f"report_id: {report_id}\n"
            f"company: {company or '未知'}\n"
            f"project: {project or '未知'}\n\n"
            "以下是报告节选（含表格）。请只依据它抽取资源量表格数据：\n\n"
            f"{excerpt}"
        )
        raw = self.llm.json(self.system_prompt, user)
        return build_extraction(report_id, raw, model=self.model_name, company=company, project=project)
