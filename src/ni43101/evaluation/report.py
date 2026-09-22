"""把评测结果渲染成 Markdown 报告（评审人最先看的东西）。"""

from __future__ import annotations

import datetime as _dt
from typing import Any

FIELD_LABELS = {
    "category": "资源类别",
    "commodity": "矿种",
    "tonnage_mt": "矿石量 (Mt)",
    "grade_value": "品位",
    "grade_unit": "品位单位",
    "contained_t": "金属量 (t)",
}
FIELD_ORDER = ["category", "commodity", "tonnage_mt", "grade_value", "grade_unit", "contained_t"]


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _num(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def render_markdown(result, title: str = "NI 43-101 抽取 Agent — 评测报告") -> str:
    summary = result.summary
    lines: list[str] = [
        f"# {title}",
        "",
        f"- 生成时间：{_dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 数值容差：±{summary.rel_tol:.0%}（设计要求字段级 accuracy 容差 ±5%）",
        f"- 样本数：{summary.n_cases}",
        f"- 字段级准确率（总体）：**{_pct(summary.overall_accuracy)}**",
        f"- 硬给率（本应拒答却给出结论）：**{_pct(summary.hard_give_rate)}**",
        "",
        "## 1. 逐案例总览",
        "",
        "| 报告 | 轮数 | 最终评分 | 是否拒答 | 字段准确率 | 轮次准确率演进 | 硬给 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]

    for case in summary.cases:
        rounds = " → ".join(f"r{k}:{_pct(v)}" for k, v in sorted(case.round_accuracy.items())) or "n/a"
        lines.append(
            "| {rid} | {rounds_used} | {score} | {abstain} | {acc} | {evo} | {hard} |".format(
                rid=case.report_id,
                rounds_used=case.rounds_used,
                score=_num(case.final_score),
                abstain="✅ 是" if case.abstained else "否",
                acc=_pct(case.accuracy),
                evo=rounds,
                hard="⚠️ 是" if case.hard_give else "—",
            )
        )

    lines += [
        "",
        "## 2. 字段级准确率",
        "",
        "| 字段 | 正确 | 错误 | 漏抽 | 多抽 | 合计 | 准确率 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for name in FIELD_ORDER:
        stat = summary.field_totals.get(name)
        if stat is None:
            continue
        lines.append(
            f"| {FIELD_LABELS.get(name, name)} | {stat.correct} | {stat.wrong} | {stat.missing} | "
            f"{stat.spurious} | {stat.total} | {_pct(stat.accuracy)} |"
        )

    lines += [
        "",
        "## 3. 拒答指标（最关心的部分）",
        "",
        "| 指标 | 值 | 含义 |",
        "| --- | --- | --- |",
        f"| 应拒答案例数 | {len(summary.expect_abstain_cases)} | GT 标注 expect_abstain |",
        f"| 实际拒答案例数 | {len(summary.abstained_cases)} | 系统输出 abstain + 待人工审核 |",
        f"| 拒答召回 | {_pct(summary.abstain_recall)} | 该拒的都拒了吗 |",
        f"| 拒答精确率 | {_pct(summary.abstain_precision)} | 拒答是否都有理由（不过度拒答） |",
        f"| **硬给率** | **{_pct(summary.hard_give_rate)}** | 应拒答却输出结论的比例，越低越好 |",
        f"| 硬给字段数 | {summary.hard_give_field_total} | 未拒答案例中错/漏/多抽的字段总数 |",
        f"| GT 标注不可确定字段 | {summary.unresolvable_total} | 例如原文截断、表格残缺的字段 |",
        f"| **其中被硬给了值** | **{summary.unresolvable_filled_total}** | "
        f"对应比例 {_pct(summary.unresolvable_fill_rate)}，这是「是否硬给」最直接的证据 |",
        "",
        "## 4. 逐案例细节",
        "",
    ]
    for case in summary.cases:
        lines.append(f"### {case.report_id}")
        lines.append("")
        lines.append(f"- 最终评分：{_num(case.final_score)}；迭代轮数：{case.rounds_used}")
        lines.append(f"- 决策：{'拒答（待人工）' if case.abstained else '放行'}")
        if case.reasons:
            lines.append("- 拒答理由：")
            lines += [f"  - {r}" for r in case.reasons]
        if case.error_fields:
            lines.append(f"- 与 GT 的差异字段数：{case.error_fields}")
        if case.unresolvable_specs:
            filled = "、".join(case.unresolvable_filled) or "无"
            lines.append(f"- GT 不可确定字段：{'、'.join(case.unresolvable_specs)}；其中被硬给值的：{filled}")
        lines.append("")

    stats = result.evolution_stats or {}
    lines += [
        "## 5. Evolution Log 统计",
        "",
        f"- 总条目：{stats.get('total_entries', 0)}",
        f"- 按阶段：{stats.get('by_stage', {})}",
        f"- 各轮平均评分：{stats.get('avg_score_by_round', {})}",
        f"- 拒答案例：{stats.get('abstained_cases', [])}",
        "",
        "| 高频错误字段 | 次数 |",
        "| --- | --- |",
    ]
    for field_name, count in stats.get("top_wrong_fields", [])[:10]:
        lines.append(f"| {field_name} | {count} |")

    if result.warnings:
        lines += ["", "## 6. 警告", ""]
        lines += [f"- {w}" for w in result.warnings]

    lines.append("")
    return "\n".join(lines)
