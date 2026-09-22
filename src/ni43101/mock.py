"""mock 模式的假 Agent：不调用任何 API，用夹具驱动全链路。

用途（也是"5 分钟跑起来"的基础）：
- 评审人不需要任何 API Key 就能验证 Extractor -> Critic -> Revise -> Abstain -> Eval 全流程；
- 夹具里**故意埋了 3 类典型错误**（单位错、品位小数点错位、幻觉引用），
  以及一条"原文确实无法确定"的样例，用来演示两件事：
    a) Revise Loop 能把错值修回来（评分上升）；
    b) 修不回来时系统 abstain 而不是硬给。

关于 MockReviser 的确定性修复（**仅 mock，且已在 README 中声明**）：
- 当 Evolution Log 里**还没有**任何失败经验时（few_shot_k=0 的基线跑），它选择"不修复"，
  对应真实场景里"没积累经验、Reviser 也改不动"；
- 当日志里已经沉淀了失败样例（复跑阶段）时，它执行一条确定的修复配方：**回原文表格逐行核对，
  用 tonnage x grade 重算金属量**，无依据的字段置 null 并登记 unresolved_blockers。
这条分支只为演示「日志 -> few-shot -> 复跑 -> 指标提升」的链路是通的；真实模式下该判断由 LLM 完成。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .agents.extractor import build_extraction
from .evolution import EvolutionLog
from .normalize import contained_tonnes, plausibility_warnings
from .pdfio import PageText, render_pages
from .schemas import CritiqueResult, ExtractionResult

_TABLE_LINE = re.compile(r"^\s*(measured|indicated|inferred)\b(.*)$", re.IGNORECASE)
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


class MockExtractor:
    model_name = "mock-extractor"

    def __init__(self, mock_dir: str | Path):
        self.mock_dir = Path(mock_dir)

    def fixture(self, report_id: str, round_no: int) -> dict:
        path = self.mock_dir / f"{report_id}.round{round_no}.json"
        if not path.is_file():
            raise FileNotFoundError(f"缺少夹具: {path}")
        return json.loads(path.read_text(encoding="utf-8"))

    def extract(
        self,
        report_id: str,
        pages: list[PageText],
        company: str | None = None,
        project: str | None = None,
    ) -> ExtractionResult:
        return build_extraction(
            report_id, self.fixture(report_id, 1), model=self.model_name, company=company, project=project
        )


def parse_source_table(source_text: str) -> dict[str, list[float]]:
    """从原文里解析 'Indicated 214.0 1.17 2.50' 这类表格行 -> {category: [tonnage, grade]}。"""
    table: dict[str, list[float]] = {}
    for line in source_text.splitlines():
        match = _TABLE_LINE.match(line)
        if not match:
            continue
        category = match.group(1).lower()
        numbers = [float(n.replace(",", "")) for n in _NUMBER.findall(match.group(2))]
        if len(numbers) >= 2:
            table[category] = numbers[:2]
    return table


class MockReviser:
    model_name = "mock-reviser"

    def __init__(self, mock_dir: str | Path, log: EvolutionLog | None = None, few_shot_k: int = 0):
        self.mock_dir = Path(mock_dir)
        self.log = log
        self.few_shot_k = few_shot_k

    # --- 有历史经验时的确定性修复配方 ---
    def _repair(self, extraction: ExtractionResult, source_text: str) -> ExtractionResult:
        table = parse_source_table(source_text)
        repaired = extraction.model_copy(deep=True)
        for row in repaired.rows:
            numbers = table.get(row.category.value)
            if not numbers:
                # 原文里该类别的行被截断 -> 无法溯源，置 null 并登记待人工
                if row.grade_value is not None:
                    row.grade_value = None
                    row.contained_t = None
                    row.quote = None
                    repaired.unresolved_blockers.append(
                        f"{row.label}.grade_value 在提供的节选中被截断，无法溯源；已置 null，待人工审核"
                    )
                row.warnings = ["grade 不可溯源"]
                continue
            tonnage, grade = numbers
            row.tonnage_mt = tonnage
            row.grade_value = grade
            row.quote = f"{row.category.value.capitalize()} {tonnage} {grade}"
            try:
                row.contained_t = contained_tonnes(
                    tonnage, grade, row.grade_unit.value if row.grade_unit else None
                )
            except Exception:  # noqa: BLE001 —— 单位不可用时保持原值，交由规则层继续报错
                pass
            # 关键：修正数值后必须**重算告警**，否则旧告警会残留（例如"品位 10.0 超区间"）
            row.warnings = ["contained 由 tonnage x grade 重算"]
            row.warnings.extend(plausibility_warnings(row))
        return repaired

    def revise(
        self,
        report_id: str,
        extraction: ExtractionResult,
        critique: CritiqueResult,
        pages: list[PageText],
        case_id: str | None = None,
    ) -> ExtractionResult:
        guidance = ""
        if self.log is not None and self.few_shot_k > 0:
            guidance = self.log.mine_few_shot(self.few_shot_k)
        if not guidance:
            # 基线：日志里还没有失败经验 -> 不做额外核对，原样返回（交由 abstain 兜底）
            return extraction
        return self._repair(extraction, render_pages(pages))


def mock_source_path(mock_dir: str | Path, report_id: str) -> Path:
    return Path(mock_dir) / f"{report_id}.source.txt"
