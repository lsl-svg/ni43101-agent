"""命令行入口。

python -m ni43101.cli mock                 # 无需任何 API Key，跑通全链路 + 出评测报告
python -m ni43101.cli rerun --mock         # 复跑对比：Evolution Log -> few-shot -> 指标变化
python -m ni43101.cli run --all            # 真实模型 + data/pdfs 下的 PDF
python -m ni43101.cli run --report-id xxx
python -m ni43101.cli eval                 # 只跑评测
python -m ni43101.cli stats                # 打印 Evolution Log 统计
"""

from __future__ import annotations

import argparse
import json
import sys

from .ablation import render_ablation_markdown, run_ablation
from .config import Settings, load_settings
from .evaluation import evaluate_run, render_markdown
from .evolution import EvolutionLog
from .runner import reset_outputs, run_reports
from .schemas import CaseResult


def _print_case(case: CaseResult) -> None:
    flag = "ABSTAIN" if case.decision.abstain else "PASS"
    score = "n/a" if case.final_score is None else round(case.final_score, 1)
    print(
        f"[{flag}] {case.report_id}: rounds={case.rounds_used} score={score} rows={len(case.extraction.rows)}"
    )
    for reason in case.decision.reasons:
        print(f"        -> {reason}")


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


# --- 子命令 ---
def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    mock = bool(getattr(args, "mock", False))
    if mock and not getattr(args, "keep", False):
        reset_outputs(settings)

    try:
        results = run_reports(settings, mock, args.report_id, on_case=_print_case)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    abstained = [c for c in results if c.decision.abstain]
    print(f"完成 {len(results)} 篇：放行 {len(results) - len(abstained)}，待人工审核 {len(abstained)}")
    if abstained:
        print(f"待人工审核队列: {settings.path(settings.run.out_dir) / 'review_queue.jsonl'}")
    return 0


def cmd_eval(settings: Settings, args: argparse.Namespace) -> int:
    result = evaluate_run(settings, rel_tol=getattr(args, "tol", None))
    report_path = settings.path(settings.run.out_dir) / "report.md"
    report_path.write_text(render_markdown(result), encoding="utf-8")

    summary = result.summary
    print(f"案例数: {summary.n_cases}")
    print(f"字段级准确率: {_fmt(summary.overall_accuracy)}")
    print(f"硬给率: {_fmt(summary.hard_give_rate)}  拒答召回: {_fmt(summary.abstain_recall)}")
    print(f"报告已写入: {report_path}")
    for warn in result.warnings:
        print(f"  ! {warn}")
    return 0


def cmd_mock(settings: Settings, args: argparse.Namespace) -> int:
    args.mock = True
    args.keep = False
    args.report_id = None
    code = cmd_run(settings, args)
    if code != 0:
        return code
    print("-" * 60)
    return cmd_eval(settings, args)


def cmd_rerun(settings: Settings, args: argparse.Namespace) -> int:
    result = run_ablation(
        settings,
        mock=bool(getattr(args, "mock", False)),
        baseline_k=args.baseline_few_shot,
        rerun_k=args.rerun_few_shot,
        on_case=_print_case,
    )
    out = result.base_dir / "ablation.md"
    out.write_text(render_ablation_markdown(result), encoding="utf-8")

    print("-" * 60)
    print(f"mine 到修复样例: {result.mined_examples}")
    print(f"基线准确率: {_fmt(result.baseline.summary.overall_accuracy)}")
    print(f"复跑准确率: {_fmt(result.rerun.summary.overall_accuracy)}")
    print(
        f"基线拒答 {len(result.baseline.summary.abstained_cases)} -> 复跑拒答 {len(result.rerun.summary.abstained_cases)}"
    )
    print(f"对比报告: {out}")
    return 0


def cmd_stats(settings: Settings, args: argparse.Namespace) -> int:
    stats = EvolutionLog(settings.path(settings.run.evolution_log)).stats()
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ni43101", description="NI 43-101 资源量抽取 Agent")
    parser.add_argument("--settings", default=None, help="settings.yaml 路径")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="跑抽取-评审-修订-拒答全流程")
    p_run.add_argument("--all", action="store_true", help="处理目录下全部报告")
    p_run.add_argument("--report-id", default=None, help="只处理指定报告")
    p_run.add_argument("--mock", action="store_true", help="用夹具代替真实模型")
    p_run.add_argument("--keep", action="store_true", help="保留已有 out/ 与 evolution.jsonl")

    p_eval = sub.add_parser("eval", help="按 GT 计算字段级准确率与拒答指标")
    p_eval.add_argument("--tol", type=float, default=None, help="数值容差（默认取 settings）")

    p_rerun = sub.add_parser("rerun", help="复跑对比：Evolution Log -> few-shot 是否带来提升")
    p_rerun.add_argument("--mock", action="store_true", help="用夹具代替真实模型")
    p_rerun.add_argument("--baseline-few-shot", type=int, default=0, help="基线注入的 few-shot 条数")
    p_rerun.add_argument("--rerun-few-shot", type=int, default=2, help="复跑注入的 few-shot 条数")

    sub.add_parser("mock", help="等价于 run --mock 后再 eval")
    sub.add_parser("stats", help="打印 Evolution Log 统计")
    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，中文/符号输出会崩；统一切到 UTF-8（失败则忽略）
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass

    parser = build_parser()
    args = parser.parse_args(argv)
    settings = load_settings(args.settings)
    handlers = {
        "run": cmd_run,
        "eval": cmd_eval,
        "mock": cmd_mock,
        "rerun": cmd_rerun,
        "stats": cmd_stats,
    }
    return handlers[args.cmd](settings, args)


if __name__ == "__main__":
    raise SystemExit(main())
