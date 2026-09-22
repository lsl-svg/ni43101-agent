"""组装与批量执行：被 CLI 与 ablation（复跑对比）复用，避免逻辑重复。"""

from __future__ import annotations

import shutil
from collections.abc import Callable

from .agents import CriticMasterAgent, ExtractorAgent, ReviserAgent
from .config import Settings
from .evolution import EvolutionLog
from .llm import PROVIDERS, LLMClient, provider_ready
from .mock import MockExtractor, MockReviser, mock_source_path
from .pdfio import load_from_txt, load_pages, select_relevant_pages
from .pipeline import Pipeline
from .schemas import CaseResult


def build_agents(settings: Settings, log: EvolutionLog, mock: bool):
    cfg = settings.run
    if mock:
        mock_dir = settings.path(settings.paths.mock_dir)
        return (
            MockExtractor(mock_dir),
            CriticMasterAgent(None, None, cfg),
            MockReviser(mock_dir, log=log, few_shot_k=cfg.few_shot_k),
        )

    missing = [n for n in ("extractor", "critic", "reviser") if not provider_ready(settings.models[n])]
    if missing:
        detail = "\n".join(
            f"  - {n}: provider={settings.models[n].provider} 需要环境变量 "
            f"{PROVIDERS.get(settings.models[n].provider, ('?',))[0]}"
            for n in missing
        )
        raise SystemExit(
            "缺少模型凭证，无法发起真实调用：\n"
            f"{detail}\n"
            "请复制 .env.example 为 .env 并填入 Key；或先用 `python -m ni43101.cli mock` 验证全链路。"
        )

    prompt_dir = settings.path(settings.paths.prompts_dir)
    return (
        ExtractorAgent(LLMClient(settings.models["extractor"]), prompt_dir / "extractor.md"),
        CriticMasterAgent(LLMClient(settings.models["critic"]), prompt_dir / "critic.md", cfg),
        ReviserAgent(LLMClient(settings.models["reviser"]), prompt_dir / "reviser.md", log, cfg.few_shot_k),
    )


def resolve_reports(settings: Settings, mock: bool, report_id: str | None = None):
    if mock:
        mock_dir = settings.path(settings.paths.mock_dir)
        suffix = ".source.txt"
        reports = [(p.name[: -len(suffix)], None) for p in sorted(mock_dir.glob(f"*{suffix}"))]
    else:
        reports = [(p.stem, p) for p in sorted(settings.path(settings.paths.pdf_dir).glob("*.pdf"))]
    if report_id:
        reports = [r for r in reports if r[0] == report_id]
    return reports


def reset_outputs(settings: Settings, wipe: bool = True) -> None:
    out_dir = settings.path(settings.run.out_dir)
    if wipe and out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = settings.path(settings.run.evolution_log)
    if wipe and log_path.exists():
        log_path.unlink()


def run_reports(
    settings: Settings,
    mock: bool,
    report_id: str | None = None,
    on_case: Callable[[CaseResult], None] | None = None,
) -> list[CaseResult]:
    """跑完目录下所有报告；单篇失败降级为拒答，不中断整批。"""
    log = EvolutionLog(settings.path(settings.run.evolution_log))
    extractor, critic, reviser = build_agents(settings, log, mock)
    pipeline = Pipeline(settings, extractor, critic, reviser, log)

    reports = resolve_reports(settings, mock, report_id)
    if not reports:
        target = settings.path(settings.paths.mock_dir if mock else settings.paths.pdf_dir)
        raise FileNotFoundError(f"未找到待处理报告，请把文件放到 {target}")

    results: list[CaseResult] = []
    for rid, pdf in reports:
        if mock:
            pages = load_from_txt(mock_source_path(settings.path(settings.paths.mock_dir), rid))
        else:
            pages = select_relevant_pages(load_pages(pdf, settings.path(settings.paths.text_dir)))
        case = pipeline.run_case_safe(rid, pages)
        results.append(case)
        if on_case:
            on_case(case)
    return results
