"""领域 schema：抽取结果、评审结果、拒答决策、Evolution Log 条目。

设计要点：
- 落盘的数值一律是**归一化后的规范单位**（tonnage 用 Mt、grade 用 g/t 或 %、contained 用 t），
  原始单位与原文引用保留在同一行里，保证可审计。
- 所有"是否放行"的阈值都不写死在这里，而是由 config/settings.yaml 提供。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ResourceCategory(str, Enum):
    measured = "measured"
    indicated = "indicated"
    inferred = "inferred"


class GradeUnit(str, Enum):
    gpt = "g/t"
    pct = "%"


class ResourceRow(BaseModel):
    """一行资源量数据（规范单位）。"""

    # 开启赋值校验：避免把裸字符串塞进枚举字段后，等到 .value 处才炸
    model_config = ConfigDict(validate_assignment=True)

    category: ResourceCategory
    commodity: str
    tonnage_mt: float | None = None
    grade_value: float | None = None
    grade_unit: GradeUnit | None = None
    contained_t: float | None = None
    page: int | None = None
    quote: str | None = None
    confidence: float | None = None
    warnings: list[str] = Field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        """行匹配键：类别 + 矿种（用于与 ground truth 对齐）。"""
        return (self.category.value, self.commodity.strip().upper())

    @property
    def label(self) -> str:
        return f"{self.category.value}/{self.commodity}"


class ExtractionResult(BaseModel):
    report_id: str
    company: str | None = None
    project: str | None = None
    rows: list[ResourceRow] = Field(default_factory=list)
    notes: str | None = None
    unresolved_blockers: list[str] = Field(default_factory=list)
    extractor_model: str | None = None


class FieldCritique(BaseModel):
    field: str
    verdict: Literal["ok", "suspect", "wrong", "missing"]
    confidence: float = 0.5
    comment: str = ""


class CritiqueResult(BaseModel):
    score: float
    field_critiques: list[FieldCritique] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)
    suggested_fixes: list[str] = Field(default_factory=list)
    critic_model: str | None = None

    @property
    def substantive_errors(self) -> list[FieldCritique]:
        return [c for c in self.field_critiques if c.verdict in ("wrong", "missing")]


class AbstainDecision(BaseModel):
    abstain: bool
    needs_human_review: bool = False
    reasons: list[str] = Field(default_factory=list)
    severity: Literal["none", "low", "medium", "high"] = "none"


class EvolutionEntry(BaseModel):
    """Evolution Log 单条记录：每次失败/降级/修复都自动 append。"""

    case_id: str
    round: int
    stage: Literal["extract", "critique", "revise", "final"]
    model: str | None = None
    score: float | None = None
    wrong_fields: list[str] = Field(default_factory=list)
    abstained: bool = False
    note: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class CaseResult(BaseModel):
    report_id: str
    extraction: ExtractionResult
    critiques: list[CritiqueResult] = Field(default_factory=list)
    decision: AbstainDecision
    rounds_used: int = 0
    evolution: list[EvolutionEntry] = Field(default_factory=list)

    @property
    def final_score(self) -> float | None:
        return self.critiques[-1].score if self.critiques else None
