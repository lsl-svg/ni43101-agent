"""配置加载：settings.yaml + 环境变量覆盖。所有阈值集中一处，便于评审。"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field


def project_root() -> Path:
    env = os.getenv("NI_ROOT")
    if env and Path(env).is_dir():
        return Path(env).resolve()
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "config" / "settings.yaml").is_file():
            return parent
    return Path.cwd()


class ModelCfg(BaseModel):
    provider: str = "openai"
    model: str = "gpt-4o"
    temperature: float = 0.0


class RunCfg(BaseModel):
    max_rounds: int = 3
    pass_score: float = 8.0
    consistency_tol: float = 0.05
    max_missing_ratio: float = 0.25
    out_dir: str = "out"
    evolution_log: str = "out/evolution.jsonl"
    few_shot_k: int = 2


class PathsCfg(BaseModel):
    pdf_dir: str = "data/pdfs"
    text_dir: str = "data/txt"
    gt_dir: str = "data/ground_truth"
    mock_dir: str = "data/mock"
    prompts_dir: str = "prompts"


class Settings(BaseModel):
    run: RunCfg = Field(default_factory=RunCfg)
    models: dict[str, ModelCfg] = Field(
        default_factory=lambda: {
            "extractor": ModelCfg(provider="openai", model="gpt-4o"),
            "critic": ModelCfg(provider="zhipu", model="glm-4-plus"),
            "reviser": ModelCfg(provider="deepseek", model="deepseek-chat"),
        }
    )
    paths: PathsCfg = Field(default_factory=PathsCfg)
    root: Path = Field(default_factory=project_root)

    def path(self, relative: str) -> Path:
        return (self.root / relative).resolve()


def load_settings(path: str | Path | None = None) -> Settings:
    root = project_root()
    cfg_path = Path(path) if path else root / "config" / "settings.yaml"
    data: dict = {}
    if cfg_path.is_file():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    settings = Settings(**data)
    settings.root = root

    # 环境变量覆盖（便于 CI / docker 里临时调参，不用改文件）
    if os.getenv("NI_MAX_ROUNDS"):
        settings.run.max_rounds = int(os.environ["NI_MAX_ROUNDS"])
    if os.getenv("NI_PASS_SCORE"):
        settings.run.pass_score = float(os.environ["NI_PASS_SCORE"])
    if os.getenv("NI_CONSISTENCY_TOL"):
        settings.run.consistency_tol = float(os.environ["NI_CONSISTENCY_TOL"])
    return settings
