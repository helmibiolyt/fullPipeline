"""Text extraction from PDF / DOCX / PPTX, structure-aware where possible.

Returns a list of (page_no, text) blocks. Chunking (chunk.py) then splits these
into section-aware chunks. Kept dependency-light: pymupdf / python-docx /
python-pptx. (Swap in Docling later for richer table/section structure.)
"""
from __future__ import annotations

from pathlib import Path


def extract_blocks(path: str) -> list[tuple[int | None, str]]:
    suf = Path(path).suffix.lower()
    if suf == ".pdf":
        return _pdf(path)
    if suf in (".docx", ".doc"):
        return _docx(path)
    if suf in (".pptx", ".ppt"):
        return _pptx(path)
    return []


def _pdf(path: str):
    import fitz  # pymupdf
    out = []
    with fitz.open(path) as doc:
        for i, page in enumerate(doc, 1):
            txt = page.get_text("text").strip()
            if txt:
                out.append((i, txt))
    return out


def _docx(path: str):
    from docx import Document
    doc = Document(path)
    paras = [p.text for p in doc.paragraphs if p.text.strip()]
    # tables -> pipe-joined rows so tabular content survives
    for t in doc.tables:
        for row in t.rows:
            cells = [c.text.strip() for c in row.cells]
            if any(cells):
                paras.append(" | ".join(cells))
    return [(None, "\n".join(paras))] if paras else []


def _pptx(path: str):
    from pptx import Presentation
    prs = Presentation(path)
    out = []
    for i, slide in enumerate(prs.slides, 1):
        texts = [sh.text for sh in slide.shapes if sh.has_text_frame and sh.text.strip()]
        if texts:
            out.append((i, "\n".join(texts)))
    return out
