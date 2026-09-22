"""Evolution Log：把每次失败/降级/修复落盘成 jsonl，并能反向喂回 few-shot。

闭环价值（需求明确要求"最后复跑"）：
  失败样例 -> evolution.jsonl -> mine_few_shot() -> 注入 Reviser prompt -> 复跑 -> 对比准确率
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .schemas import EvolutionEntry, ExtractionResult


class EvolutionLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    # --- 写 ---
    def append(self, entry: EvolutionEntry) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry.model_dump(mode="json"), ensure_ascii=False) + "\n")

    def record_extraction(
        self, case_id: str, round_no: int, result: ExtractionResult, model: str | None
    ) -> None:
        self.append(
            EvolutionEntry(
                case_id=case_id,
                round=round_no,
                stage="extract",
                model=model,
                note=f"抽取出 {len(result.rows)} 行",
                payload={"rows": [r.model_dump(mode="json") for r in result.rows]},
            )
        )

    def record_critique(
        self,
        case_id: str,
        round_no: int,
        score: float,
        wrong_fields: list[str],
        model: str | None,
        note: str = "",
    ) -> None:
        self.append(
            EvolutionEntry(
                case_id=case_id,
                round=round_no,
                stage="critique",
                model=model,
                score=score,
                wrong_fields=wrong_fields,
                note=note,
            )
        )

    def record_revision(
        self,
        case_id: str,
        round_no: int,
        before: ExtractionResult,
        after: ExtractionResult,
        blocking_issues: list[str],
        score_before: float,
        score_after: float,
    ) -> None:
        self.append(
            EvolutionEntry(
                case_id=case_id,
                round=round_no,
                stage="revise",
                score=score_after,
                wrong_fields=blocking_issues,
                note=f"评分 {score_before:.1f} -> {score_after:.1f}",
                payload={
                    "before_rows": [r.model_dump(mode="json") for r in before.rows],
                    "after_rows": [r.model_dump(mode="json") for r in after.rows],
                    "blocking_issues": blocking_issues,
                },
            )
        )

    def record_final(self, case_id: str, abstained: bool, reasons: Iterable[str], rounds_used: int) -> None:
        self.append(
            EvolutionEntry(
                case_id=case_id,
                round=rounds_used,
                stage="final",
                abstained=abstained,
                note="; ".join(reasons) or "通过",
            )
        )

    # --- 读 / 挖掘 ---
    def read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    def mine_few_shot(self, k: int = 2, exclude_case: str | None = None) -> str:
        """挑出"有错误且被修复"的历史样例，转成 Reviser 可读的 few-shot 文本。"""
        entries = [
            e
            for e in self.read()
            if e.get("stage") == "revise"
            and e.get("wrong_fields")
            and (e.get("payload") or {}).get("after_rows")
            and e.get("case_id") != exclude_case
        ]
        # 优先选"修复后行数更全"的样例
        entries.sort(key=lambda e: len(e["payload"]["after_rows"]), reverse=True)

        blocks: list[str] = []
        for idx, entry in enumerate(entries[:k], start=1):
            payload = entry["payload"]
            blocks.append(
                "\n".join(
                    [
                        f"【样例 {idx}】来源: {entry['case_id']}",
                        f"  被指出的错误: {'; '.join(entry['wrong_fields'][:4])}",
                        f"  修复前: {json.dumps(payload['before_rows'], ensure_ascii=False)[:600]}",
                        f"  修复后: {json.dumps(payload['after_rows'], ensure_ascii=False)[:600]}",
                        f"  结论: {entry.get('note', '')}",
                    ]
                )
            )
        return "\n\n".join(blocks) if blocks else "（暂无历史失败样例）"

    def stats(self) -> dict[str, Any]:
        entries = self.read()
        by_stage: dict[str, int] = {}
        wrong_field_counter: dict[str, int] = {}
        score_by_round: dict[str, list[float]] = {}
        for e in entries:
            by_stage[e.get("stage", "?")] = by_stage.get(e.get("stage", "?"), 0) + 1
            for field in e.get("wrong_fields") or []:
                key = field.split(":")[0][:60]
                wrong_field_counter[key] = wrong_field_counter.get(key, 0) + 1
            if e.get("stage") == "critique" and e.get("score") is not None:
                score_by_round.setdefault(str(e.get("round")), []).append(float(e["score"]))
        return {
            "total_entries": len(entries),
            "by_stage": by_stage,
            "top_wrong_fields": sorted(wrong_field_counter.items(), key=lambda kv: -kv[1])[:10],
            "avg_score_by_round": {
                r: round(sum(v) / len(v), 2) for r, v in sorted(score_by_round.items()) if v
            },
            "abstained_cases": [
                e["case_id"] for e in entries if e.get("stage") == "final" and e.get("abstained")
            ],
        }
