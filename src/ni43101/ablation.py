"""复跑对比（ablation）：验证「Evolution Log -> few-shot -> 复跑」是否真的带来提升。

这正是需求最后一步："最后复跑看这个 log，看你是否能用它做 few-shot 改进"。

做法：
1. **基线跑**（few_shot_k=0）：不注入任何历史经验，日志里会沉淀下失败样例（out_ablation/baseline/evolution.jsonl）；
2. **复跑**（few_shot_k=N）：Reviser 从同一份日志里 mine 出历史失败样例注入 prompt，同样本再跑一遍；
3. 两次都用同一套 GT 评测，输出 out_ablation/ablation.md 做逐案例对比。

注意：两次共用同一份 evolution.jsonl —— 因为真实流程中"经验"就是这样累积的。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .config import Settings
from .evaluation import EvalResult, evaluate_run
from .evolution import EvolutionLog
from .runner import reset_outputs, run_reports


@dataclass
class AblationResult:
    base_dir: Path
    baseline: EvalResult
    rerun: EvalResult
    mined_examples: int


def _variant(settings: Settings, out_dir: str, log_path: str, few_shot_k: int) -> Settings:
    variant = settings.model_copy(deep=True)
    variant.run.out_dir = out_dir
    variant.run.evolution_log = log_path
    variant.run.few_shot_k = few_shot_k
    return variant


def run_ablation(
    settings: Settings,
    mock: bool,
    baseline_k: int = 0,
    rerun_k: int = 2,
    base_out: str = "out_ablation",
    on_case=None,
) -> AblationResult:
    base_dir = settings.path(base_out)
    if base_dir.exists():
        shutil.rmtree(base_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    shared_log = f"{base_out}/baseline/evolution.jsonl"

    baseline_settings = _variant(settings, f"{base_out}/baseline", shared_log, baseline_k)
    reset_outputs(baseline_settings)
    run_reports(baseline_settings, mock, on_case=on_case)

    rerun_settings = _variant(settings, f"{base_out}/fewshot", shared_log, rerun_k)
    rerun_settings.path(rerun_settings.run.out_dir).mkdir(parents=True, exist_ok=True)
    run_reports(rerun_settings, mock, on_case=on_case)

    baseline_eval = evaluate_run(
        baseline_settings, out_dir=baseline_settings.path(baseline_settings.run.out_dir)
    )
    rerun_eval = evaluate_run(rerun_settings, out_dir=rerun_settings.path(rerun_settings.run.out_dir))

    mined = len([e for e in EvolutionLog(settings.path(shared_log)).read() if e.get("stage") == "revise"])
    return AblationResult(
        base_dir=base_dir,
        baseline=baseline_eval,
        rerun=rerun_eval,
        mined_examples=mined,
    )


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def render_ablation_markdown(result: AblationResult) -> str:
    baseline, rerun = result.baseline.summary, result.rerun.summary
    by_id_b = {c.report_id: c for c in baseline.cases}
    by_id_r = {c.report_id: c for c in rerun.cases}

    lines = [
        "# 复跑对比（Evolution Log -> few-shot -> 复跑）",
        "",
        f"- 从 Evolution Log 中 mine 到的修复样例条数：**{result.mined_examples}**",
        "- 基线与复跑使用同一份 `evolution.jsonl`、同一套 ground truth、同一份夹具/数据",
        "",
        "## 总体",
        "",
        "| 指标 | 基线（无 few-shot） | 复跑（注入 few-shot） | 变化 |",
        "| --- | --- | --- | --- |",
        f"| 字段级准确率 | {_pct(baseline.overall_accuracy)} | {_pct(rerun.overall_accuracy)} | "
        f"{_delta(baseline.overall_accuracy, rerun.overall_accuracy)} |",
        f"| 拒答案例数 | {len(baseline.abstained_cases)} | {len(rerun.abstained_cases)} | "
        f"{len(rerun.abstained_cases) - len(baseline.abstained_cases):+d} |",
        f"| 硬给率 | {_pct(baseline.hard_give_rate)} | {_pct(rerun.hard_give_rate)} | "
        f"{_delta(baseline.hard_give_rate, rerun.hard_give_rate)} |",
        f"| 不可确定字段被硬给次数 | {baseline.unresolvable_filled_total} | "
        f"{rerun.unresolvable_filled_total} | {rerun.unresolvable_filled_total - baseline.unresolvable_filled_total:+d} |",
        "",
        "## 逐案例",
        "",
        "| 报告 | 基线准确率 | 复跑准确率 | 基线评分 | 复跑评分 | 基线决策 | 复跑决策 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    for report_id in sorted(set(by_id_b) | set(by_id_r)):
        case_b, case_r = by_id_b.get(report_id), by_id_r.get(report_id)
        lines.append(
            "| {rid} | {ba} | {ra} | {bs} | {rs} | {bd} | {rd} |".format(
                rid=report_id,
                ba=_pct(case_b.accuracy) if case_b else "n/a",
                ra=_pct(case_r.accuracy) if case_r else "n/a",
                bs=(case_b.final_score if case_b and case_b.final_score is not None else "n/a"),
                rs=(case_r.final_score if case_r and case_r.final_score is not None else "n/a"),
                bd=("拒答" if case_b and case_b.abstained else "放行") if case_b else "n/a",
                rd=("拒答" if case_r and case_r.abstained else "放行") if case_r else "n/a",
            )
        )

    lines += [
        "",
        "## 结论怎么读",
        "",
        "- **准确率提升**：说明从历史失败中提炼的经验确实能被复用（设计要求的 few-shot 改进成立）；",
        "- **拒答数不该因为复跑而变成 0**：该拒答的案例（原文不可确定）必须继续拒答——",
        "  如果复跑后这些案例被「放行」了，说明 few-shot 让它学会了硬给，是退化而不是提升。",
        "",
        "> mock 模式说明：这里的修复由 `mock.MockReviser` 用确定性配方（回原文表格核对 + 按 tonnage x grade 重算金属量）完成，",
        "> 只为证明链路通；真实模式下这一步由 LLM 依据 mine 出的 few-shot 完成。",
        "",
    ]
    return "\n".join(lines)


def _delta(before: float | None, after: float | None) -> str:
    if before is None or after is None:
        return "n/a"
    diff = (after - before) * 100
    return f"{diff:+.1f} pt"
