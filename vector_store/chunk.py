"""Structure-aware chunking.

Splits extracted text into section-labelled chunks of ~CHUNK_TOKENS. Regulatory
docs (FDA labels, EMA EPARs) have named sections; we detect common ones so
retrieval can filter by `section` (e.g. only "contraindications" chunks).
"""
from __future__ import annotations

import re

from config import CHUNK_TOKENS, CHUNK_OVERLAP
from schema import Chunk

# Common label/EPAR/SmPC section keywords -> normalized section tag.
SECTION_KEYWORDS = {
    "indication": "indications", "contraindication": "contraindications",
    "posology": "posology", "dosage": "posology", "administration": "posology",
    "adverse": "adverse_effects", "undesirable effect": "adverse_effects",
    "side effect": "adverse_effects", "warning": "warnings",
    "precaution": "warnings", "interaction": "interactions",
    "pharmacodynamic": "mechanism", "mechanism of action": "mechanism",
    "pharmacokinetic": "pharmacokinetics", "overdose": "overdose",
    "pregnancy": "pregnancy", "composition": "composition",
    "clinical trial": "clinical_trials", "efficacy": "efficacy",
}
_HEADING = re.compile(r"^\s*([A-Z0-9][\w .()/-]{2,60})\s*$")


# Longest keyword first so "contraindication" wins over the substring "indication".
_KEYWORDS_ORDERED = sorted(SECTION_KEYWORDS.items(), key=lambda kv: -len(kv[0]))


def _detect_section(line: str) -> str | None:
    low = line.lower()
    for kw, tag in _KEYWORDS_ORDERED:
        if kw in low:
            return tag
    return None


def _language(text: str) -> str:
    for ch in text[:400]:
        o = ord(ch)
        if 0x0600 <= o <= 0x06FF:
            return "ar"
        if 0x4E00 <= o <= 0x9FFF:
            return "zh"
        if 0x3040 <= o <= 0x30FF:
            return "ja"
    return "en"


def _ntok(s: str) -> int:
    return len(s.split())


def chunk_document(blocks, source, doc_id, s3_key) -> list[Chunk]:
    """blocks: list[(page, text)] from extract.extract_blocks -> list[Chunk]."""
    chunks: list[Chunk] = []
    offset = 0
    cur_section = None
    for page, text in blocks:
        lang = _language(text)
        buf, buf_tok = [], 0
        for line in text.split("\n"):
            sec = _detect_section(line) if _HEADING.match(line) else None
            # A new section heading flushes the current chunk so one chunk = one section.
            if sec and sec != cur_section and buf and "".join(buf).strip():
                chunks.append(Chunk.new(
                    "\n".join(buf).strip(), source, doc_id, s3_key, offset,
                    page=page, section=cur_section, language=lang))
                offset += 1
                buf, buf_tok = [], 0
            if sec:
                cur_section = sec
            buf.append(line)
            buf_tok += _ntok(line)
            if buf_tok >= CHUNK_TOKENS:
                chunk_text = "\n".join(buf).strip()
                if chunk_text:
                    chunks.append(Chunk.new(
                        chunk_text, source, doc_id, s3_key, offset,
                        page=page, section=cur_section, language=lang))
                    offset += 1
                # keep a small overlap tail
                tail, ttok = [], 0
                for l in reversed(buf):
                    ttok += _ntok(l)
                    tail.insert(0, l)
                    if ttok >= CHUNK_OVERLAP:
                        break
                buf, buf_tok = tail, ttok
        if buf and "".join(buf).strip():
            chunks.append(Chunk.new(
                "\n".join(buf).strip(), source, doc_id, s3_key, offset,
                page=page, section=cur_section, language=lang))
            offset += 1
    return chunks
