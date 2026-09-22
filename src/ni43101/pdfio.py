"""PDF 读取与"相关页"筛选。

工程取舍：
1. NI 43-101 报告动辄 100-300 页，全量塞进上下文既贵又容易被长上下文稀释。
   所以先按关键词给页打分（Indicated/Inferred/Resource/tonnes/grade/cut-off/Table），
   只把 Top-N 页送进抽取 prompt —— 这属于"检索前置"，而不是让模型自己翻 200 页。
2. 表格用 pdfplumber 的 extract_tables 单独取出（表格结构对本系统是关键信息），
   文本与表格一起给模型，避免纯文本流把列对错。
3. 无 PDF 解析环境时支持 sidecar .txt（按分页符切分），保证 mock / CI 可跑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

TABLE_KEYWORDS = (
    "indicated",
    "inferred",
    "measured",
    "mineral resource",
    "resource estimate",
    "tonnage",
    "grade",
    "cut-off",
    "contained",
    "mt",
    "g/t",
)


@dataclass
class PageText:
    page: int
    text: str
    tables: list[list[list[str]]] = field(default_factory=list)


def load_from_txt(path: Path, starts_at: int = 1) -> list[PageText]:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    chunks = re.split(r"\f|===+\s*PAGE[ _]?(\d+)\s*===+", raw)
    # re.split 带捕获组时，结果在 文本/编号 之间交替；这里做一次稳健重组
    pages: list[PageText] = []
    page_no = starts_at
    idx = 0
    while idx < len(chunks):
        chunk = chunks[idx]
        if idx + 1 < len(chunks) and chunks[idx + 1] and chunks[idx + 1].isdigit():
            page_no = int(chunks[idx + 1])
            pages.append(PageText(page=page_no, text=chunk.strip()))
            idx += 2
        else:
            pages.append(PageText(page=page_no, text=chunk.strip()))
            idx += 2 if idx + 1 < len(chunks) else 1
        page_no += 1
    return [p for p in pages if p.text]


def load_pages(pdf_path: str | Path, text_dir: str | Path | None = None) -> list[PageText]:
    """优先真实解析 PDF；解析库缺失时回退到同名 .txt sidecar。"""
    pdf_path = Path(pdf_path)
    try:
        import pdfplumber  # type: ignore

        pages: list[PageText] = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    tables = page.extract_tables() or []
                except Exception:  # noqa: BLE001 —— 个别页表格畸形不应中断整篇
                    tables = []
                pages.append(PageText(page=i, text=page.extract_text() or "", tables=tables))
        return pages
    except ImportError:
        pass

    if text_dir is not None:
        sidecar = Path(text_dir) / (pdf_path.stem + ".txt")
        if sidecar.is_file():
            return load_from_txt(sidecar)

    alt = pdf_path.with_suffix(".txt")
    if alt.is_file():
        return load_from_txt(alt)

    raise FileNotFoundError(
        f"无法解析 {pdf_path.name}：未安装 pdfplumber，且未找到同名 .txt sidecar（放在 data/txt/ 下）"
    )


def score_page(page: PageText) -> int:
    text = page.text.lower()
    score = sum(2 for kw in TABLE_KEYWORDS if kw in text)
    score += 3 * sum(len(t) for t in page.tables)  # 有表格的页权重更高
    return score


def select_relevant_pages(
    pages: list[PageText], max_pages: int = 12, max_chars: int = 60_000
) -> list[PageText]:
    """按关键词+表格打分挑页，控制上下文预算，最后按原页序返回。"""
    ranked = sorted(pages, key=score_page, reverse=True)
    chosen: list[PageText] = []
    budget = max_chars
    for page in ranked:
        if len(chosen) >= max_pages:
            break
        cost = len(page.text) + 80 * sum(len(t) for t in page.tables)
        if cost > budget and chosen:
            continue
        chosen.append(page)
        budget -= cost
    return sorted(chosen, key=lambda p: p.page)


def render_pages(pages: list[PageText], max_chars: int = 60_000) -> str:
    out: list[str] = []
    used = 0
    for page in pages:
        block = [f"=== PAGE {page.page} ===", page.text.strip()]
        for t_idx, table in enumerate(page.tables, start=1):
            rows = [" | ".join((cell or "").replace("\n", " ").strip() for cell in row) for row in table]
            block.append(f"--- TABLE {page.page}.{t_idx} ---")
            block.extend(rows)
        text = "\n".join(block)
        if used + len(text) > max_chars and out:
            break
        out.append(text)
        used += len(text)
    return "\n\n".join(out)
